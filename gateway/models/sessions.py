"""
Session domain models for ActionsCat and multi-profile sandbox execution.
"""
from typing import Any
from uuid import UUID

from pydantic import Field

from .base import ModelBase


class NetworkAllowRule(ModelBase):
    """Structured network allow rule matching ActionsCat domain.NetworkAllowRule."""
    host: str
    port: int | None = None
    proto: str | None = None


class SessionInitRequest(ModelBase):
    """Request payload for POST /api/v1/sessions."""
    user_uuid: UUID
    profile: str | None = None
    network: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    network_rules: list[NetworkAllowRule] = Field(default_factory=list)
    memory_limit_mb: int | None = None
    cpu_limit: float | None = None
    env: dict[str, str] = Field(default_factory=dict)
    runtime_callback_url: str | None = None


class SessionInitResponse(ModelBase):
    """Response payload for POST /api/v1/sessions."""
    user_uuid: UUID
    profile: str
    network: str
    status: str = "ready"
    allowed_hosts: list[str] = Field(default_factory=list)
    runtime_callback_url: str | None = None
