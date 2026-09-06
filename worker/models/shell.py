"""
Shell executor and models for Worker service.

Executes arbitrary shell commands inside the worker container with:
- Non-root 'sandbox' user (inherited from Worker process)
- Strict cwd confinement to /sandbox (rejects traversal and symlink escapes)
- Minimal clean environment (no leaked secrets or host env)
- Invocation via /bin/bash --noprofile --norc -c (no user startup file persistence)
- PR_SET_CHILD_SUBREAPER + full process tree containment (cleans up background jobs & setsid)
- Process-group isolation and termination on timeout (SIGTERM -> grace -> SIGKILL)
- Bounded streaming output capture (capped memory with discard drain)
- Serialized execution per Worker (asyncio.Lock)
"""
import asyncio
import ctypes
import os
import signal
import time
from pathlib import Path
from typing import ClassVar

from loguru import logger as l
from pydantic import Field, field_validator

from .base import ModelBase

# Set PR_SET_CHILD_SUBREAPER so orphaned descendants reparent to this worker process
PR_SET_CHILD_SUBREAPER = 36
try:
    if hasattr(ctypes, "CDLL"):
        libc = ctypes.CDLL(None)
        if hasattr(libc, "prctl"):
            ret = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
            if ret == 0:
                l.debug("Worker process registered as child subreaper (PR_SET_CHILD_SUBREAPER)")
except Exception as e:
    l.warning(f"Failed to set PR_SET_CHILD_SUBREAPER: {e}")


class ShellExecRequest(ModelBase):
    """Request to execute a shell command."""
    command: str = Field(
        ...,
        min_length=1,
        max_length=65536,
        description="Shell command to execute",
    )
    cwd: str = Field(
        default="/sandbox",
        max_length=1024,
        description="Working directory (must resolve within /sandbox)",
    )
    timeout: float = Field(
        default=30.0,
        gt=0.0,
        le=120.0,
        description="Command timeout in seconds (0 < timeout <= 120)",
    )

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("command must not be empty or whitespace only")
        return v


class ShellExecResponse(ModelBase):
    """Response from shell command execution."""
    stdout: str = Field(default="", description="Captured standard output")
    stderr: str = Field(default="", description="Captured standard error")
    exit_code: int | None = Field(
        default=None,
        description="Process exit code, or null if timed out",
    )
    timed_out: bool = Field(
        default=False,
        description="True if command timed out and process tree was terminated",
    )
    stdout_truncated: bool = Field(
        default=False,
        description="True if stdout exceeded maximum output limit",
    )
    stderr_truncated: bool = Field(
        default=False,
        description="True if stderr exceeded maximum output limit",
    )
    duration_ms: int = Field(
        default=0,
        description="Execution duration in milliseconds",
    )


class BoundedStreamDrainer:
    """
    Asynchronously reads a stream up to max_bytes.
    Once max_bytes is reached, continues draining and discarding chunks
    to avoid blocking the subprocess pipe while keeping memory strictly bounded.
    """
    def __init__(
        self,
        stream: asyncio.StreamReader,
        max_bytes: int = 1024 * 1024,
        chunk_size: int = 65536,
    ):
        self.stream = stream
        self.max_bytes = max_bytes
        self.chunk_size = chunk_size
        self.captured = bytearray()
        self.truncated = False

    async def run(self) -> None:
        try:
            while True:
                chunk = await self.stream.read(self.chunk_size)
                if not chunk:
                    break
                if not self.truncated:
                    remaining = self.max_bytes - len(self.captured)
                    if len(chunk) <= remaining:
                        self.captured.extend(chunk)
                    else:
                        self.captured.extend(chunk[:remaining])
                        self.truncated = True
        except asyncio.CancelledError:
            pass
        except Exception as e:
            l.debug(f"Stream drainer read exception (expected on termination): {e}")

    def get_result(self) -> tuple[str, bool]:
        return self.captured.decode("utf-8", errors="replace"), self.truncated


class ShellExecutor:
    """
    Executes shell commands inside the worker container.
    """
    DEFAULT_SANDBOX_ROOT: Path = Path("/sandbox")
    DEFAULT_MAX_OUTPUT_BYTES: int = 1024 * 1024  # 1 MiB

    CLEAN_ENV: ClassVar[dict[str, str]] = {
        "HOME": "/sandbox",
        "USER": "sandbox",
        "LOGNAME": "sandbox",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
    }

    _lock: asyncio.Lock

    def __init__(
        self,
        sandbox_root: Path = DEFAULT_SANDBOX_ROOT,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ):
        self.sandbox_root = sandbox_root
        self.max_output_bytes = max_output_bytes
        self._lock = asyncio.Lock()

    def validate_cwd(self, cwd: str | None) -> Path:
        """
        Validates that cwd resolves strictly within sandbox_root.
        Prevents path traversal and symlink escape.
        """
        if not cwd or not cwd.strip():
            cwd = str(self.sandbox_root)

        root = self.sandbox_root.resolve()
        p = Path(cwd)
        if not p.is_absolute():
            p = root / p

        try:
            resolved = p.resolve(strict=True)
        except FileNotFoundError:
            raise ValueError(f"cwd directory does not exist: {cwd}")
        except (NotADirectoryError, RuntimeError) as e:
            raise ValueError(f"invalid cwd path: {e}")

        if not resolved.is_dir():
            raise ValueError(f"cwd is not a directory: {cwd}")

        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValueError(f"cwd '{cwd}' resolves to '{resolved}' which escapes sandbox root '{root}'")

        return resolved

    @staticmethod
    def _get_sandbox_pids() -> set[int]:
        """
        Scans /proc for all process IDs belonging to the sandbox user (UID 1000).
        """
        pids = set()
        try:
            for entry in os.listdir("/proc"):
                if entry.isdigit():
                    pid = int(entry)
                    try:
                        with open(f"/proc/{pid}/status", "r") as f:
                            for line in f:
                                if line.startswith("Uid:"):
                                    uids = line.split()[1:]
                                    if any(u == "1000" for u in uids):
                                        pids.add(pid)
                                    break
                    except (FileNotFoundError, ProcessLookupError, PermissionError):
                        pass
        except Exception as e:
            l.debug(f"Error scanning /proc: {e}")
        return pids

    async def _cleanup_descendants(self, pgid: int, baseline_pids: set[int]) -> None:
        """
        Terminates and reaps all descendant processes spawned during command execution.
        Guarantees that background jobs (&), orphan children, and setsid() processes
        cannot outlive the execution request.
        """
        # 1. Kill the process group first
        if hasattr(os, "killpg"):
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            try:
                os.kill(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        # 2. Check for any sandbox process that was not in baseline
        current_pids = self._get_sandbox_pids()
        orphan_pids = current_pids - baseline_pids

        if orphan_pids:
            for pid in orphan_pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

            # Brief grace period for cleanup
            await asyncio.sleep(0.15)

            # Force kill any still surviving processes
            still_alive = self._get_sandbox_pids() - baseline_pids
            if hasattr(os, "killpg"):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

            for pid in still_alive:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        # 3. Reap all reparented zombie children via waitpid loop (subreaper)
        for _ in range(5):
            reaped_any = False
            while True:
                try:
                    rpid, _ = os.waitpid(-1, os.WNOHANG)
                    if rpid <= 0:
                        break
                    reaped_any = True
                except ChildProcessError:
                    break
            if not reaped_any:
                break
            await asyncio.sleep(0.05)

    async def execute(self, request: ShellExecRequest) -> ShellExecResponse:
        """
        Executes request.command under serialized lock.
        """
        async with self._lock:
            resolved_cwd = self.validate_cwd(request.cwd)
            start_time = time.perf_counter()

            # Record baseline PIDs before launching command
            baseline_pids = self._get_sandbox_pids()

            # Launch /bin/bash --noprofile --norc -c with clean minimal environment and new session
            # --noprofile --norc ensures user-writable /sandbox/.bash_profile or .bashrc are NEVER loaded
            process = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "--noprofile",
                "--norc",
                "-c",
                request.command,
                cwd=str(resolved_cwd),
                env=self.CLEAN_ENV,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

            pgid = process.pid
            stdout_drainer = BoundedStreamDrainer(process.stdout, max_bytes=self.max_output_bytes)
            stderr_drainer = BoundedStreamDrainer(process.stderr, max_bytes=self.max_output_bytes)
            stdout_task = asyncio.create_task(stdout_drainer.run())
            stderr_task = asyncio.create_task(stderr_drainer.run())

            timed_out = False
            exit_code: int | None = None

            try:
                # Wait for main process to complete within timeout
                exit_code = await asyncio.wait_for(process.wait(), timeout=request.timeout)

                # Process exited. Drain remaining stream buffers with a short grace timeout
                elapsed = time.perf_counter() - start_time
                drain_timeout = max(0.1, min(1.0, request.timeout - elapsed))
                try:
                    await asyncio.wait_for(
                        asyncio.gather(stdout_task, stderr_task),
                        timeout=drain_timeout,
                    )
                except asyncio.TimeoutError:
                    stdout_task.cancel()
                    stderr_task.cancel()
                    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

            except asyncio.TimeoutError:
                timed_out = True
                exit_code = None

                # Cancel and finish stream drainers
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

            finally:
                # Always clean up all descendant processes (orphans, background jobs &, setsid)
                await self._cleanup_descendants(pgid, baseline_pids)

                # Ensure main process is reaped
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except Exception:
                    pass

            end_time = time.perf_counter()
            duration_ms = int((end_time - start_time) * 1000)

            stdout_str, stdout_truncated = stdout_drainer.get_result()
            stderr_str, stderr_truncated = stderr_drainer.get_result()

            return ShellExecResponse(
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=exit_code,
                timed_out=timed_out,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                duration_ms=duration_ms,
            )


default_shell_executor = ShellExecutor()
