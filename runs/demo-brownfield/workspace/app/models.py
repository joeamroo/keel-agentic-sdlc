"""Typed request and response models validated at the HTTP boundary.

The request model runs in pydantic strict mode: values of the wrong type are
rejected rather than coerced, and unknown fields are refused.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# Absolute ceiling for the url field. The per-deployment limit
# (LINKS_MAX_URL_LENGTH) is applied on top of this in the route and can only be
# smaller, so nothing longer than this ever reaches the database.
MAX_URL_FIELD_LENGTH = 8192
MAX_TTL_FIELD_VALUE = 1_000_000_000


class CreateLinkRequest(BaseModel):
    """Body of ``POST /api/links``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    url: str = Field(
        ...,
        min_length=1,
        max_length=MAX_URL_FIELD_LENGTH,
        description="Absolute http or https target URL.",
    )
    ttl_seconds: Optional[int] = Field(
        default=None,
        ge=0,
        le=MAX_TTL_FIELD_VALUE,
        description="Lifetime in seconds. 0 or omitted uses the configured default.",
    )


class CreateLinkResponse(BaseModel):
    """Body of a successful ``POST /api/links``."""

    code: str
    short_url: str
    target_url: str
    created_at: str
    expires_at: Optional[str] = None


class StatsResponse(BaseModel):
    """Body of ``GET /api/links/{code}/stats``."""

    code: str
    target_url: str
    created_at: str
    expires_at: Optional[str] = None
    click_count: int
    last_clicked_at: Optional[str] = None


class HealthResponse(BaseModel):
    """Body of ``GET /health``."""

    status: str
    service: str
    time: str
