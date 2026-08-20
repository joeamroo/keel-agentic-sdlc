"""Open redirect surface: only http(s) destinations may ever be stored."""
import pytest

from conftest import assert_error

DANGEROUS = [
    "javascript:alert(1)",
    "JavaScript:alert(document.domain)",
    "data:text/html,<script>alert(1)</script>",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "file:///etc/passwd",
    "FILE://localhost/etc/shadow",
]


@pytest.mark.parametrize("target", DANGEROUS)
def test_a_dangerous_scheme_is_rejected_at_creation_and_writes_no_row(app, target):
    response = app.create(target)

    assert response.status_code == 400, (
        "{0!r} must be refused at creation, got {1}: {2}".format(
            target, response.status_code, response.text[:200]
        )
    )
    error = assert_error(response)
    assert error["code"] == "unsupported_scheme", error
    assert app.link_rows() == [], (
        "a rejected destination left a row behind: " + repr(app.link_rows())
    )
    assert app.link_count() == 0


@pytest.mark.parametrize(
    "target",
    ["//evil.example.com/path", "ftp://files.example.com/secret", "mailto:a@b.test"],
    ids=["protocol_relative", "ftp", "mailto"],
)
def test_a_non_http_target_is_a_4xx_that_stores_nothing(app, target):
    response = app.create(target)

    assert 400 <= response.status_code < 500, (
        "{0!r} produced {1}: {2}".format(target, response.status_code, response.text[:200])
    )
    assert_error(response)
    assert app.link_count() == 0


def test_rejecting_a_dangerous_scheme_leaves_nothing_reachable(app):
    for target in DANGEROUS:
        app.create(target)

    assert app.link_rows() == []
    assert app.click_rows() == []
    # nothing was stored, so no code can resolve to those targets
    assert app.visit("aB3dEf9").status_code == 404


def test_http_and_https_destinations_are_still_accepted_so_this_is_not_deny_everything(app):
    for target in ("http://example.com/plain", "https://example.com/secure"):
        response = app.create(target)
        assert response.status_code == 201, (target, response.text[:200])

    assert app.link_count() == 2
