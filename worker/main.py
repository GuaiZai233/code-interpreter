"""
Python Code Interpreter Worker application entry point.
Authentication is handled by the Gateway. This service is not exposed publicly.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from loguru import logger as l
from starlette.responses import JSONResponse

from worker.fastapis import router
from worker.models import JupyterKernel
from worker.utils.aiohttp_client_session_mixin import AioHttpClientSessionClassVarMixin
from worker.utils.http_exceptions import raise_internal_error


@asynccontextmanager
async def lifespan(app: FastAPI):
    l.info("Worker is starting up...")
    await AioHttpClientSessionClassVarMixin.initialize_http_session()
    await JupyterKernel.start()
    yield
    l.info("Worker is shutting down...")
    await AioHttpClientSessionClassVarMixin.close_http_session()


app = FastAPI(title="Python Code Interpreter Worker", lifespan=lifespan)


@app.exception_handler(Exception)
async def handle_unexpected_exceptions(request: Request, exc: Exception) -> JSONResponse:
    """
    Starlette exception handlers must return Response, not raise.

    Inner handler uses raise_internal_error() for consistency; outer wrapper
    catches HTTPException and converts to JSONResponse (same pattern as foxline backend).
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
