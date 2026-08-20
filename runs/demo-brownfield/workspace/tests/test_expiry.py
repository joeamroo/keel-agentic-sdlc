"""Expiry behaviour. Time is controlled by writing expires_at, never by sleeping."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import assert_error, parse_ts


def test_an_expired_link_returns_410_with_no_location_header_and_no_click(app):
    code = app.create_ok("https://example.com/gone")
    app.set_expiry(code, datetime.now(timezone.utc) - timedelta(seconds=1))

    response = app.visit(code)

    assert response.status_code == 410, (
        "an expired link must be 410 Gone, got {0}: {1}".format(
            response.status_code, response.text[:200]
        )
    )
    error = assert_error(response)
    assert error["code"] == "link_expired", error
    assert "location" not in {k.lower() for k in response.headers.keys()}, (
        "a 410 must not hand the client a Location"
    )
    assert app.click_rows() == [], "a refused redirect must not be recorded as a click"


def test_an_expired_link_is_distinguishable_from_a_code_that_never_existed(app):
    code = app.create_ok("https://example.com/gone")
    app.set_expiry(code, datetime.now(timezone.utc) - timedelta(days=1))

    expired = app.visit(code)
    unknown = app.visit("Zz9Yy8X")

    assert expired.status_code == 410
    assert unknown.status_code == 404
    assert assert_error(unknown)["code"] == "not_found"


def test_a_link_whose_expiry_is_still_in_the_future_keeps_redirecting(app):
    target = "https://example.com/live"
    code = app.create_ok(target)
    app.set_expiry(code, datetime.now(timezone.utc) + timedelta(days=1))

    response = app.visit(code)

    assert response.status_code == 307, response.text[:200]
    assert response.headers["location"] == target


@pytest.mark.parametrize(
    "value",
    ["2000-01-01T00:00:00Z", "1999-12-31T23:59:59.999999Z"],
    ids=["past_instant", "just_before_2000"],
)
def test_an_expires_at_in_the_past_is_refused_and_writes_no_row(app, value):
    response = app.create("https://example.com/a", expires_at=value)

    assert response.status_code == 400, (value, response.status_code, response.text[:200])
    assert_error(response)
    assert app.link_rows() == []


def test_an_unparseable_expires_at_is_refused_and_writes_no_row(app):
    response = app.create("https://example.com/a", expires_at="next tuesday")

    assert response.status_code in (400, 422), response.text[:200]
    assert_error(response)
    assert app.link_rows() == []


def test_an_explicit_future_expiry_is_stored_normalized_to_utc(app):
    response = app.create("https://example.com/a", expires_at="2999-01-01T12:00:00+02:00")

    assert response.status_code == 201, response.text[:200]
    expected = datetime(2999, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert parse_ts(response.json()["expires_at"]) == expected
    assert parse_ts(app.link_rows()[0]["expires_at"]) == expected


def test_omitting_the_expiry_applies_the_configured_default_ttl(app_factory):
    app = app_factory(LINKS_DEFAULT_TTL_DAYS="3")

    body = app.create("https://example.com/a").json()

    delta = parse_ts(body["expires_at"]) - parse_ts(body["created_at"])
    assert abs(delta - timedelta(days=3)) <= timedelta(seconds=2), delta


def test_stats_for_an_expired_link_still_answer_200_with_the_old_totals(app):
    code = app.create_ok("https://example.com/gone")
    assert app.visit(code).status_code == 307
    app.set_expiry(code, datetime.now(timezone.utc) - timedelta(days=1))

    assert app.visit(code).status_code == 410
    stats = app.stats(code)

    assert stats.status_code == 200, stats.text[:200]
    body = stats.json()
    assert body["code"] == code
    assert body["total_clicks"] == 1, "the 410 must not have been counted"
