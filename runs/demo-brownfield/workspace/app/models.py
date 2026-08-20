"""Typed request and response models validated at the service boundary.

Request models run in pydantic strict mode with ``extra="forbid"``: invalid
input is rejected rather than coerced, and every string carries a length limit
before it can reach the database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .config import ABSOLUTE_MAX_TTL_SECONDS, ABSOLUTE_MAX_URL_LENGTH


class CreateLinkRequest(BaseModel):
    """Payload accepted by ``POST /api/links``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    target_url: str = Field(min_length=1, max_length=ABSOLUTE_MAX_URL_LENGTH)
    expires_in_seconds: Optional[int] = Field(default=None, ge=0, le=ABSOLUTE_MAX_TTL_SECONDS)


class CreateLinkResponse(BaseModel):
    """Body returned by ``POST /api/links``."""

    code: str
    short_url: str
    target_url: str
    created_at: datetime
    expires_at: Optional[datetime] = None


class StatsResponse(BaseModel):
    """Body returned by ``GET /api/links/{code}/stats``."""

    code: str
    short_url: str
    target_url: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    expired: bool
    clicks: int
    last_clicked_at: Optional[datetime] = None


class HealthResponse(BaseModel):
    """Body returned by ``GET /health``."""

    status: str
