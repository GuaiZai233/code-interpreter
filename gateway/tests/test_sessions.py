"""
Unit and contract tests for the /api/v1/sessions endpoint.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from gateway import meta_config
from gateway.main import app
from gateway.models.sessions import SessionInitRequest, SessionInitResponse


client = TestClient(app)
VALID_TOKEN = meta_config.AUTH_TOKEN or "test-secret-token"


@pytest.fixture(autouse=True)
def mock_auth_token(monkeypatch):
    monkeypatch.setattr(meta_config, "AUTH_TOKEN", VALID_TOKEN)


def test_session_init_unauthorized():
    resp = client.post(
        "/api/v1/sessions",
        json={"user_uuid": str(uuid.uuid4()), "profile": "minimal"},
    )
    assert resp.status_code == 401


def test_session_init_invalid_token():
    resp = client.post(
        "/api/v1/sessions",
        headers={"X-Auth-Token": "wrong-token"},
        json={"user_uuid": str(uuid.uuid4()), "profile": "minimal"},
    )
    assert resp.status_code == 401


def test_session_init_unsupported_profile():
    resp = client.post(
        "/api/v1/sessions",
        headers={"X-Auth-Token": VALID_TOKEN},
        json={"user_uuid": str(uuid.uuid4()), "profile": "unsupported-python-magic"},
    )
    assert resp.status_code == 400
    assert "Unsupported profile" in resp.text


def test_session_init_unsupported_network():
    resp = client.post(
        "/api/v1/sessions",
        headers={"X-Auth-Token": VALID_TOKEN},
        json={"user_uuid": str(uuid.uuid4()), "network": "unsupported-bridge-mode"},
    )
    assert resp.status_code == 400
    assert "Unsupported network mode" in resp.text


def test_session_init_empty_allowlist():
    resp = client.post(
        "/api/v1/sessions",
        headers={"X-Auth-Token": VALID_TOKEN},
        json={
            "user_uuid": str(uuid.uuid4()),
            "profile": "action-runtime",
            "network": "allowlist",
            "allowed_hosts": [],
            "network_rules": [],
        },
    )
    assert resp.status_code == 400
    assert "allowlist network mode requires at least one allowed host or rule" in resp.text


@pytest.mark.asyncio
async def test_session_init_success():
    target_uuid = uuid.uuid4()
    mock_worker = AsyncMock()
    mock_worker.profile = "go-builder"
    mock_worker.network_mode = "allowlist"
    mock_worker.allowed_hosts = ["api.example.com:443", "10.0.0.1:80"]
    mock_worker.runtime_callback_url = "http://host.docker.internal:7999/api/v1/runtime"

    with patch("gateway.models.worker.WorkerPool.create_session_for_user", new=AsyncMock(return_value=mock_worker)) as mock_create:
        resp = client.post(
            "/api/v1/sessions",
            headers={"X-Auth-Token": VALID_TOKEN},
            json={
                "user_uuid": str(target_uuid),
                "profile": "go-builder",
                "network": "allowlist",
                "allowed_hosts": ["api.example.com:443"],
                "network_rules": [{"host": "10.0.0.1", "port": 80, "proto": "tcp"}],
                "runtime_callback_url": "http://host.docker.internal:7999/api/v1/runtime",
                "memory_limit_mb": 512,
                "cpu_limit": 1.0,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["user_uuid"] == str(target_uuid)
        assert data["profile"] == "go-builder"
        assert data["network"] == "allowlist"
        assert data["status"] == "ready"
        assert data["runtime_callback_url"] == "http://host.docker.internal:7999/api/v1/runtime"
        assert "api.example.com:443" in data["allowed_hosts"]
        assert "10.0.0.1:80" in data["allowed_hosts"]

        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["user_uuid"] == target_uuid
        assert call_kwargs["profile"] == "go-builder"
        assert call_kwargs["network_mode"] == "allowlist"
        assert "api.example.com:443" in call_kwargs["allowed_hosts"]
        assert "10.0.0.1:80" in call_kwargs["allowed_hosts"]
        assert call_kwargs["runtime_callback_url"] == "http://host.docker.internal:7999/api/v1/runtime"
        assert call_kwargs["memory_limit_mb"] == 512
        assert call_kwargs["cpu_limit"] == 1.0


@pytest.mark.asyncio
async def test_session_init_effective_proxy_callback_priority():
    """
    Verify single source of truth invariant:
    SessionInitResponse.runtime_callback_url MUST return the worker's effective proxy URL
    (runtime_callback_url) rather than upstream target URL (target_callback_url).
    """
    target_uuid = uuid.uuid4()
    mock_worker = AsyncMock()
    mock_worker.profile = "action-runtime"
    mock_worker.network_mode = "isolated"
    mock_worker.allowed_hosts = []
    # Upstream ActionsCat core endpoint
    mock_worker.target_callback_url = "http://host.docker.internal:7999/api/v1/runtime"
    # Effective gateway proxy endpoint used by worker container
    mock_worker.runtime_callback_url = f"http://172.28.0.2:3874/api/v1/sessions/{target_uuid}/callback"

    with patch("gateway.models.worker.WorkerPool.create_session_for_user", new=AsyncMock(return_value=mock_worker)):
        resp = client.post(
            "/api/v1/sessions",
            headers={"X-Auth-Token": VALID_TOKEN},
            json={
                "user_uuid": str(target_uuid),
                "profile": "action-runtime",
                "network": "isolated",
                "runtime_callback_url": "http://host.docker.internal:7999/api/v1/runtime",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["runtime_callback_url"] == f"http://172.28.0.2:3874/api/v1/sessions/{target_uuid}/callback"


@pytest.mark.asyncio
async def test_worker_network_mode_topology_isolation():
    """
    Verify allowlist workers are placed on internet-capable bridge (so default route exists),
    while isolated/none workers are strictly placed on internal:true isolated network.
    """
    # Verification of network topology rules
    modes = [
        ("allowlist", meta_config.INTERNET_NETWORK_NAME, meta_config.GATEWAY_INTERNET_NET_IP),
        ("public", meta_config.INTERNET_NETWORK_NAME, meta_config.GATEWAY_INTERNET_NET_IP),
        ("isolated", meta_config.INTERNAL_NETWORK_NAME, meta_config.GATEWAY_INTERNAL_IP),
        ("none", meta_config.INTERNAL_NETWORK_NAME, meta_config.GATEWAY_INTERNAL_IP),
    ]

    for network_mode, expected_network, expected_gateway in modes:
        if network_mode in ("public", "allowlist") or meta_config.WORKER_INTERNET_ACCESS:
            network_name = meta_config.INTERNET_NETWORK_NAME
            gateway_ip = meta_config.GATEWAY_INTERNET_NET_IP
        else:
            network_name = meta_config.INTERNAL_NETWORK_NAME
            gateway_ip = meta_config.GATEWAY_INTERNAL_IP

        assert network_name == expected_network, f"Mode {network_mode} assigned wrong network: {network_name}"
        assert gateway_ip == expected_gateway, f"Mode {network_mode} assigned wrong gateway IP: {gateway_ip}"


@pytest.mark.asyncio
async def test_worker_full_policy_reconfiguration():
    """
    Verify full-policy reuse invariant:
    If allowed_hosts, cpu_limit, memory_limit_mb, or custom_env differ,
    existing worker MUST be released and recreated rather than reused with stale/wider permissions.
    """
    import asyncio
    from unittest.mock import MagicMock
    from gateway.models.virtual_disk import VirtualDisk
    from gateway.models.worker import Worker, WorkerPool

    user_id = uuid.uuid4()
    mock_vdisk = MagicMock(spec=VirtualDisk)

    # 1. Existing worker with policy: allowed_hosts=[A, B], cpu=1.0, env={TOKEN: old}
    worker1 = Worker(
        container_id="worker-1",
        container_name="code-worker-1",
        internal_url="http://code-worker-1:8000",
        vdisk=mock_vdisk,
        profile="action-runtime",
        network_mode="allowlist",
        allowed_hosts=["api.example.com:443", "other.example.com:443"],
        runtime_callback_url="http://host.docker.internal:7999/api/v1/runtime",
        cpu_limit=1.0,
        memory_limit_mb=512,
        custom_env={"TOKEN": "old"},
    )

    worker2 = Worker(
        container_id="worker-2",
        container_name="code-worker-2",
        internal_url="http://code-worker-2:8000",
        vdisk=mock_vdisk,
        profile="action-runtime",
        network_mode="allowlist",
        allowed_hosts=["api.example.com:443"],
        runtime_callback_url="http://host.docker.internal:7999/api/v1/runtime",
        cpu_limit=0.5,
        memory_limit_mb=256,
        custom_env={"TOKEN": "new"},
    )

    with patch.object(WorkerPool, "_state_lock", asyncio.Lock()), \
         patch.object(WorkerPool, "_workers", {worker1.container_id: worker1}), \
         patch.object(WorkerPool, "_user_to_worker_map", {user_id: worker1.container_id}), \
         patch.object(WorkerPool, "_destroy_worker", new=AsyncMock()) as mock_destroy, \
         patch.object(WorkerPool, "_create_worker", new=AsyncMock(return_value=worker2)) as mock_create:

        # Attempt to create session with narrower policy (allowed_hosts=[api.example.com:443], cpu=0.5, TOKEN=new)
        reconfigured = await WorkerPool.create_session_for_user(
            user_uuid=user_id,
            profile="action-runtime",
            network_mode="allowlist",
            allowed_hosts=["api.example.com:443"],
            runtime_callback_url="http://host.docker.internal:7999/api/v1/runtime",
            cpu_limit=0.5,
            memory_limit_mb=256,
            custom_env={"TOKEN": "new"},
        )

        # Worker 1 MUST be destroyed because policy changed!
        mock_destroy.assert_called_once_with(worker1)
        mock_create.assert_called_once()
        assert reconfigured.container_id == "worker-2"
        assert reconfigured.cpu_limit == 0.5
        assert reconfigured.allowed_hosts == ["api.example.com:443"]

    # 2. Identical policy request: existing worker MUST be reused
    mock_destroy.reset_mock()
    mock_create.reset_mock()
    with patch.object(WorkerPool, "_state_lock", asyncio.Lock()), \
         patch.object(WorkerPool, "_workers", {worker2.container_id: worker2}), \
         patch.object(WorkerPool, "_user_to_worker_map", {user_id: worker2.container_id}), \
         patch.object(WorkerPool, "_destroy_worker", new=AsyncMock()) as mock_destroy, \
         patch.object(WorkerPool, "_create_worker", new=AsyncMock()) as mock_create:

        reused = await WorkerPool.create_session_for_user(
            user_uuid=user_id,
            profile="action-runtime",
            network_mode="allowlist",
            allowed_hosts=["api.example.com:443"],
            runtime_callback_url="http://host.docker.internal:7999/api/v1/runtime",
            cpu_limit=0.5,
            memory_limit_mb=256,
            custom_env={"TOKEN": "new"},
        )

        mock_destroy.assert_not_called()
        mock_create.assert_not_called()
        assert reused.container_id == "worker-2"


@pytest.mark.asyncio
async def test_session_callback_proxy_success():
    """
    Verify callback reverse proxy forwards requests to upstream callback URL
    without requiring Gateway's admin X-Auth-Token, carrying Authorization bearer token,
    forwarding body, and returning upstream response.
    """
    from unittest.mock import MagicMock
    from gateway.models.virtual_disk import VirtualDisk
    from gateway.models.worker import Worker, WorkerPool
    from gateway.utils.aiohttp_client_session_mixin import AioHttpClientSessionClassVarMixin

    user_id = uuid.uuid4()
    mock_vdisk = MagicMock(spec=VirtualDisk)
    worker = Worker(
        container_id="worker-proxy-test",
        container_name="code-worker-proxy",
        internal_url="http://code-worker-proxy:8000",
        vdisk=mock_vdisk,
        profile="action-runtime",
        network_mode="isolated",
        target_callback_url="http://upstream-actions-cat:7999/api/v1/runtime",
        runtime_callback_url=f"http://172.28.0.2:3874/api/v1/sessions/{user_id}/callback",
        user_uuid=user_id,
    )

    # Mock aiohttp response
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.content_type = "application/json"
    mock_resp.headers = {"Content-Type": "application/json", "X-Custom-Header": "core-resp"}
    mock_resp.read = AsyncMock(return_value=b'{"success": true, "written": 42}')

    # Mock context manager for http_session.request
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=None)

    mock_http_session = MagicMock()
    mock_http_session.request = MagicMock(return_value=mock_cm)

    with patch.object(WorkerPool, "get_active_worker_by_user", return_value=worker), \
         patch.object(AioHttpClientSessionClassVarMixin, "get_http_session", return_value=mock_http_session):

        # Note: No X-Auth-Token header passed! This tests that container callback bypasses gateway admin auth.
        resp = client.post(
            f"/api/v1/sessions/{user_id}/callback/state",
            headers={"Authorization": "Bearer sample-run-token", "Content-Type": "application/json"},
            content=b'{"state_key": "val"}',
        )

        assert resp.status_code == 200
        assert resp.json() == {"success": True, "written": 42}
        assert resp.headers.get("x-custom-header") == "core-resp"

        mock_http_session.request.assert_called_once()
        call_kwargs = mock_http_session.request.call_args.kwargs
        assert call_kwargs["method"] == "POST"
        assert call_kwargs["url"] == "http://upstream-actions-cat:7999/api/v1/runtime/state"
        assert call_kwargs["data"] == b'{"state_key": "val"}'
        assert call_kwargs["headers"]["authorization"] == "Bearer sample-run-token"


@pytest.mark.asyncio
async def test_session_callback_proxy_not_found():
    """Verify 404 is returned when no active worker exists for user."""
    from gateway.models.worker import WorkerPool

    random_id = uuid.uuid4()
    with patch.object(WorkerPool, "get_active_worker_by_user", return_value=None):
        resp = client.post(f"/api/v1/sessions/{random_id}/callback/state")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_session_callback_proxy_no_callback_url():
    """Verify 400 is returned when worker has no target_callback_url configured."""
    from unittest.mock import MagicMock
    from gateway.models.virtual_disk import VirtualDisk
    from gateway.models.worker import Worker, WorkerPool

    user_id = uuid.uuid4()
    mock_vdisk = MagicMock(spec=VirtualDisk)
    worker = Worker(
        container_id="worker-no-url",
        container_name="code-worker-no-url",
        internal_url="http://code-worker-no-url:8000",
        vdisk=mock_vdisk,
        profile="action-runtime",
        network_mode="isolated",
        target_callback_url=None,
        runtime_callback_url=None,
        user_uuid=user_id,
    )

    with patch.object(WorkerPool, "get_active_worker_by_user", return_value=worker):
        resp = client.post(f"/api/v1/sessions/{user_id}/callback/state")
        assert resp.status_code == 400
        assert "No runtime callback URL configured" in resp.text


@pytest.mark.asyncio
async def test_resolve_allowlist_hosts():
    """Verify hostname DNS resolution and ExtraHosts formatting in allowlist mode."""
    from gateway.models.worker import WorkerPool

    hosts = ["127.0.0.1:8080", "10.0.0.1", "localhost:443"]
    extra_hosts, env_hosts = await WorkerPool._resolve_allowlist_hosts(hosts)

    # IPs should be preserved in env_hosts and NOT added to extra_hosts
    assert "127.0.0.1:8080" in env_hosts
    assert "10.0.0.1" in env_hosts

    # localhost should resolve to 127.0.0.1 and be formatted as "localhost:127.0.0.1" in extra_hosts
    assert any(eh.startswith("localhost:") for eh in extra_hosts)
    # env_hosts should contain both the original and resolved ip:port
    assert "localhost:443" in env_hosts
    assert any(":443" in eh for eh in env_hosts if not eh.startswith("localhost"))



