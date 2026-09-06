"""
/execute endpoint.
"""
from loguru import logger as l

from worker.fastapis.tagged_api_router import TaggedAPIRouter
from worker.models import ExecuteRequest, ExecuteResponse
from worker.utils.http_exceptions import raise_not_implemented

router = TaggedAPIRouter(prefix="/execute", tag="Execute code")


@router.post("", response_model=ExecuteResponse)
async def execute_code(request: ExecuteRequest) -> ExecuteResponse:
    l.debug(f"Execute request: {request}")
    raise_not_implemented("Python code execution is disabled in this minimal sandbox runtime.")
