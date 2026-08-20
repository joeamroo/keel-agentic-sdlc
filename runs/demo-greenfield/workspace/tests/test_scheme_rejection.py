"""Open redirect surface: only http and https destinations may be stored."""
import pytest

from conftest import assert_error_envelope

DANGEROUS = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "ftp://files.example.com/secret.txt",
]


@pytest.mark.parametrize("target", DANGEROUS)
def test_non_http_scheme_is_rejected_with_400_unsupported_scheme_and_writes_no_row(app, target):
    response = app.create(target)

    assert response.status_code == 400, (
        "{0!r} must be refused at creation, got {1}: {2}".format(
            target, response.status_code, response.text[:200]
        )
    )
    error = assert_error_envelope(response)
    assert error["code"] == "unsupported_scheme", error
    assert app.link_rows() == [], "a rejected destination must leave no row behind"


@pytest.mark.parametrize("target", DANGEROUS)
def test_a_rejected_scheme_is_not_reachable_through_any_code(app, target):
    app.create(target)

    rows = app.link_rows()
    assert rows == []
    # nothing was stored, so nothing can be followed
    assert app.visit("aB3dEf9").status_code == 404


def test_http_and_https_destinations_are_still_accepted(app):
    for target in ("http://example.com/plain", "https://example.com/secure"):
        response = app.create(target)
        assert response.status_code == 201, (target, response.text)
    assert len(app.link_rows()) == 2
