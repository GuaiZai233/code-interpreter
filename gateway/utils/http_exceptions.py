"""
通用 HTTP 异常辅助函数

本文件保持跨项目通用，不包含业务特定的描述文案（如 "AI 服务"）。
调用方在具体业务场景中传入 detail 消息。

同步自 foxline-pro-backend-server/utils/http_exceptions.py
"""
from typing import Any, NoReturn

from fastapi import HTTPException
from pydantic import BaseModel

from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_413_CONTENT_TOO_LARGE,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_501_NOT_IMPLEMENTED,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_504_GATEWAY_TIMEOUT,
)

# --- 400 ---

def ensure_request_param(to_check: Any, detail: str) -> None:
    """
    Ensures a parameter exists. If not, raises a 400 Bad Request.
    This function returns None if the check passes.
    """
    if not to_check:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=detail)

def raise_bad_request(detail: str = '') -> NoReturn:
    """Raises an HTTP 400 Bad Request exception."""
    raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=detail)


def reject_forbidden_fields(data: BaseModel, forbidden: type[BaseModel]) -> None:
    """
    显式拒绝请求体中来自 ``forbidden`` 模型的字段（权限边界守卫）。

    基于 Pydantic v2 的 ``model_fields_set`` 精确区分"未提供"和"提供了但不该改"。

    :param data: 待检查的请求体实例
    :param forbidden: 声明禁止字段的模型类
    :raises HTTPException 400: data 的 ``model_fields_set`` 与
        ``forbidden.model_fields`` 存在交集
    """
    violated = data.model_fields_set & set(forbidden.model_fields)
    if violated:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"The following fields cannot be modified: {', '.join(sorted(violated))}"
        )

def raise_unauthorized(detail: str) -> NoReturn:
    """Raises an HTTP 401 Unauthorized exception."""
    raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail=detail)

def raise_insufficient_quota(detail: str = "Insufficient quota") -> NoReturn:
    """Raises an HTTP 402 Payment Required exception."""
    raise HTTPException(status_code=HTTP_402_PAYMENT_REQUIRED, detail=detail)

def raise_forbidden(detail: str) -> NoReturn:
    """Raises an HTTP 403 Forbidden exception."""
    raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail=detail)

def raise_not_found(detail: str) -> NoReturn:
    """Raises an HTTP 404 Not Found exception."""
    raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail=detail)

def raise_conflict(detail: str) -> NoReturn:
    """Raises an HTTP 409 Conflict exception."""
    raise HTTPException(status_code=HTTP_409_CONFLICT, detail=detail)

def raise_payload_too_large(detail: str = "Request payload is too large") -> NoReturn:
    """Raises an HTTP 413 Payload Too Large exception."""
    raise HTTPException(status_code=HTTP_413_CONTENT_TOO_LARGE, detail=detail)

def raise_unprocessable_entity(detail: str) -> NoReturn:
    """Raises an HTTP 422 Unprocessable Entity exception."""
    raise HTTPException(status_code=HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)

def raise_too_many_requests(detail: str, retry_after: int | None = None) -> NoReturn:
    """Raises an HTTP 429 Too Many Requests exception.

    :param detail: 错误描述
    :param retry_after: 建议客户端等待的秒数（Retry-After 响应头，RFC 6585）
    """
    headers = {'Retry-After': str(retry_after)} if retry_after is not None else None
    raise HTTPException(status_code=HTTP_429_TOO_MANY_REQUESTS, detail=detail, headers=headers)

HTTP_529_SITE_OVERLOADED = 529
"""非标准状态码（Cloudflare 约定）：上游服务过载/限流"""

def raise_upstream_rate_limit(detail: str) -> NoReturn:
    """Raises an HTTP 529 (upstream rate-limited/overloaded) exception."""
    raise HTTPException(status_code=HTTP_529_SITE_OVERLOADED, detail=detail)

# --- 500 ---

def raise_internal_error(detail: str = "Internal server error") -> NoReturn:
    """Raises an HTTP 500 Internal Server Error exception."""
    raise HTTPException(status_code=HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)

def raise_bad_gateway(detail: str) -> NoReturn:
    """Raises an HTTP 502 Bad Gateway exception."""
    raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=detail)

def raise_not_implemented(detail: str = "Not yet supported") -> NoReturn:
    """Raises an HTTP 501 Not Implemented exception."""
    raise HTTPException(status_code=HTTP_501_NOT_IMPLEMENTED, detail=detail)

def raise_service_unavailable(detail: str) -> NoReturn:
    """Raises an HTTP 503 Service Unavailable exception."""
    raise HTTPException(status_code=HTTP_503_SERVICE_UNAVAILABLE, detail=detail)

def raise_gateway_timeout(detail: str) -> NoReturn:
    """Raises an HTTP 504 Gateway Timeout exception."""
    raise HTTPException(status_code=HTTP_504_GATEWAY_TIMEOUT, detail=detail)
