"""
/execute endpoint.
"""
from loguru import logger as l

from worker.fastapis.tagged_api_router import TaggedAPIRouter
from worker.models import ExecuteRequest, ExecuteResponse, ExecutionResultType, ExecutionStatus, JupyterKernel
from worker.utils.http_exceptions import raise_bad_request, raise_service_unavailable

router = TaggedAPIRouter(prefix="/execute", tag="Execute code")


@router.post("", response_model=ExecuteResponse)
async def execute_code(request: ExecuteRequest) -> ExecuteResponse:
    code = request.code
    head = code[:80].replace('\n', '\\n')
    tail = code[-80:].replace('\n', '\\n') if len(code) > 160 else ""
    l.debug(f"Execute request: {len(code)} bytes, head=[{head}], tail=[{tail}]")
    result = await JupyterKernel.execute_code(request.code)
    l.debug(f"Execution result: status={result.status}, type={result.type}")

    match result.status:
        case ExecutionStatus.OK:
            return ExecuteResponse(
                result_base64=result.value if result.type == ExecutionResultType.IMAGE_PNG_BASE64 else None,
                result_text=result.value if result.type != ExecutionResultType.IMAGE_PNG_BASE64 else None,
            )
        case ExecutionStatus.TIMEOUT:
            l.error("FATAL: Code execution timed out. This worker instance is now considered unhealthy.")
            raise_service_unavailable("Code execution timed out. This worker instance is now considered unhealthy and should be killed.")
        case ExecutionStatus.KERNEL_ERROR:
            l.error("FATAL: Kernel dead. This worker instance is now considered unhealthy.")
            raise_service_unavailable("Code execution environment dead. This worker instance is now considered unhealthy and should be killed.")
        case ExecutionStatus.ERROR:
            match result.type:
                case ExecutionResultType.CONNECTION_ERROR:
                    l.error("FATAL: Kernel dead. This worker instance is now considered unhealthy.")
                    raise_service_unavailable("Code execution environment dead. This worker instance is now considered unhealthy and should be killed.")
                case _:
                    preview = result.value[:500] if result.value else ""
                    l.warning(f"Python execution failed. Type: {result.type}, Message: {preview}")
                    raise_bad_request(f"Python Execution Error: {result.value}")
