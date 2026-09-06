#!/usr/bin/env python3
"""
Minimal Sandbox Runtime Test Suite

This test suite verifies the capabilities intentionally retained in the
minimal sandbox runtime:
- Phase 1: Gateway Auth, Health & Worker Pool Initialization
- Phase 2: Disabled Code Execution Boundary Enforcement (HTTP 501, no Worker allocation)
- Phase 3: Worker Allocation, Virtual Disk Mounting & Non-Root Sandbox Ownership
- Phase 4: IPTables Firewall Jail & Network Isolation
- Phase 5: File Operations via Gateway Dual-Mount Architecture
- Phase 6: Session Release, Virtual Disk Teardown & Worker Recycling

Run with: python test_all_phases.py
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid

import httpx

# =============================================================================
# Configuration
# =============================================================================

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:3874")
TIMEOUT = 30.0


# =============================================================================
# Utilities
# =============================================================================

def get_auth_token() -> str | None:
    """Retrieve auth token from the gateway container."""
    try:
        result = subprocess.run(
            ["docker", "exec", "code-interpreter_gateway", "cat", "/gateway/auth_token.txt"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        print(f"Failed to get auth token: {e}")
    return None


def run_docker_exec(container_name: str, cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command inside a docker container."""
    return subprocess.run(
        ["docker", "exec", container_name] + cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )


def find_worker_container(user_uuid: str) -> str | None:
    """Find the worker container assigned to user_uuid by checking gateway logs."""
    try:
        logs_res = subprocess.run(
            ["docker", "logs", "--tail", "200", "code-interpreter_gateway"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        pattern = rf"Assigned idle worker (code-worker-[a-f0-9]+) to user {user_uuid}"
        match = re.search(pattern, logs_res.stdout) or re.search(pattern, logs_res.stderr)
        if match:
            return match.group(1)
    except Exception as e:
        print(f"Failed to read gateway logs: {e}")

    try:
        ps_res = subprocess.run(
            ["docker", "ps", "--filter", "name=code-worker-", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        names = [n for n in ps_res.stdout.strip().splitlines() if n.startswith("code-worker-")]
        return names[0] if names else None
    except Exception:
        return None


class TestRunner:
    """Tracks and reports test results."""

    def __init__(self):
        self.results: dict[str, bool] = {}

    def record(self, test_id: str, passed: bool, message: str = ""):
        status = "PASS" if passed else "FAIL"
        self.results[test_id] = passed
        print(f"  [{status}] {test_id}: {message}")


# =============================================================================
# Phase 1: Gateway Auth, Health & Worker Pool Initialization
# =============================================================================

async def test_phase_1(runner: TestRunner, token: str):
    print("\n" + "=" * 70)
    print("PHASE 1: Gateway Auth, Health & Worker Pool Initialization")
    print("=" * 70)

    # 1.1: Verify unauthorized request without token is rejected
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GATEWAY_URL}/api/v1/status")
        passed = resp.status_code in (401, 422)
        runner.record("1.1", passed, f"Unauthorized request rejected (HTTP {resp.status_code})")

    # 1.2: Verify authorized request to /api/v1/status
    headers = {"X-Auth-Token": token}
    async with httpx.AsyncClient(headers=headers) as client:
        resp = await client.get(f"{GATEWAY_URL}/api/v1/status", timeout=TIMEOUT)
        data = resp.json() if resp.status_code == 200 else {}
        total = data.get("total_workers", 0)
        busy = data.get("busy_workers", 0)
        idle = total - busy
        passed = (resp.status_code == 200 and total >= 2 and idle >= 1)
        runner.record(
            "1.2",
            passed,
            f"Pool status healthy: total={total}, idle={idle}, busy={busy}",
        )


# =============================================================================
# Phase 2: Disabled Code Execution Boundary Enforcement
# =============================================================================

async def test_phase_2(runner: TestRunner, token: str):
    print("\n" + "=" * 70)
    print("PHASE 2: Disabled Code Execution Boundary Enforcement")
    print("=" * 70)

    headers = {"X-Auth-Token": token}
    dummy_uuid = str(uuid.uuid4())

    async with httpx.AsyncClient(headers=headers) as client:
        # Check pool before execute call
        status_before = (await client.get(f"{GATEWAY_URL}/api/v1/status")).json()
        busy_before = status_before.get("busy_workers", 0)

        # 2.1: Verify execute endpoint returns HTTP 501 Not Implemented
        resp = await client.post(
            f"{GATEWAY_URL}/api/v1/execute",
            params={"user_uuid": dummy_uuid},
            json={"code": "print('hello')"},
            timeout=TIMEOUT,
        )
        passed_501 = resp.status_code == 501
        runner.record(
            "2.1",
            passed_501,
            f"/api/v1/execute returns HTTP 501 Not Implemented (got {resp.status_code})",
        )

        # 2.2: Verify that /execute did NOT allocate a worker
        status_after = (await client.get(f"{GATEWAY_URL}/api/v1/status")).json()
        busy_after = status_after.get("busy_workers", 0)
        passed_no_alloc = busy_after == busy_before
        runner.record(
            "2.2",
            passed_no_alloc,
            f"No worker allocated by disabled execute (busy before: {busy_before}, after: {busy_after})",
        )


# =============================================================================
# Phase 3: Worker Allocation, Virtual Disk & Permissions
# =============================================================================

async def test_phase_3(runner: TestRunner, token: str, test_uuid: str) -> str | None:
    print("\n" + "=" * 70)
    print("PHASE 3: Worker Allocation, Virtual Disk & Permissions")
    print("=" * 70)

    headers = {"X-Auth-Token": token}
    async with httpx.AsyncClient(headers=headers) as client:
        # Trigger worker allocation via file upload endpoint using public IP URL
        test_file = {
            "path": "/sandbox/",
            "name": "runtime_check.txt",
            "download_url": "https://1.1.1.1/",
        }
        resp = await client.post(
            f"{GATEWAY_URL}/api/v1/files",
            params={"user_uuid": test_uuid},
            json={"files": [test_file]},
            timeout=TIMEOUT,
        )
        passed_alloc = resp.status_code == 201
        runner.record("3.1", passed_alloc, f"Worker allocated via file operation (HTTP {resp.status_code})")

    # Find the assigned worker container
    worker_name = find_worker_container(test_uuid)
    if not worker_name:
        runner.record("3.2", False, "No active worker containers found")
        return None

    # 3.2: Verify virtual disk is mounted to /sandbox as ext4
    df_res = run_docker_exec(worker_name, ["df", "-T", "/sandbox"])
    passed_mount = "ext4" in df_res.stdout and "/sandbox" in df_res.stdout
    runner.record("3.2", passed_mount, f"Virtual disk mounted as ext4 filesystem at /sandbox ({worker_name})")

    # 3.3: Verify /sandbox permissions (owned by sandbox:sandbox, UID 1000)
    ls_res = run_docker_exec(worker_name, ["ls", "-ld", "/sandbox"])
    passed_owner = "sandbox sandbox" in ls_res.stdout
    runner.record("3.3", passed_owner, f"/sandbox ownership is sandbox:sandbox ({ls_res.stdout.strip()})")

    # 3.4: Verify worker process runs under sandbox user (UID 1000)
    proc_res = run_docker_exec(
        worker_name,
        ["sh", "-c", "for p in /proc/[0-9]*; do [ -d \"$p\" ] && grep -s -E '^(Name|Uid):' \"$p/status\"; done"],
    )
    passed_user = "uvicorn" in proc_res.stdout and "1000" in proc_res.stdout
    runner.record("3.4", passed_user, "FastAPI worker process is running under 'sandbox' user (UID 1000)")

    return worker_name


# =============================================================================
# Phase 4: IPTables Firewall Jail & Network Isolation
# =============================================================================

async def test_phase_4(runner: TestRunner, worker_name: str):
    print("\n" + "=" * 70)
    print("PHASE 4: IPTables Firewall Jail & Network Isolation")
    print("=" * 70)

    if not worker_name:
        runner.record("4.1", False, "Worker container not available")
        runner.record("4.2", False, "Worker container not available")
        runner.record("4.3", False, "Worker container not available")
        return

    # 4.1: Verify INPUT chain policies and Gateway port restriction
    iptables_in = run_docker_exec(worker_name, ["iptables", "-L", "INPUT", "-v", "-n"])
    passed_in = "DROP" in iptables_in.stdout and "dpt:8000" in iptables_in.stdout
    runner.record("4.1", passed_in, "INPUT policy is DROP and only allows Gateway to port 8000")

    # 4.2: Verify OUTPUT chain policies (isolated mode drops non-local traffic)
    iptables_out = run_docker_exec(worker_name, ["iptables", "-L", "OUTPUT", "-v", "-n"])
    passed_out = "DROP" in iptables_out.stdout or "10.0.0.0/8" in iptables_out.stdout
    runner.record("4.2", passed_out, "OUTPUT policy enforces network jail rules")

    # 4.3: Verify worker cannot establish direct outbound internet connection in isolated mode
    curl_res = run_docker_exec(worker_name, ["curl", "-s", "--connect-timeout", "2", "https://1.1.1.1"])
    passed_isolated = curl_res.returncode != 0
    runner.record("4.3", passed_isolated, "Worker cannot reach public internet in isolated mode")


# =============================================================================
# Phase 5: File Operations (Gateway Dual-Mount Architecture)
# =============================================================================

async def test_phase_5(runner: TestRunner, token: str, test_uuid: str, worker_name: str):
    print("\n" + "=" * 70)
    print("PHASE 5: File Operations (Gateway Dual-Mount Architecture)")
    print("=" * 70)

    headers = {"X-Auth-Token": token}
    async with httpx.AsyncClient(headers=headers) as client:
        # 5.1: Verify uploaded file exists on the virtual disk inside worker's /sandbox
        check_file = run_docker_exec(worker_name, ["test", "-f", "/sandbox/runtime_check.txt"])
        passed_exists = check_file.returncode == 0
        runner.record("5.1", passed_exists, "Uploaded file exists in /sandbox on virtual disk")

        # 5.2: Verify path traversal attempt is blocked by Gateway validation (400 or 422)
        bad_file = {
            "path": "/sandbox/../../etc",
            "name": "malicious.txt",
            "download_url": "https://1.1.1.1/",
        }
        traversal_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/files",
            params={"user_uuid": test_uuid},
            json={"files": [bad_file]},
            timeout=TIMEOUT,
        )
        passed_traversal = traversal_resp.status_code in (400, 422)
        runner.record(
            "5.2",
            passed_traversal,
            f"Path traversal blocked by Gateway validation (HTTP {traversal_resp.status_code})",
        )

        # 5.3: Verify export of non-existent file returns 404 Not Found
        export_req = {
            "files": [{
                "path": "/sandbox/",
                "name": "non_existent_file.txt",
                "upload_url": "https://1.1.1.1/",
            }]
        }
        export_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/files/export",
            params={"user_uuid": test_uuid},
            json=export_req,
            timeout=TIMEOUT,
        )
        passed_404 = export_resp.status_code == 404
        runner.record(
            "5.3",
            passed_404,
            f"Exporting non-existent file returns 404 Not Found (HTTP {export_resp.status_code})",
        )


# =============================================================================
# Phase 6: Session Release, Virtual Disk Teardown & Worker Recycling
# =============================================================================

async def test_phase_6(runner: TestRunner, token: str, test_uuid: str, worker_name: str):
    print("\n" + "=" * 70)
    print("PHASE 6: Session Release, Virtual Disk Teardown & Worker Recycling")
    print("=" * 70)

    headers = {"X-Auth-Token": token}
    async with httpx.AsyncClient(headers=headers) as client:
        # 6.1: Call release endpoint
        rel_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/release",
            params={"user_uuid": test_uuid},
            timeout=TIMEOUT,
        )
        passed_rel = rel_resp.status_code == 204
        runner.record("6.1", passed_rel, f"Session released successfully (HTTP {rel_resp.status_code})")

    # Allow gateway cleanup to complete
    await asyncio.sleep(2.5)

    # 6.2: Verify the released worker container was destroyed
    inspect_res = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={worker_name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    passed_destroyed = worker_name not in inspect_res.stdout.strip().splitlines()
    runner.record("6.2", passed_destroyed, f"Worker container {worker_name} destroyed on release")

    # 6.3: Verify idle worker pool was replenished
    async with httpx.AsyncClient(headers=headers) as client:
        status = (await client.get(f"{GATEWAY_URL}/api/v1/status", timeout=TIMEOUT)).json()
        total = status.get("total_workers", 0)
        busy = status.get("busy_workers", 0)
        idle_count = total - busy
        passed_replenished = idle_count >= 1
        runner.record("6.3", passed_replenished, f"Idle worker pool replenished (idle={idle_count})")


# =============================================================================
# Main
# =============================================================================

async def main():
    print("=" * 70)
    print("MINIMAL SANDBOX RUNTIME TEST SUITE")
    print("=" * 70)

    token = get_auth_token()
    if not token:
        print("\nERROR: Could not get auth token. Is the service running?")
        sys.exit(1)

    print(f"\nAuth Token: {token[:20]}...")
    test_uuid = str(uuid.uuid4())
    print(f"Test Session UUID: {test_uuid}")

    runner = TestRunner()

    await test_phase_1(runner, token)
    await test_phase_2(runner, token)
    worker_name = await test_phase_3(runner, token, test_uuid)
    if worker_name:
        await test_phase_4(runner, worker_name)
        await test_phase_5(runner, token, test_uuid, worker_name)
        await test_phase_6(runner, token, test_uuid, worker_name)
    else:
        print("\nSkipping Phases 4-6 due to worker allocation failure.")

    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)

    passed = sum(1 for v in runner.results.values() if v)
    total = len(runner.results)

    for test_id, res in sorted(runner.results.items()):
        status = "PASS" if res else "FAIL"
        print(f"  Test {test_id}: {status}")

    print(f"\nTotal: {passed}/{total} tests passed")
    print("=" * 70)

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
