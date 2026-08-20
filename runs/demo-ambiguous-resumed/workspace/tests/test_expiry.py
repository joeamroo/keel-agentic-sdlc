"""Expiry is enforced by stored timestamp, never by wall-clock sleeping."""

import datetime as dt

import pytest

PAST = "2000-01-01T00:00:00Z"


def test_expired_link_returns_410_and_emits_no_location_header(client, create_link, db, helpers):
    resp = create_link(client, "https://example.com/a", expires_in_days=1)
    assert resp.status_code == 201, resp.text
    code = helpers.code_of(resp.json())

    db.set_expiry(code, PAST)

    redirect = client.get(f"/{code}", follow_redirects=False)

    assert redirect.status_code == 410, (
        f"expired link answered {redirect.status_code} instead of 410 Gone: {redirect.text}"
    )
    assert "location" not in {k.lower() for k in redirect.headers.keys()}, dict(redirect.headers)
    assert helpers.error_code(redirect) in {"link_expired", "expired", "gone"}, redirect.text
    assert "example.com/a" not in redirect.text, "the 410 body leaked the destination"


def test_expired_link_row_is_not_removed_or_mutated_by_the_failed_lookup(
    client, create_link, db, helpers
):
    code = helpers.code_of(create_link(client, "https://example.com/a").json())
    db.set_expiry(code, PAST)

    client.get(f"/{code}", follow_redirects=False)

    row = db.row_for(code)
    assert row is not None, "the expired row disappeared during a redirect attempt"
    assert db.destination_of(row) == "https://example.com/a"


def test_omitted_expiry_uses_the_30_day_default(client, create_link, helpers):
    body = create_link(client, "https://example.com/a").json()

    created = helpers.parse_iso(body["created_at"])
    expires = helpers.parse_iso(body["expires_at"])

    delta = expires - created
    assert abs(delta - dt.timedelta(days=30)) <= dt.timedelta(seconds=2), (
        f"LINKS_DEFAULT_EXPIRY_DAYS=30 but expires_at - created_at == {delta}"
    )


def test_explicit_one_day_expiry_is_echoed_as_iso_utc_one_day_out(client, create_link, helpers):
    resp = create_link(client, "https://example.com/a", expires_in_days=1)
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["expires_at"].endswith("Z"), body["expires_at"]
    delta = helpers.parse_iso(body["expires_at"]) - helpers.parse_iso(body["created_at"])
    assert abs(delta - dt.timedelta(days=1)) <= dt.timedelta(seconds=2), delta


@pytest.mark.parametrize("days", [0, -1, 366, 100000])
def test_out_of_range_numeric_expiry_is_rejected_and_writes_no_row(client, create_link, db, days):
    resp = create_link(client, "https://example.com/a", expires_in_days=days)

    assert resp.status_code == 400, f"expires_in_days={days} -> {resp.status_code}: {resp.text}"
    assert db.count() == 0


def test_non_numeric_expiry_is_rejected_and_writes_no_row(client, create_link, db):
    resp = create_link(client, "https://example.com/a", expires_in_days="abc")

    assert resp.status_code in (400, 422), resp.text
    assert db.count() == 0
