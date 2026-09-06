"""
/shell/exec endpoint for Gateway service.
Proxies arbitrary shell execution to the assigned Worker container.
"""
from loguru import logger as l

from gateway.fastapis.deps import WorkerDep
from gateway.fastapis.tagged_api_router import TaggedAPIRouter
from gateway.models.shell import ShellExecRequest, ShellExecResponse

router = TaggedAPIRouter(prefix="/exec", tag="Shell Execution")


@router.post("", response_model=ShellExecResponse)
async def shell_exec(request: ShellExecRequest, worker: WorkerDep) -> ShellExecResponse:
    """
    Executes an arbitrary shell command within the user's isolated sandbox worker.

    **Authentication**:
    - Requires `X-Auth-Token` header.
    - Requires `user_uuid` query parameter to allocate or reuse the worker session.

    **Security Invariants**:
    - Gateway NEVER executes user commands locally or via docker exec.
    - Commands run strictly inside the worker container under non-root 'sandbox' user.
    - Cwd is strictly confined within /sandbox.
    - Process tree is terminated upon timeout.
    """
    l.debug(f"Gateway shell exec request for worker {worker.container_name}: command={request.command!r}")
    response = await worker.shell_exec(request)
    l.debug(
        f"Gateway shell exec completed on worker {worker.container_name}: "
        f"exit_code={response.exit_code}, timed_out={response.timed_out}, duration_ms={response.duration_ms}"
    )
    return response
