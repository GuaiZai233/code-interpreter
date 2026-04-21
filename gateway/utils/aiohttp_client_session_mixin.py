"""
AioHttp ClientSession shared management module.

Provides global shared aiohttp.ClientSession instance management via Mixin pattern.

Design pattern:
- **Mixin + ClassVar**: Uses ClassVar to store global singleton ClientSession
- **Explicit lifecycle management**: Manages resources via initialize_http_session() and close_http_session()
- **RuntimeError fast-fail**: Uses RuntimeError during initialization and access to ensure correct usage

Usage scenarios:
- Business classes that need to send HTTP requests
- Avoid creating new ClientSession for each request (performance optimization)
- Reuse connection pools, DNS cache, and other resources

Usage example:
    ```python
    from gateway.utils.aiohttp_client_session_mixin import AioHttpClientSessionClassVarMixin

    class MyService(AioHttpClientSessionClassVarMixin):
        # Instance method: use self.http_session property
        async def fetch_data(self, url: str) -> dict:
            async with self.http_session.get(url) as resp:
                return await resp.json()

        # Class method: use cls.get_http_session() classmethod
        @classmethod
        async def fetch_static_data(cls, url: str) -> dict:
            async with cls.get_http_session().get(url) as resp:
                return await resp.json()

    # Initialize at application startup
    await AioHttpClientSessionClassVarMixin.initialize_http_session()

    # Instance method usage
    service = MyService()
    data = await service.fetch_data("https://api.example.com/data")

    # Class method usage (no instance needed)
    static_data = await MyService.fetch_static_data("https://api.example.com/static")

    # Cleanup at application shutdown
    await AioHttpClientSessionClassVarMixin.close_http_session()
    ```

Advantages:
- Global singleton, avoids repeated ClientSession creation
- Connection pool reuse, improves performance
- Type safe (property returns explicit ClientSession type)
- Clear lifecycle (initialization and shutdown timing is clear)
- Built-in tracing for debugging
"""
import re
import ssl
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp
from aiohttp import TraceConfig, TraceRequestStartParams
from loguru import logger as l


_SENSITIVE_HEADERS: frozenset[str] = frozenset({
    'authorization', 'x-auth-token', 'cookie', 'set-cookie',
})
"""Headers whose values are redacted in trace logs to prevent credential leakage."""

_SENSITIVE_BODY_KEYS: frozenset[str] = frozenset({
    'service_key', 'password', 'secret', 'token', 'code',
})
"""JSON body keys whose values are redacted in trace logs."""

_MAX_TRACE_BODY_BYTES: int = 4096
"""Maximum request body bytes to accumulate for trace logging. Prevents OOM on large payloads."""


_SENSITIVE_URL_PARAMS: frozenset[str] = frozenset({
    'x-amz-signature', 'x-amz-credential', 'x-amz-security-token',
    'signature', 'policy', 'x-amz-date',
})
"""URL query parameter keys that indicate presigned credentials."""


def _sanitize_url(url: object) -> str:
    """Strip presigned credential params from URL for safe logging."""
    url_str = str(url)
    parsed = urlparse(url_str)
    if not parsed.query:
        return url_str
    params = parse_qs(parsed.query, keep_blank_values=True)
    has_sensitive = any(k.lower() in _SENSITIVE_URL_PARAMS for k in params)
    if not has_sensitive:
        return url_str
    safe_params = {
        k: ['***'] if k.lower() in _SENSITIVE_URL_PARAMS else v
        for k, v in params.items()
    }
    return parsed._replace(query=urlencode(safe_params, doseq=True)).geturl()


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Redact sensitive header values for safe logging."""
    return {
        k: ('***' if k.lower() in _SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }


def _sanitize_body(body_str: str) -> str:
    """Best-effort redaction of known sensitive keys in JSON body strings.

    Uses JSON-aware string matching: ``"(?:[^"\\\\]|\\\\.)*"`` handles escaped
    quotes (``\\"``) inside values, which are common in code execution payloads.
    """
    for key in _SENSITIVE_BODY_KEYS:
        body_str = re.sub(
            rf'("{key}"\s*:\s*)"(?:[^"\\]|\\.)*"',
            rf'\1"***"',
            body_str,
            flags=re.IGNORECASE,
        )
    return body_str


def _is_binary_upload(headers: dict[str, str]) -> bool:
    """Check if request is a binary/multipart upload (body should not be traced)."""
    ct = headers.get('Content-Type', headers.get('content-type', ''))
    return ct.startswith(('multipart/', 'application/octet-stream'))


async def _on_request_start(
    session: aiohttp.ClientSession,
    trace_config_ctx: aiohttp.tracing.SimpleNamespace,
    params: TraceRequestStartParams,
) -> None:
    """Records request info when request starts."""
    trace_config_ctx.method = params.method
    trace_config_ctx.url = params.url
    trace_config_ctx.headers = dict(params.headers)
    # Eagerly skip if Content-Type already set (e.g. PUT with octet-stream).
    # For multipart FormData POST, Content-Type is set AFTER on_request_start
    # (payload materializes later), so we defer to on_request_chunk_sent.
    trace_config_ctx.skip_body = _is_binary_upload(trace_config_ctx.headers)
    trace_config_ctx.body_chunks: list[bytes] = []
    trace_config_ctx.body_bytes_collected: int = 0


async def _on_request_chunk_sent(
    session: aiohttp.ClientSession,
    trace_config_ctx: aiohttp.tracing.SimpleNamespace,
    params: aiohttp.TraceRequestChunkSentParams,
) -> None:
    """Collects data when request body is sent (skipped for binary uploads).

    Deferred detection: for multipart FormData, Content-Type isn't available at
    on_request_start. The first chunk of a multipart body starts with the
    boundary marker (b'--'). Detect and stop accumulating immediately.
    """
    if trace_config_ctx.skip_body:
        return
    # Deferred multipart detection on first chunk
    if not trace_config_ctx.body_chunks and params.chunk[:2] == b'--':
        trace_config_ctx.skip_body = True
        return
    # Size cap: stop accumulating after threshold to prevent OOM on large payloads
    trace_config_ctx.body_bytes_collected += len(params.chunk)
    if trace_config_ctx.body_bytes_collected > _MAX_TRACE_BODY_BYTES:
        trace_config_ctx.skip_body = True
        return
    trace_config_ctx.body_chunks.append(params.chunk)


async def _on_request_end(
    session: aiohttp.ClientSession,
    trace_config_ctx: aiohttp.tracing.SimpleNamespace,
    params: aiohttp.TraceRequestEndParams,
) -> None:
    """Records complete request when request ends (with sensitive data redaction)."""
    safe_headers = _sanitize_headers(trace_config_ctx.headers)
    if trace_config_ctx.skip_body:
        total = trace_config_ctx.body_bytes_collected
        body_summary = f"(body not traced, {total} bytes total)" if total > _MAX_TRACE_BODY_BYTES else "(binary upload, body not traced)"
    else:
        body = b''.join(trace_config_ctx.body_chunks)
        body_str = body.decode('utf-8', errors='replace') if body else "(empty)"
        body_summary = _sanitize_body(body_str)
    safe_url = _sanitize_url(trace_config_ctx.url)
    l.debug(
        f"[HTTP Request] {trace_config_ctx.method} {safe_url}\n"
        f"Headers: {safe_headers}\n"
        f"Body: {body_summary}"
    )


async def _on_request_exception(
    session: aiohttp.ClientSession,
    trace_config_ctx: aiohttp.tracing.SimpleNamespace,
    params: aiohttp.TraceRequestExceptionParams,
) -> None:
    """Records detailed info when request exception occurs (URL sanitized)."""
    safe_url = _sanitize_url(trace_config_ctx.url)
    exc = params.exception
    # aiohttp ClientResponseError.__str__() embeds the full URL including presigned params.
    # Only log type + status to avoid credential leakage.
    if hasattr(exc, 'request_info'):
        exc_summary = f"HTTP {getattr(exc, 'status', '?')}: {type(exc).__name__}"
    else:
        exc_summary = f"{type(exc).__name__}: {exc}"
    l.error(
        f"[HTTP Request Exception] {trace_config_ctx.method} {safe_url}\n"
        f"Exception: {exc_summary}"
    )


def _create_trace_config() -> TraceConfig:
    """Creates request tracing configuration."""
    trace_config = TraceConfig()
    trace_config.on_request_start.append(_on_request_start)
    trace_config.on_request_chunk_sent.append(_on_request_chunk_sent)
    trace_config.on_request_end.append(_on_request_end)
    trace_config.on_request_exception.append(_on_request_exception)
    return trace_config


class AioHttpClientSessionClassVarMixin:
    """
    Mixin to provide a shared aiohttp ClientSession for asynchronous HTTP requests.

    The session must be initialized in an async context (e.g., FastAPI startup event)
    by calling `initialize_http_session()` before use.

    All classes inheriting this mixin share a single global ClientSession instance.
    """

    _http_session: ClassVar[aiohttp.ClientSession | None] = None
    _ssl_context: ClassVar[ssl.SSLContext | None] = None

    @classmethod
    async def initialize_http_session(
        cls,
        ssl_ca_cert_path: Path | None = None,
        disable_strict_verify: bool = False,
        **session_kwargs,
    ) -> None:
        """
        Initialize the aiohttp ClientSession in an async context.

        Should be called during application startup (e.g., FastAPI startup event).

        Args:
            ssl_ca_cert_path: CA certificate path (optional, for verifying self-signed certs)
            disable_strict_verify: Disable VERIFY_X509_STRICT (fixes Python 3.13+ intermittent verification failures)
            **session_kwargs: Optional keyword arguments to pass to aiohttp.ClientSession
        """
        if cls._http_session is not None and not cls._http_session.closed:
            raise RuntimeError(f"{cls.__name__}: HTTP session already initialized")

        # Configure SSL context
        if ssl_ca_cert_path:
            cls._ssl_context = ssl.create_default_context()
            cls._ssl_context.load_verify_locations(ssl_ca_cert_path)
            if disable_strict_verify:
                cls._ssl_context.verify_flags &= ~ssl.VERIFY_X509_STRICT

        # Create TCPConnector with connection pool parameters
        # limit: max concurrent connections (0 = unlimited)
        # limit_per_host: max connections per host (0 = unlimited)
        # keepalive_timeout: connection keep-alive time (seconds)
        # enable_cleanup_closed: cleanup closed connections
        # ttl_dns_cache: DNS cache time (seconds)
        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=30,
            keepalive_timeout=60,
            force_close=False,
            enable_cleanup_closed=True,
            ttl_dns_cache=300,
            ssl=cls._ssl_context,
        )
        session_kwargs['connector'] = connector

        # Configure timeout:
        # total: max time for entire request
        # connect: connection establishment timeout
        # sock_read: response body read timeout
        timeout = aiohttp.ClientTimeout(
            total=300,
            connect=30,
            sock_read=60,
        )
        session_kwargs.setdefault('timeout', timeout)

        cls._http_session = aiohttp.ClientSession(
            trust_env=False,
            trace_configs=[_create_trace_config()],
            **session_kwargs,
        )
        l.info(f"{cls.__name__}: HTTP session initialized")

    @classmethod
    def get_http_session(cls) -> aiohttp.ClientSession:
        """
        Get the aiohttp ClientSession instance at class level.

        Use this method when accessing the session from classmethods or without an instance.

        Returns:
            An instance of aiohttp.ClientSession.
        """
        if cls._http_session is None or cls._http_session.closed:
            raise RuntimeError(
                f"{cls.__name__}: HTTP session not initialized. "
                "Call `AioHttpClientSessionClassVarMixin.initialize_http_session()` "
                "during application startup (e.g., in FastAPI startup event)."
            )
        return cls._http_session

    @classmethod
    def get_ssl_context(cls) -> ssl.SSLContext | None:
        """Gets the SSL context."""
        return cls._ssl_context

    @property
    def http_session(self) -> aiohttp.ClientSession:
        """
        Get the aiohttp ClientSession instance.

        Delegates to the class-level get_http_session() method.

        Returns:
            An instance of aiohttp.ClientSession.
        """
        return self.__class__.get_http_session()

    @classmethod
    async def close_http_session(cls) -> None:
        """
        Close the aiohttp ClientSession if it is open.

        Should be called during application shutdown (e.g., FastAPI shutdown event).
        """
        if cls._http_session is None or cls._http_session.closed:
            return  # Already closed or never initialized — idempotent
        await cls._http_session.close()
        cls._http_session = None
        l.info(f"{cls.__name__}: HTTP session closed")
