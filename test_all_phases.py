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
- Phase 6: Shell Execution API (Non-Root, Bounded Streams, Timeout Process-Tree Cleanup & Confinement)
- Phase 7: Session Release, Virtual Disk Teardown & Worker Recycling

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

GATEWAY_URL = os.environ.get("GATEWAY_URL", f"http://127.0.0.1:{os.environ.get('GATEWAY_PORT', '13874')}")
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
# Phase 6: Shell Execution API
# =============================================================================

async def test_phase_6(runner: TestRunner, token: str, test_uuid: str, worker_name: str):
    print("\n" + "=" * 70)
    print("PHASE 6: Shell Execution API")
    print("=" * 70)

    headers = {"X-Auth-Token": token}
    params = {"user_uuid": test_uuid}

    async with httpx.AsyncClient(headers=headers) as client:
        # 6.1: Basic execution (echo hello)
        resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "echo hello"},
            timeout=TIMEOUT,
        )
        data = resp.json() if resp.status_code == 200 else {}
        passed_basic = (
            resp.status_code == 200
            and data.get("stdout") == "hello\n"
            and data.get("stderr") == ""
            and data.get("exit_code") == 0
            and data.get("timed_out") is False
            and data.get("stdout_truncated") is False
            and data.get("stderr_truncated") is False
            and data.get("duration_ms", -1) >= 0
        )
        runner.record("6.1", passed_basic, f"Basic shell execution (stdout={data.get('stdout')!r}, exit_code={data.get('exit_code')})")

        # 6.2: Stderr capture
        resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "echo err >&2"},
            timeout=TIMEOUT,
        )
        data = resp.json() if resp.status_code == 200 else {}
        passed_stderr = (
            resp.status_code == 200
            and data.get("stdout") == ""
            and data.get("stderr") == "err\n"
            and data.get("exit_code") == 0
        )
        runner.record("6.2", passed_stderr, f"Stderr captured separately (stderr={data.get('stderr')!r})")

        # 6.3: Non-zero exit code (exit 7)
        resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "exit 7"},
            timeout=TIMEOUT,
        )
        data = resp.json() if resp.status_code == 200 else {}
        passed_exit7 = (
            resp.status_code == 200
            and data.get("exit_code") == 7
            and data.get("timed_out") is False
        )
        runner.record("6.3", passed_exit7, f"Non-zero exit code returns HTTP 200 with exit_code=7 (got {data.get('exit_code')})")

        # 6.4: Working directory (default /sandbox & custom subdir)
        resp_def = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "pwd"},
            timeout=TIMEOUT,
        )
        data_def = resp_def.json() if resp_def.status_code == 200 else {}

        await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "mkdir -p /sandbox/subdir"},
            timeout=TIMEOUT,
        )
        resp_sub = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "pwd", "cwd": "/sandbox/subdir"},
            timeout=TIMEOUT,
        )
        data_sub = resp_sub.json() if resp_sub.status_code == 200 else {}
        passed_cwd = (
            resp_def.status_code == 200
            and data_def.get("stdout", "").strip() == "/sandbox"
            and resp_sub.status_code == 200
            and data_sub.get("stdout", "").strip() == "/sandbox/subdir"
        )
        runner.record("6.4", passed_cwd, "Default cwd is /sandbox and custom cwd /sandbox/subdir works")

        # 6.5: CWD path traversal rejection (/etc, /sandbox/.., /sandbox/../../etc)
        traversal_paths = ["/etc", "/sandbox/..", "/sandbox/../../etc"]
        traversal_results = []
        for bad_cwd in traversal_paths:
            t_resp = await client.post(
                f"{GATEWAY_URL}/api/v1/shell/exec",
                params=params,
                json={"command": "pwd", "cwd": bad_cwd},
                timeout=TIMEOUT,
            )
            traversal_results.append(t_resp.status_code in (400, 422))
        passed_traversal = all(traversal_results)
        runner.record("6.5", passed_traversal, "CWD traversal rejected for /etc, /sandbox/.., etc (all 400/422)")

        # 6.6: Symlink escape rejection
        await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "ln -sfn /etc /sandbox/escape"},
            timeout=TIMEOUT,
        )
        sym_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "pwd", "cwd": "/sandbox/escape"},
            timeout=TIMEOUT,
        )
        passed_symlink = sym_resp.status_code in (400, 422)
        runner.record("6.6", passed_symlink, f"Symlink escape rejected by real-path resolution (HTTP {sym_resp.status_code})")

        # 6.7: Filesystem persistence across commands in same session
        await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "echo 'hello persistence' > /sandbox/state.txt"},
            timeout=TIMEOUT,
        )
        cat_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "cat /sandbox/state.txt"},
            timeout=TIMEOUT,
        )
        cat_data = cat_resp.json() if cat_resp.status_code == 200 else {}
        passed_fs_persist = (
            cat_resp.status_code == 200
            and cat_data.get("stdout", "").strip() == "hello persistence"
        )
        runner.record("6.7", passed_fs_persist, "Filesystem persists across shell invocations in same session")

        # 6.8: Shell execution state does not persist (fresh bash process)
        await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "export TESTVAR=abc123xyz"},
            timeout=TIMEOUT,
        )
        var_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "printf '%s' \"$TESTVAR\""},
            timeout=TIMEOUT,
        )
        var_data = var_resp.json() if var_resp.status_code == 200 else {}
        passed_fresh_shell = (
            var_resp.status_code == 200
            and var_data.get("stdout") == ""
        )
        runner.record("6.8", passed_fresh_shell, "Shell environment is isolated per command (variables not persisted)")

        # 6.9: Command timeout & child process reap (sleep 10 with timeout=1)
        t_start = time.time()
        to_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "sleep 10", "timeout": 1.0},
            timeout=TIMEOUT,
        )
        t_elapsed = time.time() - t_start
        to_data = to_resp.json() if to_resp.status_code == 200 else {}
        await asyncio.sleep(0.5)
        ps_sleep = run_docker_exec(worker_name, ["pgrep", "-f", "sleep 10"])
        passed_timeout = (
            to_resp.status_code == 200
            and to_data.get("timed_out") is True
            and to_data.get("exit_code") is None
            and t_elapsed < 4.0
            and ps_sleep.returncode != 0
        )
        runner.record("6.9", passed_timeout, f"Timeout terminates process and leaves no orphans (timed_out=True, elapsed={t_elapsed:.2f}s)")

        # 6.10: Process tree cleanup (bash -c 'sleep 30 & wait')
        t_start = time.time()
        pt_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "bash -c 'sleep 30 & wait'", "timeout": 1.0},
            timeout=TIMEOUT,
        )
        t_elapsed = time.time() - t_start
        pt_data = pt_resp.json() if pt_resp.status_code == 200 else {}
        await asyncio.sleep(0.5)
        ps_tree_sleep = run_docker_exec(worker_name, ["pgrep", "-f", "sleep 30"])
        passed_tree_cleanup = (
            pt_resp.status_code == 200
            and pt_data.get("timed_out") is True
            and ps_tree_sleep.returncode != 0
        )
        runner.record("6.10", passed_tree_cleanup, "Process group cleanup terminates entire process tree on timeout")

        # 6.11: Output truncation & memory safety (5MB output)
        trunc_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "yes x | head -c 5000000"},
            timeout=TIMEOUT,
        )
        trunc_data = trunc_resp.json() if trunc_resp.status_code == 200 else {}
        stdout_len = len(trunc_data.get("stdout", "").encode("utf-8"))
        passed_trunc = (
            trunc_resp.status_code == 200
            and trunc_data.get("stdout_truncated") is True
            and stdout_len <= 1024 * 1024 + 1024
            and trunc_data.get("exit_code") == 0
        )
        runner.record("6.11", passed_trunc, f"Output bounded and truncated without OOM (stdout_bytes={stdout_len}, truncated={trunc_data.get('stdout_truncated')})")

        # 6.12: Non-root sandbox user verification (UID 1000)
        id_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "id -u && id -un"},
            timeout=TIMEOUT,
        )
        id_data = id_resp.json() if id_resp.status_code == 200 else {}
        id_lines = id_data.get("stdout", "").strip().splitlines()
        passed_sandbox_user = (
            id_resp.status_code == 200
            and len(id_lines) >= 2
            and id_lines[0].strip() == "1000"
            and id_lines[1].strip() == "sandbox"
        )
        runner.record("6.12", passed_sandbox_user, "Commands execute strictly under 'sandbox' user (UID 1000)")

        # 6.13: Rootfs write protection (/worker write denied, /sandbox write permitted)
        rw_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "touch /worker/testfile 2>&1"},
            timeout=TIMEOUT,
        )
        rw_data = rw_resp.json() if rw_resp.status_code == 200 else {}

        sw_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "touch /sandbox/testfile && echo ok"},
            timeout=TIMEOUT,
        )
        sw_data = sw_resp.json() if sw_resp.status_code == 200 else {}
        passed_rootfs_prot = (
            rw_data.get("exit_code") != 0
            and sw_data.get("exit_code") == 0
            and sw_data.get("stdout", "").strip() == "ok"
        )
        runner.record("6.13", passed_rootfs_prot, "Rootfs write protection: /worker write denied, /sandbox write permitted")

        # 6.14: Offline network isolation via firewall jail
        net_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "curl -s --connect-timeout 2 https://example.com"},
            timeout=TIMEOUT,
        )
        net_data = net_resp.json() if net_resp.status_code == 200 else {}
        passed_net = (
            net_resp.status_code == 200
            and net_data.get("exit_code") != 0
        )
        runner.record("6.14", passed_net, f"Outbound network traffic blocked in isolated mode (exit_code={net_data.get('exit_code')})")

        # 6.15: Loopback / internal access to Worker control port 8000 is blocked
        # Both iptables and verify_gateway_source middleware prevent commands in container
        # from accessing http://127.0.0.1:8000/api/v1/shell/exec
        loopback_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "curl -s -m 2 http://127.0.0.1:8000/api/v1/shell/exec || echo BLOCKED"},
            timeout=TIMEOUT,
        )
        loopback_data = loopback_resp.json() if loopback_resp.status_code == 200 else {}
        passed_loopback = (
            loopback_resp.status_code == 200
            and "BLOCKED" in loopback_data.get("stdout", "")
            and "detail" not in loopback_data.get("stdout", "")
        )
        runner.record("6.15", passed_loopback, "Loopback access to worker port 8000 is blocked by iptables / gateway verification")

        # 6.16: Background job (&) containment and orphan reaping
        # Commands spawning detached background jobs (&) cannot leave surviving processes
        bg_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "sleep 60 >/dev/null 2>&1 & echo launched"},
            timeout=TIMEOUT,
        )
        bg_data = bg_resp.json() if bg_resp.status_code == 200 else {}
        await asyncio.sleep(0.5)
        ps_bg_sleep = run_docker_exec(worker_name, ["pgrep", "-f", "sleep 60"])
        passed_bg_cleanup = (
            bg_resp.status_code == 200
            and bg_data.get("stdout", "").strip() == "launched"
            and ps_bg_sleep.returncode != 0
        )
        runner.record("6.16", passed_bg_cleanup, "Background job (&) terminated upon command completion without lingering orphans")

        # 6.17: Daemonization / setsid() process escape containment
        # Processes attempting to escape via setsid() are reaped by PR_SET_CHILD_SUBREAPER + proc tracking
        setsid_cmd = (
            "python3 -c \""
            "import os, time; "
            "pid = os.fork(); "
            "if pid == 0: "
            "    os.setsid(); "
            "    time.sleep(60)\" & echo daemon_spawned"
        )
        daemon_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": setsid_cmd},
            timeout=TIMEOUT,
        )
        daemon_data = daemon_resp.json() if daemon_resp.status_code == 200 else {}
        await asyncio.sleep(0.5)
        ps_daemon = run_docker_exec(worker_name, ["pgrep", "-f", "time.sleep(60)"])
        passed_daemon = (
            daemon_resp.status_code == 200
            and daemon_data.get("stdout", "").strip() == "daemon_spawned"
            and ps_daemon.returncode != 0
        )
        runner.record("6.17", passed_daemon, "setsid() daemon escape contained and reaped via subreaper tracking")

        # 6.18: Shell profile persistence isolation (--noprofile --norc)
        # Attempting to persist arbitrary code via ~/.bash_profile or ~/.bashrc is ignored by fresh shell
        setup_profile = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "echo 'export PERSISTED_VAR=compromised' > /sandbox/.bash_profile && echo 'export PERSISTED_VAR=compromised' > /sandbox/.bashrc"},
            timeout=TIMEOUT,
        )
        check_profile = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params=params,
            json={"command": "printf '%s' \"$PERSISTED_VAR\""},
            timeout=TIMEOUT,
        )
        profile_data = check_profile.json() if check_profile.status_code == 200 else {}
        passed_profile_isolation = (
            setup_profile.status_code == 200
            and check_profile.status_code == 200
            and profile_data.get("stdout") == ""
        )
        runner.record("6.18", passed_profile_isolation, "Bash startup files (.bash_profile, .bashrc) ignored via --noprofile --norc")


# =============================================================================
# Phase 7: Session Release, Virtual Disk Teardown & Worker Recycling
# =============================================================================

async def test_phase_7(runner: TestRunner, token: str, test_uuid: str, worker_name: str):
    print("\n" + "=" * 70)
    print("PHASE 7: Session Release, Virtual Disk Teardown & Worker Recycling")
    print("=" * 70)

    headers = {"X-Auth-Token": token}
    async with httpx.AsyncClient(headers=headers) as client:
        # 7.1: Call release endpoint
        rel_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/release",
            params={"user_uuid": test_uuid},
            timeout=TIMEOUT,
        )
        passed_rel = rel_resp.status_code == 204
        runner.record("7.1", passed_rel, f"Session released successfully (HTTP {rel_resp.status_code})")

    # Allow gateway cleanup to complete
    await asyncio.sleep(2.5)

    # 7.2: Verify the released worker container was destroyed
    inspect_res = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={worker_name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    passed_destroyed = worker_name not in inspect_res.stdout.strip().splitlines()
    runner.record("7.2", passed_destroyed, f"Worker container {worker_name} destroyed on release")

    # 7.3: Verify idle worker pool was replenished
    async with httpx.AsyncClient(headers=headers) as client:
        status = (await client.get(f"{GATEWAY_URL}/api/v1/status", timeout=TIMEOUT)).json()
        total = status.get("total_workers", 0)
        busy = status.get("busy_workers", 0)
        idle_count = total - busy
        passed_replenished = idle_count >= 1
        runner.record("7.3", passed_replenished, f"Idle worker pool replenished (idle={idle_count})")

    # 7.4: Verify released sandbox filesystem destroyed (previous files absent in fresh session)
    new_uuid = str(uuid.uuid4())
    async with httpx.AsyncClient(headers=headers) as client:
        check_resp = await client.post(
            f"{GATEWAY_URL}/api/v1/shell/exec",
            params={"user_uuid": new_uuid},
            json={"command": "test -e /sandbox/state.txt"},
            timeout=TIMEOUT,
        )
        check_data = check_resp.json() if check_resp.status_code == 200 else {}
        passed_clean = (
            check_resp.status_code == 200
            and check_data.get("exit_code") != 0
        )
        runner.record("7.4", passed_clean, "Released sandbox filesystem destroyed (previous session files absent)")

        # Clean up the verification session
        await client.post(
            f"{GATEWAY_URL}/api/v1/release",
            params={"user_uuid": new_uuid},
            timeout=TIMEOUT,
        )


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
        await test_phase_7(runner, token, test_uuid, worker_name)
    else:
        print("\nSkipping Phases 4-7 due to worker allocation failure.")

    # Summary
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)
    for test_id, passed in runner.results.items():
        print(f"  Test {test_id}: {'PASS' if passed else 'FAIL'}")

    total = len(runner.results)
    passed_count = sum(1 for p in runner.results.values() if p)
    print(f"\nTotal: {passed_count}/{total} tests passed")
    print("=" * 70)

    if passed_count != total:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
