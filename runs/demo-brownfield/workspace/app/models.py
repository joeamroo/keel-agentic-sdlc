"""Typed request and response models validated at the HTTP boundary."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated

from .config import ABSOLUTE_MAX_URL_LENGTH, MAX_EXPIRES_AT_LENGTH

UrlStr = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=ABSOLUTE_MAX_URL_LENGTH),
]

ExpiryStr = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=MAX_EXPIRES_AT_LENGTH),
]


class CreateLinkRequest(BaseModel):
    """Body of ``POST /api/links``."""

    model_config = ConfigDict(extra="forbid")

    url: UrlStr = Field(
        ...,
        description="Public http(s) destination URL to shorten.",
    )
    expires_at: Optional[ExpiryStr] = Field(
        default=None,
        description="Optional ISO-8601 expiry instant; must be in the future.",
    )


class CreateLinkResponse(BaseModel):
    """Body returned by a successful ``POST /api/links``."""

    code: str
    short_url: str
    url: str
    created_at: str
    expires_at: str


class ClickEntry(BaseModel):
    """A single recorded redirect, free of any client address."""

    timestamp: str
    referrer: Optional[str] = None
    user_agent: Optional[str] = None


class StatsResponse(BaseModel):
    """Body returned by ``GET /api/links/{code}/stats``."""

    code: str
    url: str
    created_at: str
    expires_at: str
    total_clicks: int
    clicks: List[ClickEntry]


class HealthResponse(BaseModel):
    """Body returned by ``GET /health``."""

    status: str
