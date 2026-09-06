"""
/reset endpoint.
"""
from starlette.status import HTTP_204_NO_CONTENT

from worker.fastapis.tagged_api_router import TaggedAPIRouter

router = TaggedAPIRouter(prefix="/reset", tag="Reset kernel")


@router.post("", status_code=HTTP_204_NO_CONTENT)
async def reset_kernel() -> None:
    return None
