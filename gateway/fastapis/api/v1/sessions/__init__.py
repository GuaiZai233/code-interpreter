"""
/sessions endpoint for Gateway service.
Provisions and reconfigures sandbox sessions with profiles and network policies.
"""
from loguru import logger as l
from starlette.status import HTTP_201_CREATED

from gateway.fastapis.tagged_api_router import TaggedAPIRouter
from gateway.models.sessions import SessionInitRequest, SessionInitResponse
from gateway.models.worker import WorkerPool
from gateway.models.exceptions import WorkerPoolShuttingDownError, WorkerProvisionError
from gateway.utils.http_exceptions import raise_bad_request, raise_service_unavailable

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

    return SessionInitResponse(
        user_uuid=request.user_uuid,
        profile=worker.profile,
        network=worker.network_mode,
        status="ready",
        allowed_hosts=worker.allowed_hosts,
        runtime_callback_url=worker.runtime_callback_url,
    )
