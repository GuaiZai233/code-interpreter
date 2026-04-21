"""
Code Interpreter Gateway application entry point.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger as l
from starlette.responses import JSONResponse

from gateway import meta_config
from gateway.fastapis import router
from gateway.models.worker import WorkerPool
from gateway.models.backend_service import BackendService
from gateway.utils.aiohttp_client_session_mixin import AioHttpClientSessionClassVarMixin
from gateway.utils.http_exceptions import raise_internal_error
from gateway.utils.logger import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    await setup_logging(
        log_level=meta_config.LOG_LEVEL,
        log_file_path=Path(meta_config.LOG_FILE_PATH),
    )
    l.info("Starting Code Interpreter Gateway")
    l.info(f"Auth token configured: {'yes' if meta_config.AUTH_TOKEN else 'no (auto-generated)'}")

    await AioHttpClientSessionClassVarMixin.initialize_http_session()
    await WorkerPool.init()
    recycling_task = asyncio.create_task(WorkerPool.recycle_timed_out_workers())

    yield

    recycling_task.cancel()
    l.info("Shutting down. Cleaning up all worker containers...")
    await BackendService.shutdown()
    await WorkerPool.close()
    await AioHttpClientSessionClassVarMixin.close_http_session()


app = FastAPI(title="Code Interpreter Gateway", lifespan=lifespan)

# SECURITY DESIGN: CORS configured via environment variable CORS_ALLOWED_ORIGINS.
# Default '*' is intentional for development flexibility. Production deployments
# MUST set explicit origins. Rate limiting is handled at infrastructure level (nginx/cloudflare).
app.add_middleware(
    CORSMiddleware,
    allow_origins=meta_config.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def handle_unexpected_exceptions(request: Request, exc: Exception) -> JSONResponse:
    """
    全局兜底异常处理器。

    Starlette ExceptionMiddleware 要求处理器返回 Response，不能 raise HTTPException
    （否则异常穿透中间件链，CORS headers 等丢失）。参考 foxline 后端的
    _with_request_context 包装模式。
    """
    l.exception(f"Unhandled exception for request: {request.method} {request.url.path}")
    try:
        raise_internal_error()
    except HTTPException as http_exc:
        return JSONResponse(
            status_code=http_exc.status_code,
            content={"detail": http_exc.detail},
        )


app.include_router(router)
