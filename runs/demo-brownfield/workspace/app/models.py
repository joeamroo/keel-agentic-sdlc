"""Typed request and response models validated at the HTTP boundary.

Request models run in pydantic strict mode with ``extra='forbid'`` so invalid
input is rejected rather than coerced, and every string that can reach the
database carries a length limit.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

MAX_URL_LENGTH = 2048


class CreateLinkRequest(BaseModel):
    """Body of ``POST /api/links``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    url: StrictStr = Field(min_length=1, max_length=MAX_URL_LENGTH)
    expires_in_seconds: Optional[StrictInt] = Field(default=None, ge=1)


class LinkCreatedResponse(BaseModel):
    """Successful ``POST /api/links`` response."""

    code: str
    short_url: str
    url: str
    created_at: str
    expires_at: Optional[str] = None


class LinkStatsResponse(BaseModel):
    """Successful ``GET /api/links/{code}/stats`` response."""

    code: str
    url: str
    created_at: str
    expires_at: Optional[str] = None
    expired: bool = False
    clicks: int = 0
    last_clicked_at: Optional[str] = None


class HealthResponse(BaseModel):
    """Successful ``GET /health`` response."""

    status: str
    time: str


class ErrorBody(BaseModel):
    """Inner object of the error envelope."""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """The single error shape returned by every failing request."""

    error: ErrorBody
