"""Unknown and malformed codes: 404, and nothing is created."""
import pytest

from _pipeline_support import (  # noqa: F401 - fixtures are imported by name
    assert_error_envelope,
    make_app,
    service,
    stub_dns,
)


def test_an_unknown_code_returns_404_and_creates_nothing(service):
    response = service.visit("Ab3Cd9Z")

    assert response.status_code == 404, (
        "an unissued code must be a 404, got {0}: {1}".format(
            response.status_code, response.text[:200]
        )
    )
    error = assert_error_envelope(response)
    assert error["code"] == "not_found", error
    assert "location" not in set(k.lower() for k in response.headers.keys())
    assert service.link_rows() == [], "a 404 must not create a link row"
    assert service.click_rows() == [], "a 404 must not create a click row"


@pytest.mark.parametrize(
    "code",
    ["short", "waytoolongforacodehere", "abc-def", "abc_def", "1234567890"],
    ids=["too_short", "too_long", "hyphen", "underscore", "ten_digits"],
)
def test_a_malformed_code_returns_404_rather_than_a_server_error(service, code):
    response = service.visit(code)

    assert response.status_code == 404, (
        "{0!r} should be a plain 404, got {1}: {2}".format(
            code, response.status_code, response.text[:200]
        )
    )
    assert_error_envelope(response)
    assert service.click_rows() == []


def test_stats_for_an_unknown_code_returns_404_and_creates_nothing(service):
    response = service.stats("Ab3Cd9Z")

    assert response.status_code == 404, response.text[:200]
    assert assert_error_envelope(response)["code"] == "not_found"
    assert service.link_rows() == []


def test_a_404_for_one_code_leaves_an_existing_link_working(service):
    code = service.create("https://example.com/live").json()["code"]

    assert service.visit("Zz9Yy8X").status_code == 404

    assert service.visit(code).status_code == 307
    assert service.stats(code).json()["total_clicks"] == 1
