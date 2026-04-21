"""
SandboxFileSystem rich domain model for file operations.

This module provides the SandboxFileSystem class which encapsulates all
sandbox file operations including path validation, upload, and export.
"""
import asyncio
import mimetypes
import os as sync_os
import stat as stat_module
import uuid as uuid_mod
from collections.abc import AsyncGenerator, Callable, Coroutine
from pathlib import PurePosixPath
from typing import Any, ClassVar, TypeVar

import aiofiles
import aiohttp
import anyio
import filetype as ft
import imagesize
import orjson
from aiofiles import os as async_os
from anyio import to_thread
from loguru import logger as l
from PIL import Image
from ssrf_protect.ssrf_protect import SSRFProtect, SSRFProtectException

from gateway import meta_config
from gateway.utils.aiohttp_client_session_mixin import AioHttpClientSessionClassVarMixin, _sanitize_url
from gateway.utils.file_hash import StreamingHasher

from .backend_service import BackendService
from .base import ModelBase
from .exceptions import BatchFileOperationError
from .field_types import Str256
from .files import (
    FileDownloadError,
    FileExportItem,
    FileExportResultItem,
    FileUploadItem,
    FileUploadResultItem,
    FileTooLargeError,
    SandboxFileRefBase,
)

# Type variables for generic file operation result aggregation
_FileItemT = TypeVar('_FileItemT', bound=SandboxFileRefBase)
_ResultT = TypeVar('_ResultT', FileUploadResultItem, FileExportResultItem)


class SandboxFileSystem(ModelBase, AioHttpClientSessionClassVarMixin):
    """
    Rich domain model for sandbox file system operations.

    Manages file uploads and exports with proper path validation and
    concurrency control. Follows the rich domain model pattern with
    properties directly held rather than using Config objects.

    The file_op_semaphore is passed in from WorkerPool to allow shared
    concurrency control across all workers.
    """
    # Constants
    SANDBOX_ROOT: ClassVar[str] = "/sandbox"
    CHUNK_SIZE: ClassVar[int] = 8192
    FILE_TRANSFER_TIMEOUT: ClassVar[aiohttp.ClientTimeout] = aiohttp.ClientTimeout(total=120.0, connect=10.0)

    # Properties (rich domain model - direct attributes)
    mount_point: Str256
    """Gateway-side mount point for direct sandbox access."""
    file_op_semaphore: asyncio.Semaphore
    """Shared semaphore for concurrency control (passed from WorkerPool)."""

    model_config = {'arbitrary_types_allowed': True}

    def compute_path(self, sandbox_path: str, filename: str) -> str:
        """
        Compute the mounted filesystem path for a sandbox file.

        Uses PurePosixPath.relative_to() for bulletproof path traversal prevention.
        This method resolves .. BEFORE validation, preventing normpath bypass attacks.

        Args:
            sandbox_path: Path in sandbox (e.g., /sandbox/data or /sandbox/data/file.txt).
                          If path already includes filename, it will be normalized.
            filename: Filename (e.g., input.csv).

        Returns:
            Full path in Gateway's mount point.

        Raises:
            ValueError: If path escapes sandbox boundary or contains invalid characters.
        """
        # Validate filename doesn't contain path separators
        if '/' in filename or '\\' in filename:
            raise ValueError("Invalid filename")

        # Use PurePosixPath for safe path handling
        sandbox_path_obj = PurePosixPath(sandbox_path)

        # Handle case where sandbox_path already includes the filename
        # e.g., path="/sandbox/file.txt", name="file.txt" -> extract dir "/sandbox"
        # This makes the API more robust against common caller mistakes
        if sandbox_path_obj.name == filename:
            sandbox_path_obj = sandbox_path_obj.parent
            l.debug(f"Auto-corrected path: extracted directory from full file path")

        # Use PurePosixPath for safe path validation
        # This handles all normalization internally and prevents traversal attacks
        sandbox_base = PurePosixPath(self.SANDBOX_ROOT)
        requested_dir = sandbox_path_obj
        full_path = requested_dir / filename

        # relative_to() raises ValueError if path escapes the base
        # This is the bulletproof check - works even after normalization
        try:
            relative = full_path.relative_to(sandbox_base)
        except ValueError:
            raise ValueError("Path escapes sandbox boundary")

        # Construct the final path in mount point
        return sync_os.path.join(self.mount_point, str(relative))

    async def upload_file(
        self,
        file_item: FileUploadItem,
        max_size_bytes: int,
    ) -> FileUploadResultItem:
        """
        Upload a single file to the sandbox via Gateway dual-mount.

        Downloads from the presigned URL and writes to the sandbox filesystem
        using atomic write (temp file + rename). Includes SSRF protection.

        Args:
            file_item: File upload item with path, name, and download URL.
            max_size_bytes: Maximum allowed file size in bytes.

        Returns:
            Upload result with full path and size.

        Raises:
            FileDownloadError: If SSRF protection blocks the URL.
            FileTooLargeError: If file exceeds size limit.
            aiohttp.ClientError: If download fails.
        """
        download_url = str(file_item.download_url)

        # SSRF protection
        if meta_config.SSRF_PROTECTION_ENABLED:
            try:
                SSRFProtect.validate(download_url)
            except SSRFProtectException as e:
                raise FileDownloadError(download_url, f"SSRF protection: {e}") from e

        target_path = self.compute_path(file_item.path, file_item.name)
        await async_os.makedirs(sync_os.path.dirname(target_path), exist_ok=True)

        tmp_path = f"{target_path}.{uuid_mod.uuid4().hex[:12]}.tmp"
        total_size = 0

        try:
            async with self.http_session.get(
                download_url,
                timeout=self.FILE_TRANSFER_TIMEOUT,
                allow_redirects=False,  # Prevent SSRF bypass via redirects
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_size_bytes:
                    raise FileTooLargeError(int(content_length), max_size_bytes)

                async with aiofiles.open(tmp_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(self.CHUNK_SIZE):
                        total_size += len(chunk)
                        if total_size > max_size_bytes:
                            raise FileTooLargeError(total_size, max_size_bytes)
                        await f.write(chunk)

            await async_os.rename(tmp_path, target_path)
        except (FileTooLargeError, FileDownloadError, aiohttp.ClientError, OSError) as e:
            # Sanitize: aiohttp ClientResponseError.__str__() embeds the full URL
            # (including presigned query params) in the error message
            err_msg = str(e)
            if hasattr(e, 'request_info'):
                err_msg = f"HTTP {getattr(e, 'status', '?')}: {type(e).__name__}"
            l.error(f"Upload failed: {err_msg}")
            try:
                await async_os.remove(tmp_path)
                l.debug(f"Cleaned up temp file: {tmp_path}")
            except FileNotFoundError:
                l.debug(f"Temp file already removed: {tmp_path}")
            except OSError as cleanup_err:
                l.error(f"Failed to clean up temp file {tmp_path}: {cleanup_err}")
            raise

        l.debug(f"Uploaded file ({total_size} bytes)")
        return FileUploadResultItem(
            full_path=f"{file_item.path}/{file_item.name}",
            size=total_size,
        )

    async def export_file(self, file_item: FileExportItem) -> FileExportResultItem:
        """
        Export a single file from the sandbox: hash, metadata, S3 upload, then promote.

        Flow per file:
        1. Stat + stream-read for SHA-256 hash (64KB chunks)
        2. Detect MIME type (mimetypes + filetype magic bytes)
        3. Extract metadata: Pillow (image) / ffprobe (video/audio)
        4. Upload to S3 via presigned POST (unchanged)
        5. POST backend /files/{file_id}/promotions to promote PendingFile

        :raises FileNotFoundError: source file missing
        :raises PermissionError: cannot read source file
        :raises aiohttp.ClientError: S3 upload or backend promote failed
        """
        source_path = self.compute_path(file_item.path, file_item.name)

        try:
            stat = await async_os.stat(source_path)
        except FileNotFoundError:
            l.error(f"Export failed: file not found: {file_item.path}/{file_item.name}")
            raise
        except PermissionError:
            l.error(f"Export failed: permission denied: {file_item.path}/{file_item.name}")
            raise

        # Reject non-regular files: FIFO/device/socket would block open() indefinitely
        # (user can mkfifo inside sandbox without privileges)
        if not stat_module.S_ISREG(stat.st_mode):
            raise ValueError(
                f"File {file_item.name} is not a regular file (mode={oct(stat.st_mode)})"
            )

        file_size = stat.st_size
        pre_mtime_ns = stat.st_mtime_ns

        # Size gate: reject before expensive hash/metadata/upload if backend provided a limit
        if file_item.max_size_bytes is not None and file_size > file_item.max_size_bytes:
            raise ValueError(
                f"File {file_item.name} ({file_size} bytes) exceeds export limit"
                f" ({file_item.max_size_bytes} bytes)"
            )

        # 1. Compute SHA-256 hash + sniff first 4KB for magic bytes
        content_hash, first_chunk = await self._compute_hash_and_sniff(source_path)

        # 2. Detect MIME type
        content_type = self._detect_mime(file_item.name, first_chunk)

        # 3. Extract type-specific metadata
        file_type, file_metadata = await self._extract_file_metadata(source_path, content_type)

        # 4. Upload to S3 (presigned POST or PUT)
        await self._upload_to_s3(file_item, source_path, file_size)

        # TOCTOU best-effort check: detect accidental modification between hash and S3 upload.
        # NOT a strong content-integrity proof: a malicious sandbox process can replace file
        # content and restore mtime via utime(), making the hash/S3 content diverge undetected.
        # Accepted trade-off: copy-to-temp is too expensive for the typical large-file case;
        # a streaming hash-during-upload approach would be the ideal fix (future work).
        # stat mtime_ns has nanosecond resolution — zero IO overhead, catches non-adversarial races.
        post_stat = await async_os.stat(source_path)
        if post_stat.st_mtime_ns != pre_mtime_ns or post_stat.st_size != file_size:
            l.error(
                f"File {file_item.name} was modified during export "
                f"(mtime: {pre_mtime_ns} -> {post_stat.st_mtime_ns}, "
                f"size: {file_size} -> {post_stat.st_size})"
            )
            raise ValueError(f"File {file_item.name} was modified during export, aborting promote")

        # 5. Promote via backend domain model
        await BackendService.promote_file(
            file_item.file_id,
            content_hash=content_hash,
            content_type=content_type,
            file_type=file_type,
            file_size=file_size,
            file_metadata=file_metadata,
        )

        l.debug(f"Exported file: {file_item.name} ({file_size} bytes, type={file_type})")
        return FileExportResultItem(
            path=file_item.path,
            name=file_item.name,
            size=file_size,
        )

    # ---- Export helper methods ----

    HASH_CHUNK_SIZE: ClassVar[int] = 65536
    """SHA-256 streaming chunk size (64KB)"""

    MAGIC_SNIFF_SIZE: ClassVar[int] = 4096
    """First N bytes captured for magic-byte MIME detection"""

    FFPROBE_TIMEOUT_SECONDS: ClassVar[int] = 30
    """ffprobe subprocess timeout"""

    @classmethod
    async def _compute_hash_and_sniff(cls, path: str) -> tuple[str, bytes]:
        """
        Stream-read file to compute SHA-256 hash and capture first bytes for MIME sniffing.

        :return: (hex_digest, first_4kb_bytes)
        """
        hasher = StreamingHasher()
        first_chunk = b''
        async with aiofiles.open(path, 'rb') as f:
            while chunk := await f.read(cls.HASH_CHUNK_SIZE):
                hasher.update(chunk)
                if not first_chunk:
                    first_chunk = chunk[:cls.MAGIC_SNIFF_SIZE]
        return hasher.hexdigest(), first_chunk

    @staticmethod
    def _detect_mime(name: str, first_chunk: bytes) -> str:
        """
        Detect MIME type: magic bytes authoritative, filename extension fallback.

        Magic bytes reflect the actual file content (not user-controlled),
        so they take priority. Filename extension is only used when magic-byte
        detection returns nothing (e.g., plain text, CSV, unknown formats).

        :return: MIME string (e.g. 'image/png'), defaults to 'application/octet-stream'
        """
        # Primary: magic bytes (reflects actual content, not user-controlled filename)
        if first_chunk:
            kind = ft.guess(first_chunk)
            if kind is not None:
                return kind.mime

        # Fallback: filename extension (for formats without magic bytes, e.g., .csv, .txt)
        mime, _ = mimetypes.guess_type(name)
        return mime or 'application/octet-stream'

    @classmethod
    async def _extract_file_metadata(
        cls, path: str, content_type: str,
    ) -> tuple[str, dict[str, int | float | None] | None]:
        """
        Extract type-specific metadata matching backend FileMetadata schema.

        :return: (file_type_str, metadata_dict_or_None)
        """
        if content_type.startswith('image/'):
            return 'image', await cls._extract_image_metadata(path)
        elif content_type.startswith('video/'):
            return 'video', await cls._extract_video_metadata(path)
        elif content_type.startswith('audio/'):
            return 'audio', await cls._extract_audio_metadata(path)
        return 'other', None

    @staticmethod
    async def _extract_image_metadata(path: str) -> dict[str, int | None]:
        """
        Extract image dimensions: imagesize primary, Pillow fallback.

        Both run via ``to_thread`` to avoid blocking the event loop on corrupted files.
        Follows backend ``ImageFile.extract_metadata_from_path`` pattern.
        """
        # Primary: imagesize (lightweight, seek-based, <0.1ms normal case)
        try:
            width, height = await to_thread.run_sync(imagesize.get, path)
            if width != -1 and height != -1:
                return {'width': width, 'height': height}
            l.debug("imagesize could not parse dimensions, falling back to Pillow")
        except Exception as e:
            l.debug(f"imagesize failed, falling back to Pillow: {e}")

        # Fallback: Pillow C extension (more robust, handles BMP etc.)
        try:
            def _pillow_get_size() -> tuple[int, int]:
                with Image.open(path) as img:
                    return img.size
            width, height = await to_thread.run_sync(_pillow_get_size)
            return {'width': width, 'height': height}
        except Exception as e:
            l.warning(f"Image metadata extraction failed: {e}")
            return {'width': None, 'height': None}

    @classmethod
    async def _extract_video_metadata(cls, path: str) -> dict[str, int | float | None]:
        """Extract video dimensions + duration via ffprobe."""
        data = await cls._run_ffprobe(path)
        if data is None:
            return {'width': None, 'height': None, 'duration_seconds': None}

        width: int | None = None
        height: int | None = None
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                width = stream.get('width')
                height = stream.get('height')
                break

        return {
            'width': width,
            'height': height,
            'duration_seconds': cls._parse_duration(data),
        }

    @classmethod
    async def _extract_audio_metadata(cls, path: str) -> dict[str, float | None]:
        """Extract audio duration via ffprobe."""
        data = await cls._run_ffprobe(path)
        if data is None:
            return {'duration_seconds': None}
        return {'duration_seconds': cls._parse_duration(data)}

    @classmethod
    async def _run_ffprobe(cls, source: str) -> dict[str, Any] | None:
        """
        Run ffprobe and return parsed JSON data.

        Shared by video and audio metadata extraction.
        Follows backend ``UserFile._run_ffprobe`` pattern.
        """
        try:
            with anyio.fail_after(cls.FFPROBE_TIMEOUT_SECONDS):
                result = await anyio.run_process(
                    ['ffprobe', '-v', 'quiet', '-print_format', 'json',
                     '-show_streams', '-show_format', source],
                    check=False,
                )
            if result.returncode != 0:
                l.warning(f"ffprobe returned non-zero for {source}: exit={result.returncode}")
                return None
            return orjson.loads(result.stdout)
        except TimeoutError:
            l.warning(f"ffprobe timed out ({cls.FFPROBE_TIMEOUT_SECONDS}s): {source}")
            return None
        except Exception as e:
            l.warning(f"ffprobe failed: {e}")
            return None

    @staticmethod
    def _parse_duration(ffprobe_data: dict[str, Any]) -> float | None:
        """Extract duration from ffprobe data (format.duration preferred, stream fallback)."""
        fmt = ffprobe_data.get('format', {})
        duration_str = fmt.get('duration')
        if duration_str is not None:
            try:
                return float(duration_str)
            except (ValueError, TypeError):
                pass

        for stream in ffprobe_data.get('streams', []):
            duration_str = stream.get('duration')
            if duration_str is not None:
                try:
                    return float(duration_str)
                except (ValueError, TypeError):
                    continue
        return None

    async def _upload_to_s3(
        self, file_item: FileExportItem, source_path: str, file_size: int,
    ) -> None:
        """Upload file to S3 via presigned POST or PUT."""
        try:
            if file_item.upload_fields is not None:
                file_handle = open(source_path, 'rb')  # noqa: ASYNC230
                try:
                    data = aiohttp.FormData()
                    for field_name, field_value in file_item.upload_fields.items():
                        data.add_field(field_name, field_value)
                    data.add_field(
                        'file', file_handle,
                        filename=file_item.name,
                        content_type='application/octet-stream',
                    )
                    async with self.http_session.post(
                        str(file_item.upload_url), data=data,
                        timeout=self.FILE_TRANSFER_TIMEOUT,
                    ) as response:
                        response.raise_for_status()
                finally:
                    file_handle.close()
            else:
                async def file_reader() -> AsyncGenerator[bytes, None]:
                    async with aiofiles.open(source_path, 'rb') as f:
                        while chunk := await f.read(self.CHUNK_SIZE):
                            yield chunk

                async with self.http_session.put(
                    str(file_item.upload_url),
                    data=file_reader(),
                    headers={
                        'Content-Type': 'application/octet-stream',
                        'Content-Length': str(file_size),
                    },
                    timeout=self.FILE_TRANSFER_TIMEOUT,
                ) as response:
                    response.raise_for_status()
        except aiohttp.ClientError as e:
            # Sanitize: ClientResponseError.__str__() embeds the full presigned URL
            err_msg = f"HTTP {getattr(e, 'status', '?')}: {type(e).__name__}" if hasattr(e, 'request_info') else str(e)
            l.error(f"S3 upload failed: {err_msg}")
            raise

    async def _run_with_semaphore(
        self,
        func: Callable[[_FileItemT], Coroutine[Any, Any, _ResultT]],
        item: _FileItemT,
    ) -> _ResultT:
        """Run an async function with semaphore-based concurrency limiting."""
        async with self.file_op_semaphore:
            return await func(item)

    @staticmethod
    def _aggregate_results(
        file_items: list[_FileItemT],
        results: list[_ResultT | BaseException],
        operation_type: str,
    ) -> list[_ResultT]:
        """
        Aggregate file operation results, raising on any failures.

        Args:
            file_items: Original file items.
            results: Results or exceptions from asyncio.gather.
            operation_type: "Upload" or "Export" for error messages.

        Returns:
            List of successful results.

        Raises:
            BatchFileOperationError: If any operations failed.
        """
        successful: list[_ResultT] = []
        failed: list[tuple[_FileItemT, BaseException]] = []

        for file_item, result in zip(file_items, results, strict=True):
            if isinstance(result, BaseException):
                failed.append((file_item, result))
            else:
                successful.append(result)

        if failed:
            # Extract error type/message without exposing internal paths
            first_exc = failed[0][1]
            error_type = type(first_exc).__name__
            raise BatchFileOperationError(
                operation=operation_type,
                failed_count=len(failed),
                total_count=len(file_items),
                first_error=error_type,
            )

        return successful

    async def upload_files(
        self,
        files: list[FileUploadItem],
        max_size_bytes: int,
    ) -> list[FileUploadResultItem]:
        """
        Upload multiple files to the sandbox with concurrency control.

        Args:
            files: List of file upload items.
            max_size_bytes: Maximum allowed file size in bytes.

        Returns:
            List of upload results.

        Raises:
            BatchFileOperationError: If any uploads failed.
        """
        l.debug(f"Uploading {len(files)} file(s)")

        async def upload_one(file_item: FileUploadItem) -> FileUploadResultItem:
            return await self.upload_file(file_item, max_size_bytes)

        results = await asyncio.gather(
            *[self._run_with_semaphore(upload_one, f) for f in files],
            return_exceptions=True,
        )
        return self._aggregate_results(files, results, "Upload")

    async def export_files(self, files: list[FileExportItem]) -> list[FileExportResultItem]:
        """
        Export multiple files from the sandbox with concurrency control.

        Args:
            files: List of file export items.

        Returns:
            List of export results.

        Raises:
            BatchFileOperationError: If any exports failed.
        """
        l.debug(f"Exporting {len(files)} file(s)")

        results = await asyncio.gather(
            *[self._run_with_semaphore(self.export_file, f) for f in files],
            return_exceptions=True,
        )
        return self._aggregate_results(files, results, "Export")
