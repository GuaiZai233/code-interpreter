"""
/api/v1 routes aggregation for Worker service.
"""
from worker.fastapis.tagged_api_router import TaggedAPIRouter

from .kernel import router as kernel_router
from .shell import router as shell_router

router = TaggedAPIRouter(prefix="/v1")
router.include_router(kernel_router)
router.include_router(shell_router)
