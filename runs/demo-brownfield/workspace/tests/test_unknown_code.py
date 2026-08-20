"""Unknown and malformed codes are 404s that change nothing."""
import pytest

from conftest import assert_error_envelope


def test_unknown_code_returns_404_and_creates_nothing(app):
    response = app.visit("Ab3Cd9Z")

    assert response.status_code == 404, response.text[:200]
    error = assert_error_envelope(response)
    assert error["code"] == "not_found", error
    assert "location" not in set(k.lower() for k in response.headers.keys())
    assert app.link_rows() == []
    assert app.click_rows() == []


@pytest.mark.parametrize(
    "code",
    ["short", "waytoolongforacode", "abc-def", "abc_def", "aaaaaa%20", "1234567890"],
    ids=["too_short", "too_long", "hyphen", "underscore", "escaped_space", "ten_digits"],
)
def test_a_code_outside_the_character_set_or_length_is_404_not_a_server_error(app, code):
    response = app.visit(code)

    assert response.status_code == 404, (
        "{0!r} should be a plain 404, got {1}: {2}".format(
            code, response.status_code, response.text[:200]
        )
    )
    assert_error_envelope(response)
    assert app.click_rows() == []


def test_stats_for_an_unknown_code_returns_404_with_a_json_error_body(app):
    response = app.stats("Ab3Cd9Z")

    assert response.status_code == 404, response.text[:200]
    error = assert_error_envelope(response)
    assert error["code"] == "not_found", error
    assert app.link_rows() == []


def test_a_404_for_one_code_does_not_disturb_an_existing_link(app):
    code = app.create("https://example.com/live").json()["code"]

    assert app.visit("Zz9Yy8X").status_code == 404

    assert app.visit(code).status_code == 307
    assert app.stats(code).json()["total_clicks"] == 1
