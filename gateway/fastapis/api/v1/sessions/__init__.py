"""
/sessions endpoint for Gateway service.
Provisions and reconfigures sandbox sessions with profiles and network policies.
"""
import asyncio
from uuid import UUID

import aiohttp
from fastapi import Request, Response
from loguru import logger as l
from starlette.status import HTTP_201_CREATED

from gateway.fastapis.tagged_api_router import TaggedAPIRouter
from gateway.models.sessions import SessionInitRequest, SessionInitResponse
from gateway.models.worker import WorkerPool
from gateway.models.exceptions import WorkerPoolShuttingDownError, WorkerProvisionError
from gateway.utils.aiohttp_client_session_mixin import AioHttpClientSessionClassVarMixin
from gateway.utils.http_exceptions import (
    raise_bad_request,
    raise_gateway_timeout,
    raise_not_found,
    raise_service_unavailable,
)

router = TaggedAPIRouter(prefix="/sessions", tag="Sessions")

SUPPORTED_PROFILES = {"go-builder", "action-runtime", "minimal"}
SUPPORTED_NETWORK_MODES = {"none", "public", "allowlist", "isolated"}


@router.post("", response_model=SessionInitResponse, status_code=HTTP_201_CREATED)
async def init_session(request: SessionInitRequest) -> SessionInitResponse:
    """
    Provisions a new sandbox session or binds an existing worker for a user_uuid,
    enforcing profile selection, resource limits, and network isolation policies.
    """
    profile = request.profile or "minimal"
    if profile not in SUPPORTED_PROFILES:
        raise_bad_request(
            f"Unsupported profile: {profile}. Supported profiles: {sorted(SUPPORTED_PROFILES)}"
        )

    network = request.network or "isolated"
    if network not in SUPPORTED_NETWORK_MODES:
        raise_bad_request(
            f"Unsupported network mode: {network}. Supported modes: {sorted(SUPPORTED_NETWORK_MODES)}"
        )

    # Normalize allowed hosts from both allowed_hosts and network_rules
    allowed_hosts = list(request.allowed_hosts)
    for rule in request.network_rules:
        if rule.port and rule.port > 0:
            formatted = f"{rule.host}:{rule.port}"
        else:
            formatted = rule.host
        if formatted and formatted not in allowed_hosts:
            allowed_hosts.append(formatted)

    if network == "allowlist" and not allowed_hosts:
        raise_bad_request(
            "allowlist network mode requires at least one allowed host or rule"
        )

    l.info(
        f"Session init request for user {request.user_uuid}: "
        f"profile={profile}, network={network}, allowed_hosts={len(allowed_hosts)}, "
        f"callback={request.runtime_callback_url}"
    )

    try:
        worker = await WorkerPool.create_session_for_user(
            user_uuid=request.user_uuid,
            profile=profile,
            network_mode=network,
            allowed_hosts=allowed_hosts,
            runtime_callback_url=request.runtime_callback_url,
            cpu_limit=request.cpu_limit,
            memory_limit_mb=request.memory_limit_mb,
            custom_env=request.env,
        )
    except WorkerPoolShuttingDownError as e:
        raise_service_unavailable(e.message)
    except WorkerProvisionError as e:
        raise_service_unavailable(e.message)

    target_cb = getattr(worker, "target_callback_url", None)
    runtime_cb = getattr(worker, "runtime_callback_url", None)
    callback_url = target_cb if isinstance(target_cb, str) else (runtime_cb if isinstance(runtime_cb, str) else None)

    return SessionInitResponse(
        user_uuid=request.user_uuid,
        profile=worker.profile,
        network=worker.network_mode,
        status="ready",
        allowed_hosts=worker.allowed_hosts,
        runtime_callback_url=callback_url,
    )


@router.api_route(
    "/{user_uuid}/callback/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
@router.api_route(
    "/{user_uuid}/callback",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def session_callback_proxy(user_uuid: UUID, request: Request, path: str = "") -> Response:
    """
    Reverse proxy endpoint for worker runtime callbacks.
    Forwards incoming requests from isolated worker containers to upstream runtime_callback_url.
    """
    worker = WorkerPool.get_active_worker_by_user(user_uuid)
    if not worker:
        raise_not_found(f"No active session found for user {user_uuid}")

    target_url_base = worker.target_callback_url or worker.runtime_callback_url
    if not target_url_base:
        raise_bad_request(f"No runtime callback URL configured for session {user_uuid}")

    target_url = target_url_base.rstrip("/")
    if path:
        target_url = f"{target_url}/{path.lstrip('/')}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    # Filter hop-by-hop headers
    excluded_headers = {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in excluded_headers
    }

    body = await request.body()
    http_session = AioHttpClientSessionClassVarMixin.get_http_session()

    l.debug(f"Proxying callback {request.method} for session {user_uuid} to {target_url}")

    try:
        async with http_session.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            data=body if body else None,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=30.0),
        ) as upstream_resp:
            resp_body = await upstream_resp.read()
            resp_headers = {
                k: v for k, v in upstream_resp.headers.items()
                if k.lower() not in excluded_headers
            }
            return Response(
                content=resp_body,
                status_code=upstream_resp.status,
                headers=resp_headers,
                media_type=upstream_resp.content_type,
            )
    except aiohttp.ClientError as e:
        l.error(f"Callback proxy error forwarding to {target_url}: {e}")
        raise_service_unavailable(f"Failed to reach upstream callback: {e}")
    except asyncio.TimeoutError:
        l.error(f"Callback proxy timeout forwarding to {target_url}")
        raise_gateway_timeout("Upstream callback timed out")
