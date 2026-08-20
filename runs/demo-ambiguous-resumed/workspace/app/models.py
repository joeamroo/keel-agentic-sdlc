"""Typed request and response models validated at the HTTP boundary."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

# Hard boundary cap on the accepted body string. The configured
# LINKS_MAX_URL_LENGTH is enforced separately so that an over long URL yields the
# machine readable ``url_too_long`` code rather than a generic body rejection.
MAX_ACCEPTED_URL_CHARS = 65536


class CreateLinkRequest(BaseModel):
    """Body of ``POST /links``.

    Unknown fields are rejected rather than ignored, and values are validated in
    strict mode so that e.g. ``"7"`` or ``true`` are not coerced into an integer.
    """

    model_config = ConfigDict(extra="forbid")

    url: StrictStr = Field(
        ...,
        min_length=1,
        max_length=MAX_ACCEPTED_URL_CHARS,
        description="Absolute http or https destination URL to shorten.",
    )
    expires_in_days: Optional[StrictInt] = Field(
        default=None,
        description="Lifetime in whole days; omitted means the configured default.",
    )


class CreateLinkResponse(BaseModel):
    """Body returned by a successful ``POST /links``."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="The issued base62 short code.")
    short_url: str = Field(..., description="Full short URL built from the public base URL.")
    destination: str = Field(..., description="The normalized destination that will be served.")
    created_at: str = Field(..., description="ISO-8601 UTC creation timestamp.")
    expires_at: str = Field(..., description="ISO-8601 UTC expiry timestamp.")


class HealthResponse(BaseModel):
    """Body returned by the liveness endpoint."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(..., description="Always 'ok' when the process is live.")
    service: str = Field(..., description="Service identifier.")
    time: str = Field(..., description="Current ISO-8601 UTC time.")
