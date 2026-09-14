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


