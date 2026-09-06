"""
Python Code Interpreter Worker application entry point.
Authentication is handled by the Gateway. This service is not exposed publicly.
"""
from fastapi import FastAPI, Request
from loguru import logger as l

from worker.fastapis import router
from worker.utils.http_exceptions import raise_internal_error

app = FastAPI(title="Python Code Interpreter Worker")


@app.exception_handler(Exception)
async def handle_unexpected_exceptions(request: Request, exc: Exception):
    l.exception(f"Unhandled exception for request: {request.method} {request.url.path}")
    raise_internal_error()


app.include_router(router)
