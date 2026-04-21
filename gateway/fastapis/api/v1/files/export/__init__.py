"""
/files/export endpoint — NDJSON streaming response with per-file results.

Each file's export result (promoted/failed) is emitted as a separate JSON line
as soon as processing completes. Heartbeat lines are sent every 30 seconds
during long-running file operations to keep proxies and client connections alive.
"""
import asyncio
from collections.abc import AsyncGenerator

from loguru import logger as l
from starlette.responses import StreamingResponse

from gateway.fastapis.deps import WorkerDep
from gateway.fastapis.tagged_api_router import TaggedAPIRouter
from gateway.models.files import (
    ExportFailedEvent,
    ExportHeartbeatEvent,
    ExportPromotedEvent,
    FileExportItem,
    FileExportRequest,
)
from gateway.models.sandbox_filesystem import SandboxFileSystem
from gateway.models.worker import Worker

router = TaggedAPIRouter(prefix="/export", tag="File operations")

HEARTBEAT_INTERVAL_SECONDS = 30
"""Interval between heartbeat lines to keep proxies alive during slow file ops."""


async def _export_ndjson_stream(
    files: list[FileExportItem],
    sandbox_fs: SandboxFileSystem,
    worker: Worker,
) -> AsyncGenerator[bytes, None]:
    """
    Generate NDJSON lines: one per file result + periodic heartbeats.

    Files are processed sequentially with shared semaphore for cross-request
    concurrency control. Worker is touched per-file to prevent idle reaper
    from reclaiming during long exports.
    """
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def _heartbeat_loop() -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                worker.touch()
                event = ExportHeartbeatEvent()
                await queue.put(event.model_dump_json().encode() + b'\n')
        except asyncio.CancelledError:
            pass

    async def _process_files() -> None:
        for file_item in files:
            worker.touch()
            try:
                result = await sandbox_fs._run_with_semaphore(
                    sandbox_fs.export_file, file_item,
                )
                event = ExportPromotedEvent(
                    file_id=file_item.file_id,
                    name=result.name,
                    size=result.size,
                )
            except Exception as e:
                l.error(f"Export failed for {file_item.name}: {type(e).__name__}: {e}")
                event = ExportFailedEvent(
                    file_id=file_item.file_id,
                    error=f"{type(e).__name__}: {e}",
                )
            await queue.put(event.model_dump_json().encode() + b'\n')
        await queue.put(None)

    heartbeat_task = asyncio.create_task(_heartbeat_loop())
    process_task = asyncio.create_task(_process_files())

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
    finally:
        heartbeat_task.cancel()
        if not process_task.done():
            process_task.cancel()
        for task in (heartbeat_task, process_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


@router.post("")
async def export_files(request: FileExportRequest, worker: WorkerDep) -> StreamingResponse:
    """
    Export files from sandbox — NDJSON streaming response.

    Each file is processed independently: hash + metadata + S3 upload + backend promote.
    Results are streamed as NDJSON lines as they complete. Heartbeat lines are sent
    every 30s to keep proxy connections alive during slow uploads.

    **Response format** (``application/x-ndjson``), each line is one of::

        {"type":"heartbeat"}
        {"type":"promoted","file_id":"...","name":"...","size":N}
        {"type":"failed","file_id":"...","error":"..."}
    """
    l.debug(f"Export files request: {len(request.files)} file(s)")
    worker.touch()
    sandbox_fs = worker._get_sandbox_fs()
    return StreamingResponse(
        _export_ndjson_stream(request.files, sandbox_fs, worker),
        status_code=202,
        media_type='application/x-ndjson',
        headers={
            'X-Accel-Buffering': 'no',  # nginx: disable proxy buffering for streaming
            'Cache-Control': 'no-cache',
        },
    )
