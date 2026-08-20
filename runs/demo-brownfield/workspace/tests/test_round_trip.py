"""Round trip: create a link, follow it, pin the redirect contract."""
import re

from conftest import parse_ts

CODE_RE = re.compile(r"^[A-Za-z0-9]{7}$")


def test_creating_a_link_then_following_it_returns_307_to_the_exact_stored_target(app):
    target = "https://example.com/deep/path?a=1&b=two#frag"

    created = app.create(target)
    assert created.status_code == 201, created.text[:300]
    body = created.json()
    assert set(body) >= {"code", "short_url", "url", "created_at", "expires_at"}, body
    code = body["code"]
    assert CODE_RE.match(code), "code must be 7 base62 characters: " + repr(code)
    assert body["url"] == target, "url must be echoed byte for byte"
    assert body["short_url"] == "http://localhost:8000/" + code, body["short_url"]

    followed = app.visit(code)
    assert followed.status_code == 307, (
        "the design pins a 307 redirect, got {0}: {1}".format(
            followed.status_code, followed.text[:200]
        )
    )
    assert followed.headers["location"] == target, (
        "Location must be the stored target verbatim, got "
        + repr(followed.headers.get("location"))
    )
    assert "no-store" in followed.headers.get("cache-control", "").lower(), (
        "a redirect must not be cacheable; Cache-Control was "
        + repr(followed.headers.get("cache-control"))
    )


def test_the_created_row_is_the_single_source_of_the_redirect_target(app):
    target = "http://example.com/only-one"
    code = app.create_ok(target)

    rows = app.link_rows()
    assert len(rows) == 1, rows
    assert rows[0]["code"] == code
    assert rows[0]["url"] == target
    assert parse_ts(rows[0]["expires_at"]) > parse_ts(rows[0]["created_at"])

    assert app.visit(code).headers["location"] == target


def test_short_url_uses_links_base_url_with_any_trailing_slash_stripped(app_factory):
    app = app_factory(LINKS_BASE_URL="https://sho.rt/")

    body = app.create("https://example.com/a").json()

    assert body["short_url"] == "https://sho.rt/" + body["code"], body["short_url"]


def test_two_links_created_in_one_process_get_distinct_codes_and_distinct_targets(app):
    first = app.create_ok("https://example.com/one")
    second = app.create_ok("https://example.com/two")

    assert first != second, "two creations returned the same code"
    assert app.visit(first).headers["location"] == "https://example.com/one"
    assert app.visit(second).headers["location"] == "https://example.com/two"


def test_links_code_length_is_read_from_the_environment(app_factory):
    app = app_factory(LINKS_CODE_LENGTH="10")

    code = app.create_ok("https://example.com/a")

    assert re.match(r"^[A-Za-z0-9]{10}$", code), code
    assert app.visit(code).status_code == 307
