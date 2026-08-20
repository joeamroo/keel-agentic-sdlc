"""Create a link, follow it, and pin the redirect contract."""
import re
from datetime import datetime

from conftest import TS_FORMAT, parse_ts

CODE_RE = re.compile("^[A-Za-z0-9]{7}$")


def test_creating_a_link_returns_201_with_code_short_url_and_the_url_echoed_unchanged(app):
    target = "https://example.com/page?a=1&b=two#frag"

    response = app.create(target)

    assert response.status_code == 201, response.text
    body = response.json()
    assert CODE_RE.match(body["code"]), "code must be 7 base62 chars: " + repr(body["code"])
    assert body["url"] == target, "url must be echoed byte for byte including query and fragment"
    assert body["short_url"] == "http://testserver/" + body["code"]
    assert isinstance(body["created_at"], str) and isinstance(body["expires_at"], str)

    rows = app.link_rows()
    assert len(rows) == 1
    assert rows[0]["code"] == body["code"]
    assert rows[0]["url"] == target


def test_following_a_fresh_code_returns_307_with_location_equal_to_the_stored_url(app):
    target = "https://example.com/page?a=1&b=two#frag"
    code = app.create(target).json()["code"]

    response = app.visit(code)

    assert response.status_code == 307, (
        "expected the design's 307, got {0}: {1}".format(response.status_code, response.text[:200])
    )
    assert response.headers["location"] == target
    assert "no-store" in response.headers.get("cache-control", "").lower(), (
        "redirect must not be cacheable; Cache-Control was "
        + repr(response.headers.get("cache-control"))
    )


def test_short_url_uses_links_base_url_with_the_trailing_slash_stripped(app_factory):
    app = app_factory(LINKS_BASE_URL="https://sho.rt/")

    body = app.create("https://example.com/a").json()

    assert body["short_url"] == "https://sho.rt/" + body["code"]


def test_timestamps_are_stored_in_the_fixed_utc_format_that_sorts_chronologically(app):
    code = app.create("https://example.com/a").json()["code"]

    row = app.link_rows()[0]
    created = datetime.strptime(row["created_at"], TS_FORMAT)
    expires = datetime.strptime(row["expires_at"], TS_FORMAT)
    assert expires > created, "expires_at must be strictly greater than created_at"
    assert row["code"] == code


def test_the_redirect_reads_the_row_written_at_creation_rather_than_a_cached_value(app):
    target = "https://example.com/original"
    code = app.create(target).json()["code"]
    assert app.visit(code).headers["location"] == target

    body = app.stats(code).json()
    assert body["url"] == target
    assert parse_ts(body["expires_at"]) > parse_ts(body["created_at"])
