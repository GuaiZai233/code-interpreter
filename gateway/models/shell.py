"""
Shell execution models for Gateway service.
"""
from pydantic import Field, field_validator

from .base import ModelBase


class ShellExecRequest(ModelBase):
    """Request to execute a shell command."""
    command: str = Field(
        ...,
        min_length=1,
        max_length=65536,
        description="Shell command to execute",
    )
    cwd: str = Field(
        default="/sandbox",
        max_length=1024,
        description="Working directory (must resolve within /sandbox)",
    )
    timeout: float = Field(
        default=30.0,
        gt=0.0,
        le=120.0,
        description="Command timeout in seconds (0 < timeout <= 120)",
    )

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("command must not be empty or whitespace only")
        return v


class ShellExecResponse(ModelBase):
    """Response from shell command execution."""
    stdout: str = Field(default="", description="Captured standard output")
    stderr: str = Field(default="", description="Captured standard error")
    exit_code: int | None = Field(
        default=None,
        description="Process exit code, or null if timed out",
    )
    timed_out: bool = Field(
        default=False,
        description="True if command timed out and process tree was terminated",
    )
    stdout_truncated: bool = Field(
        default=False,
        description="True if stdout exceeded maximum output limit",
    )
    stderr_truncated: bool = Field(
        default=False,
        description="True if stderr exceeded maximum output limit",
    )
    duration_ms: int = Field(
        default=0,
        description="Execution duration in milliseconds",
    )
