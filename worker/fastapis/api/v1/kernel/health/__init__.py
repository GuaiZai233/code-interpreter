"""
/health endpoint.
"""
from worker.fastapis.tagged_api_router import TaggedAPIRouter
from worker.models import HealthResponse

router = TaggedAPIRouter(prefix="/health", tag="Health check")


@router.get("", response_model=HealthResponse)
async def get_health_status() -> HealthResponse:
    return HealthResponse(status="ok")
