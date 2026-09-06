"""
/execute endpoint.
"""
from loguru import logger as l

from gateway.fastapis.tagged_api_router import TaggedAPIRouter
from gateway.models.execute import ExecuteRequest, ExecuteResponse
from gateway.utils.http_exceptions import raise_not_implemented

router = TaggedAPIRouter(prefix="/execute", tag="Execute code")


@router.post("", response_model=ExecuteResponse)
async def execute(request: ExecuteRequest, user_uuid: str | None = None) -> ExecuteResponse:
    """
    Execute endpoint is disabled in this minimal sandbox runtime.

    Returns HTTP 501 Not Implemented without allocating or binding any worker.
    """
    l.debug(f"Execute request rejected: code execution is disabled (user_uuid={user_uuid})")
    raise_not_implemented("Python code execution is disabled in this minimal sandbox runtime.")
