"""
/shell/exec endpoint for Worker service.
"""
from loguru import logger as l

from worker.fastapis.tagged_api_router import TaggedAPIRouter
from worker.models import ShellExecRequest, ShellExecResponse, default_shell_executor
from worker.utils.http_exceptions import raise_bad_request

router = TaggedAPIRouter(prefix="/exec", tag="Shell Execution")


@router.post("", response_model=ShellExecResponse)
async def shell_exec(request: ShellExecRequest) -> ShellExecResponse:
    """
    Executes a shell command inside the worker container.
    """
    l.debug(f"Worker shell exec request: command={request.command!r}, cwd={request.cwd}, timeout={request.timeout}")
    try:
        response = await default_shell_executor.execute(request)
        l.debug(
            f"Worker shell exec completed: exit_code={response.exit_code}, "
            f"timed_out={response.timed_out}, duration_ms={response.duration_ms}"
        )
        return response
    except ValueError as e:
        raise_bad_request(str(e))
