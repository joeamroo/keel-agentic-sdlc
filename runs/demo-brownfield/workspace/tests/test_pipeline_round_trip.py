"""Round trip: create a link, follow it, pin the redirect contract."""
import re

from _pipeline_support import (  # noqa: F401 - fixtures are imported by name
    assert_error_envelope,
    link_target,
    make_app,
    parse_ts,
    service,
    stub_dns,
)

CODE_RE = re.compile(r"^[A-Za-z0-9]{7}$")
TARGET = "https://example.com/page?a=1&b=two#frag"


def test_a_created_link_redirects_with_307_and_the_stored_url_in_location(service):
    created = service.create(TARGET)

    assert created.status_code == 201, created.text[:300]
    body = created.json()
    assert CODE_RE.match(body["code"]), "code must be 7 base62 chars: " + repr(body["code"])
    assert body["url"] == TARGET, "the url must be echoed byte for byte"
    assert body["short_url"] == "http://testserver/" + body["code"]
    assert "id" not in body, "the row id must never be exposed: " + repr(body)

    followed = service.visit(body["code"])

    assert followed.status_code == 307, (
        "the design's redirect status is 307, got {0}: {1}".format(
            followed.status_code, followed.text[:200]
        )
    )
    assert followed.headers["location"] == TARGET, (
        "Location must be the stored destination exactly, got "
        + repr(followed.headers.get("location"))
    )
    assert "no-store" in followed.headers.get("cache-control", "").lower(), (
        "a redirect must not be cacheable; Cache-Control was "
        + repr(followed.headers.get("cache-control"))
    )

    rows = service.link_rows()
    assert len(rows) == 1
    assert rows[0]["code"] == body["code"]
    assert link_target(rows[0]) == TARGET


def test_short_url_is_built_from_links_base_url_with_the_trailing_slash_stripped(make_app):
    svc = make_app(LINKS_BASE_URL="https://sho.rt/")

    body = svc.create("https://example.com/a").json()

    assert body["short_url"] == "https://sho.rt/" + body["code"], body


def test_two_creations_of_the_same_target_yield_two_distinct_codes_that_both_resolve(service):
    first = service.create("https://example.com/same").json()["code"]
    second = service.create("https://example.com/same").json()["code"]

    assert first != second, "codes must be unpredictable, not derived from the target"
    assert service.visit(first).headers["location"] == "https://example.com/same"
    assert service.visit(second).headers["location"] == "https://example.com/same"


def test_the_created_link_reports_an_expiry_strictly_after_its_creation_instant(service):
    body = service.create("https://example.com/a").json()

    assert parse_ts(body["expires_at"]) > parse_ts(body["created_at"]), body
