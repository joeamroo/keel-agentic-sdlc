"""Expiry. Time is controlled by writing the row, never by sleeping."""
from datetime import datetime, timedelta, timezone

import pytest

from _pipeline_support import (  # noqa: F401 - fixtures are imported by name
    assert_error_envelope,
    make_app,
    parse_ts,
    service,
    stub_dns,
)


def test_a_link_whose_expiry_lies_in_the_past_is_refused_with_410_and_no_location(service):
    code = service.create("https://example.com/gone").json()["code"]
    service.set_expiry(code, datetime.now(timezone.utc) - timedelta(seconds=1))

    response = service.visit(code)

    assert response.status_code == 410, (
        "an expired link must not redirect; got {0}: {1}".format(
            response.status_code, response.text[:200]
        )
    )
    error = assert_error_envelope(response)
    assert error["code"] == "link_expired", error
    header_names = set(k.lower() for k in response.headers.keys())
    assert "location" not in header_names, (
        "a refused redirect must not leak the destination in a Location header"
    )


def test_a_refused_expired_redirect_records_no_click(service):
    code = service.create("https://example.com/gone").json()["code"]
    service.set_expiry(code, datetime.now(timezone.utc) - timedelta(days=1))

    for _ in range(3):
        assert service.visit(code).status_code == 410

    assert service.click_rows() == [], (
        "a redirect that failed must not be counted as a click"
    )
    assert service.stats(code).json()["total_clicks"] == 0


def test_a_link_that_has_not_yet_expired_still_redirects(service):
    code = service.create("https://example.com/live").json()["code"]
    service.set_expiry(code, datetime.now(timezone.utc) + timedelta(days=1))

    response = service.visit(code)

    assert response.status_code == 307, response.text[:200]
    assert response.headers["location"] == "https://example.com/live"


def test_an_expiry_in_the_past_is_refused_at_creation_and_writes_no_row(service):
    response = service.create("https://example.com/a", expires_at="2000-01-01T00:00:00Z")

    assert response.status_code in (400, 422), (
        "a past expiry must be refused, got {0}: {1}".format(
            response.status_code, response.text[:200]
        )
    )
    assert_error_envelope(response)
    assert service.link_rows() == []


def test_an_expired_link_still_serves_the_clicks_it_already_accumulated(service):
    code = service.create("https://example.com/gone").json()["code"]
    assert service.visit(code).status_code == 307
    service.set_expiry(code, datetime.now(timezone.utc) - timedelta(days=1))

    assert service.visit(code).status_code == 410
    stats = service.stats(code)

    assert stats.status_code == 200, stats.text[:200]
    assert stats.json()["total_clicks"] == 1, (
        "the refused redirect must not have been counted: " + stats.text[:200]
    )
