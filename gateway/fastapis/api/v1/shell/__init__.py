"""
/shell routes aggregation for Gateway service.
"""
from gateway.fastapis.tagged_api_router import TaggedAPIRouter

from .exec import router as exec_router

router = TaggedAPIRouter(prefix="/shell", tag="Shell")
router.include_router(exec_router)
