"""Open redirect surface: only http(s) destinations may ever be stored."""
import pytest

from _pipeline_support import (  # noqa: F401 - fixtures are imported by name
    assert_error_envelope,
    make_app,
    service,
    stub_dns,
)

DANGEROUS = [
    "javascript:alert(1)",
    "JavaScript:alert(document.domain)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "file://localhost/etc/shadow",
]


@pytest.mark.parametrize("target", DANGEROUS)
def test_a_non_http_scheme_is_refused_at_creation_and_leaves_no_row_behind(service, target):
    response = service.create(target)

    assert 400 <= response.status_code < 500, (
        "{0!r} must be refused at creation, got {1}: {2}".format(
            target, response.status_code, response.text[:200]
        )
    )
    assert response.status_code == 400, (
        "the implementation refuses a bad scheme with 400, got {0}".format(
            response.status_code
        )
    )
    error = assert_error_envelope(response)
    assert error["code"] == "unsupported_scheme", error
    assert service.link_rows() == [], (
        "a refused destination must write no links row; found " + repr(service.link_rows())
    )
    assert service.click_rows() == []


def test_refusing_a_javascript_target_does_not_hand_back_a_followable_code(service):
    response = service.create("javascript:alert(1)")

    assert response.status_code == 400
    assert "code" not in response.json(), response.text[:200]
    assert service.link_rows() == []
    # nothing was stored, so no enumeration can reach the payload
    assert service.visit("aB3dEf9").status_code == 404


def test_an_http_and_an_https_target_are_still_accepted_so_the_filter_is_not_deny_all(service):
    plain = service.create("http://example.com/plain")
    secure = service.create("https://example.com/secure")

    assert plain.status_code == 201, plain.text[:200]
    assert secure.status_code == 201, secure.text[:200]
    assert service.link_count() == 2
