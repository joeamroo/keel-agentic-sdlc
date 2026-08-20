"""Input validation on creation and the stable error envelope everywhere."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import assert_error_envelope

BASE = "https://example.com/"


@pytest.mark.parametrize(
    "payload",
    [{}, {"url": 12345}, {"url": None}, {"url": ""}, {"url": ["https://example.com/"]}],
    ids=["missing_url", "integer_url", "null_url", "empty_url", "list_url"],
)
def test_malformed_create_payload_is_rejected_and_writes_no_row(app, payload):
    response = app.client.post("/api/links", json=payload)

    assert response.status_code in (400, 422), (
        "{0!r} must be refused, got {1}: {2}".format(
            payload, response.status_code, response.text[:200]
        )
    )
    assert_error_envelope(response)
    assert app.link_rows() == []


def test_a_url_longer_than_links_max_url_length_is_rejected_and_writes_no_row(app):
    too_long = BASE + "a" * (2049 - len(BASE))
    assert len(too_long) == 2049

    response = app.create(too_long)

    assert response.status_code in (400, 422), response.text[:200]
    assert_error_envelope(response)
    assert app.link_rows() == []


def test_a_url_exactly_at_the_maximum_length_is_accepted(app):
    exact = BASE + "a" * (2048 - len(BASE))
    assert len(exact) == 2048

    response = app.create(exact)

    assert response.status_code == 201, response.text[:200]
    assert response.json()["url"] == exact
    assert app.link_rows()[0]["url"] == exact


def test_links_max_url_length_is_read_from_the_environment(app_factory):
    app = app_factory(LINKS_MAX_URL_LENGTH="40")
    long_for_this_app = BASE + "a" * 40

    response = app.create(long_for_this_app)

    assert response.status_code in (400, 422), response.text[:200]
    assert app.link_rows() == []


def test_every_error_path_returns_the_same_json_envelope(app):
    code = app.create("https://example.com/a").json()["code"]
    app.set_expiry(code, datetime.now(timezone.utc) - timedelta(days=1))

    responses = [
        app.create("javascript:alert(1)"),
        app.create("http://169.254.169.254/"),
        app.client.post("/api/links", json={}),
        app.visit("Zz9Yy8X"),
        app.visit(code),
        app.stats("Zz9Yy8X"),
    ]

    statuses = [r.status_code for r in responses]
    assert all(400 <= s < 600 for s in statuses), statuses
    for response in responses:
        error = assert_error_envelope(response)
        assert set(error.keys()) >= {"code", "message"}, error
