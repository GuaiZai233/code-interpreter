"""
Python Code Interpreter Worker application entry point.
Authentication is handled by the Gateway. This service is not exposed publicly.
"""
import os
from fastapi import FastAPI, Request
from loguru import logger as l
from starlette.responses import JSONResponse

from worker.fastapis import router
from worker.utils.http_exceptions import raise_internal_error

app = FastAPI(title="Python Code Interpreter Worker")


@app.middleware("http")
async def verify_gateway_source(request: Request, call_next):
    """
    Ensures that Worker control endpoints are only called by the Gateway.
    Prevents sandbox commands executing inside the container from calling
    the Worker's own control endpoints over loopback or local network.
    """
    gateway_ip = os.environ.get("GATEWAY_INTERNAL_IP")
    client_host = request.client.host if request.client else None
    if gateway_ip and client_host != gateway_ip:
        l.warning(
            f"Blocked unauthorized internal call to Worker control API: "
            f"client_ip={client_host} (expected Gateway {gateway_ip}), path={request.url.path}"
        )
        return JSONResponse(
            status_code=403,
            content={"detail": "Forbidden: Worker control APIs are accessible exclusively from Gateway."},
        )
    return await call_next(request)


@app.exception_handler(Exception)
async def handle_unexpected_exceptions(request: Request, exc: Exception):
    l.exception(f"Unhandled exception for request: {request.method} {request.url.path}")
    raise_internal_error()


app.include_router(router)
