"""
/reset endpoint.
"""
from worker.fastapis.tagged_api_router import TaggedAPIRouter
from worker.utils.http_exceptions import raise_not_implemented

router = TaggedAPIRouter(prefix="/reset", tag="Reset kernel")


@router.post("")
async def reset_kernel() -> None:
    raise_not_implemented("Kernel reset is not supported in this minimal sandbox runtime.")

