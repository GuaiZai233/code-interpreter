"""
Backend service domain model.

Encapsulates all interactions with the foxline-pro-backend-server API:
- JWT lifecycle (login via pre-shared key, cache, auto-refresh on 401)
- File promotion (POST /files/{file_id}/promotions)

Uses the shared aiohttp session from AioHttpClientSessionClassVarMixin
(connection pool shared with SandboxFileSystem and other models).
"""
import asyncio
from typing import ClassVar, Self

import aiohttp
from loguru import logger as l

from gateway.meta_config import meta_config
from gateway.utils.aiohttp_client_session_mixin import AioHttpClientSessionClassVarMixin

from .base import ModelBase


class BackendService(ModelBase, AioHttpClientSessionClassVarMixin):
    """
    Backend API rich domain model (classmethod singleton).

    Handles service JWT lifecycle and domain operations against the backend.
    Inherits AioHttpClientSessionClassVarMixin to reuse the shared HTTP session
    (same connection pool as SandboxFileSystem).
    """

    _token: ClassVar[str | None] = None
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        raise RuntimeError(f"{cls.__name__} is a classmethod singleton, do not instantiate")

    # ---- JWT lifecycle ----

    @classmethod
    async def _ensure_token(cls) -> str:
        """Get cached JWT, logging in lazily if needed."""
        async with cls._lock:
            if cls._token is None:
                await cls._login()
            return cls._token  # type: ignore[return-value]  # guarded by _login

    @classmethod
    async def _refresh_token(cls) -> str:
        """Force re-login (called on 401 from backend)."""
        async with cls._lock:
            cls._token = None
            await cls._login()
            return cls._token  # type: ignore[return-value]

    @classmethod
    async def _login(cls) -> None:
        url = f"{meta_config.BACKEND_BASE_URL}/api/v1/auth/service-jwt"
        async with cls.get_http_session().post(
            url,
            json={'service_name': 'sandbox', 'service_key': meta_config.SANDBOX_SERVICE_KEY},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status != 201:
                body = await response.text()
                raise RuntimeError(
                    f"Service login failed: POST {url} -> HTTP {response.status}: {body}"
                )
            data = await response.json()
            cls._token = data['token']
            l.info("Sandbox service JWT acquired")

    @classmethod
    async def shutdown(cls) -> None:
        """Clear cached token (call at app shutdown)."""
        cls._token = None

    # ---- Domain operations ----

    @classmethod
    async def promote_file(
        cls,
        file_id: str,
        *,
        content_hash: str,
        content_type: str,
        file_type: str,
        file_size: int,
        file_metadata: dict[str, object] | None = None,
    ) -> None:
        """
        Promote a PendingFile to its concrete STI type via backend API.

        Sends file metadata computed by the gateway (hash, MIME, dimensions/duration)
        to POST /files/{file_id}/promotions. The backend handles:
        - Storage quota reservation
        - STI type conversion (PendingFile -> ImageFile/VideoFile/etc.)
        - Async content moderation enqueue (taskiq)

        Auto-retries once on 401 (expired JWT -> re-login).

        :param file_id: PendingFile UUID (from export request)
        :param content_hash: SHA-256 hex digest (64 chars)
        :param content_type: detected MIME type
        :param file_type: target type ('image', 'video', 'audio', 'other')
        :param file_size: actual file size in bytes (from stat)
        :param file_metadata: type-specific metadata dict, e.g. {'width': 1024, 'height': 768}
        :raises aiohttp.ClientResponseError: on non-2xx response after retry
        """
        url = f"{meta_config.BACKEND_BASE_URL}/api/v1/files/{file_id}/promotions"
        payload: dict[str, object] = {
            'content_hash': content_hash,
            'content_type': content_type,
            'file_type': file_type,
            'size': file_size,
        }
        if file_metadata is not None:
            payload['file_metadata'] = file_metadata

        for attempt in range(2):
            token = (
                await cls._ensure_token()
                if attempt == 0
                else await cls._refresh_token()
            )
            async with cls.get_http_session().post(
                url,
                json=payload,
                headers={'Authorization': f'Bearer {token}'},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 401 and attempt == 0:
                    l.warning("Backend returned 401 on promote, refreshing service JWT")
                    continue
                if response.status >= 400:
                    body = await response.text()
                    l.error(f"Promote failed: POST {url} -> HTTP {response.status}: {body}")
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"promote failed: {body}",
                    )
                l.debug(f"Promoted file {file_id} via backend (type={file_type})")
                return
