"""Expiry: default TTL, explicit instants, rejection of the past, and 410.

Time is controlled by writing the expires_at column, never by sleeping.
"""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import assert_error_envelope, parse_ts


def test_omitting_expires_at_stores_created_at_plus_the_configured_ttl_days(app):
    body = app.create("https://example.com/a").json()

    created = parse_ts(body["created_at"])
    expires = parse_ts(body["expires_at"])
    delta = abs((expires - created) - timedelta(days=30))
    assert delta <= timedelta(seconds=1), (
        "default TTL must be LINKS_DEFAULT_TTL_DAYS=30 days, got "
        + str(expires - created)
    )
    row = app.link_rows()[0]
    assert parse_ts(row["expires_at"]) == expires


def test_configured_ttl_days_is_honoured_instead_of_a_hard_coded_thirty(app_factory):
    app = app_factory(LINKS_DEFAULT_TTL_DAYS="3")

    body = app.create("https://example.com/a").json()

    delta = parse_ts(body["expires_at"]) - parse_ts(body["created_at"])
    assert abs(delta - timedelta(days=3)) <= timedelta(seconds=1), delta


def test_explicit_future_expires_at_is_stored_and_returned_normalized_to_utc(app):
    response = app.create("https://example.com/a", expires_at="2999-01-01T12:00:00+02:00")

    assert response.status_code == 201, response.text
    expected = datetime(2999, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert parse_ts(response.json()["expires_at"]) == expected
    assert parse_ts(app.link_rows()[0]["expires_at"]) == expected


@pytest.mark.parametrize(
    "value",
    ["2000-01-01T00:00:00Z", "1999-12-31T23:59:59.999999Z"],
    ids=["past_instant", "just_before_2000"],
)
def test_expires_at_in_the_past_is_rejected_with_400_and_writes_no_row(app, value):
    response = app.create("https://example.com/a", expires_at=value)

    assert response.status_code == 400, (value, response.status_code, response.text[:200])
    assert_error_envelope(response)
    assert app.link_rows() == []


def test_expires_at_equal_to_now_is_rejected_and_writes_no_row(app):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    response = app.create("https://example.com/a", expires_at=now)

    assert response.status_code == 400, response.text
    assert_error_envelope(response)
    assert app.link_rows() == []


def test_unparseable_expires_at_is_rejected_and_writes_no_row(app):
    response = app.create("https://example.com/a", expires_at="next tuesday")

    assert response.status_code in (400, 422), response.text
    assert_error_envelope(response)
    assert app.link_rows() == []


def test_expired_link_returns_410_without_a_location_header_and_logs_no_click(app):
    code = app.create("https://example.com/gone").json()["code"]
    app.set_expiry(code, datetime.now(timezone.utc) - timedelta(seconds=1))

    response = app.visit(code)

    assert response.status_code == 410, (
        "expired links must be 410 Gone, got {0}".format(response.status_code)
    )
    error = assert_error_envelope(response)
    assert error["code"] == "link_expired", error
    header_names = set(k.lower() for k in response.headers.keys())
    assert "location" not in header_names, "a 410 must not tempt clients with a Location"
    assert app.click_rows() == [], "a refused redirect must not be counted as a click"


def test_an_expired_link_is_distinguishable_from_a_code_that_never_existed(app):
    code = app.create("https://example.com/gone").json()["code"]
    app.set_expiry(code, datetime.now(timezone.utc) - timedelta(days=1))

    expired = app.visit(code)
    unknown = app.visit("Zz9Yy8X")

    assert expired.status_code == 410
    assert unknown.status_code == 404
    assert assert_error_envelope(unknown)["code"] == "not_found"


def test_expired_link_still_serves_its_accumulated_stats_with_200(app):
    code = app.create("https://example.com/gone").json()["code"]
    assert app.visit(code).status_code == 307
    app.set_expiry(code, datetime.now(timezone.utc) - timedelta(days=1))

    assert app.visit(code).status_code == 410
    stats = app.stats(code)

    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body["code"] == code
    assert body["total_clicks"] == 1, "the 410 must not have been counted"
