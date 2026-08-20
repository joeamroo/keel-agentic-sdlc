"""Unknown and malformed codes are 404s that create nothing."""
import pytest

from conftest import assert_error


def test_an_unknown_code_returns_404_and_creates_nothing(app):
    response = app.visit("Ab3Cd9Z")

    assert response.status_code == 404, response.text[:200]
    error = assert_error(response)
    assert error["code"] == "not_found", error
    assert "location" not in {k.lower() for k in response.headers.keys()}
    assert app.link_rows() == [], "a 404 lookup created a links row"
    assert app.click_rows() == [], "a 404 lookup created a clicks row"


@pytest.mark.parametrize(
    "code",
    ["short", "waytoolongforacode", "abc-def", "abc_def", "1234567890", ".."],
    ids=["too_short", "too_long", "hyphen", "underscore", "ten_digits", "dots"],
)
def test_a_malformed_code_is_a_plain_404_and_not_a_server_error(app, code):
    response = app.visit(code)

    assert response.status_code == 404, (
        "{0!r} should be a plain 404, got {1}: {2}".format(
            code, response.status_code, response.text[:200]
        )
    )
    assert_error(response)
    assert app.link_count() == 0
    assert app.click_count() == 0


def test_stats_for_an_unknown_code_returns_404_with_the_error_envelope(app):
    response = app.stats("Ab3Cd9Z")

    assert response.status_code == 404, response.text[:200]
    assert assert_error(response)["code"] == "not_found"
    assert app.link_count() == 0


def test_a_404_for_one_code_does_not_disturb_an_existing_link(app):
    code = app.create_ok("https://example.com/live")

    assert app.visit("Zz9Yy8X").status_code == 404

    assert app.visit(code).status_code == 307
    assert app.stats(code).json()["total_clicks"] == 1
    assert app.link_count() == 1
