"""Typed request and response models used at the HTTP boundary.

Every string that reaches the database has an explicit length limit here, and the
models are strict: values of the wrong type are rejected rather than coerced.
"""

from __future__ import annotations

from typing import Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

MAX_URL_LENGTH: int = 2048
MAX_CODE_LENGTH: int = 32
MIN_CODE_LENGTH: int = 3
CODE_PATTERN: str = r"^[A-Za-z0-9_-]+$"
MAX_TTL_SECONDS: int = 31_536_000  # one year


class CreateLinkRequest(BaseModel):
    """Body of ``POST /api/links``.

    Attributes:
        url: Destination URL; validated for scheme, credentials and address safety
            before it is stored.
        expires_in_seconds: Optional lifetime in seconds; omit for a link that never
            expires.
        code: Optional custom short code.
    """

    model_config = ConfigDict(strict=True, populate_by_name=True, extra="ignore")

    url: str = Field(
        ...,
        min_length=1,
        max_length=MAX_URL_LENGTH,
        validation_alias=AliasChoices("url", "target_url", "long_url"),
        description="Destination URL (http or https only).",
    )
    expires_in_seconds: Optional[int] = Field(
        default=None,
        ge=1,
        le=MAX_TTL_SECONDS,
        validation_alias=AliasChoices("expires_in_seconds", "expires_in", "ttl_seconds"),
        description="Lifetime in seconds; omitted means the link never expires.",
    )
    code: Optional[str] = Field(
        default=None,
        min_length=MIN_CODE_LENGTH,
        max_length=MAX_CODE_LENGTH,
        pattern=CODE_PATTERN,
        validation_alias=AliasChoices("code", "custom_code", "alias"),
        description="Optional custom short code.",
    )


class LinkCreatedResponse(BaseModel):
    """Body of a successful ``POST /api/links``.

    Attributes:
        code: The allocated short code.
        short_url: Absolute short URL built from LINKS_BASE_URL.
        target_url: The stored, validated destination.
        created_at: RFC 3339 UTC creation timestamp.
        expires_at: RFC 3339 UTC expiry timestamp, or ``None``.
    """

    code: str
    short_url: str
    target_url: str
    created_at: str
    expires_at: Optional[str] = None


class LinkStatsResponse(BaseModel):
    """Body of ``GET /api/links/{code}/stats``.

    Attributes:
        code: The short code.
        target_url: The stored destination.
        created_at: RFC 3339 UTC creation timestamp.
        expires_at: RFC 3339 UTC expiry timestamp, or ``None``.
        expired: Whether the link has passed its expiry instant.
        click_count: Number of redirects served for this code.
        last_clicked_at: RFC 3339 UTC timestamp of the most recent click, if any.
    """

    code: str
    target_url: str
    created_at: str
    expires_at: Optional[str] = None
    expired: bool
    click_count: int
    last_clicked_at: Optional[str] = None


class HealthResponse(BaseModel):
    """Body of ``GET /health``.

    Attributes:
        status: Always ``"ok"`` when the process is serving requests.
    """

    status: str
