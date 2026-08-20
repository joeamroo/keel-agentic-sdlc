"""Dangerous and non-http(s) schemes are refused before anything is written."""

import pytest

SCHEME_ERRORS = {
    "scheme_not_allowed",
    "invalid_scheme",
    "unsupported_scheme",
    "invalid_url",
    "invalid_destination",
}


@pytest.mark.parametrize(
    "destination",
    [
        "javascript:alert(1)",
        "javascript:alert(document.domain)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "file:///etc/passwd",
    ],
    ids=["javascript_alert", "javascript_domain", "data_html", "file_etc_passwd"],
)
def test_open_redirect_scheme_is_rejected_and_no_row_is_written(
    client, create_link, db, helpers, destination
):
    resp = create_link(client, destination)

    assert resp.status_code == 400, f"{destination} was not rejected: {resp.status_code} {resp.text}"
    code = helpers.error_code(resp)
    assert code, f"rejection carried no machine-readable error code: {resp.text}"
    assert code in SCHEME_ERRORS, f"unexpected error code {code!r} for {destination}"
    assert db.count() == 0, f"a row was written for the rejected destination {destination}"


@pytest.mark.parametrize(
    "destination",
    [
        "ftp://example.com/x",
        "mailto:someone@example.com",
        "blob:https://example.com/1234",
        "vbscript:msgbox(1)",
        "about:blank",
    ],
)
def test_non_http_scheme_is_rejected_and_no_row_is_written(client, create_link, db, destination):
    resp = create_link(client, destination)

    assert resp.status_code == 400, f"{destination} was not rejected: {resp.status_code} {resp.text}"
    assert db.count() == 0, f"a row was written for the rejected destination {destination}"


def test_rejection_body_is_json_and_does_not_reflect_destination_markup(client, create_link, db):
    payload = 'javascript:"<img src=x onerror=alert(1)>"'

    resp = create_link(client, payload)

    assert resp.status_code == 400, resp.text
    assert "text/html" not in resp.headers.get("content-type", "").lower()
    assert "<img" not in resp.text, "error body reflected raw destination markup"
    assert db.count() == 0


def test_credentials_in_url_are_rejected_and_no_row_is_written(client, create_link, db, helpers):
    resp = create_link(client, "http://user:pass@example.com/")

    assert resp.status_code == 400, resp.text
    assert helpers.error_code(resp) in {"credentials_in_url", "invalid_url", "invalid_destination"}
    assert db.count() == 0


def test_malformed_and_overlong_destinations_are_rejected_and_write_no_row(client, create_link, db):
    for destination in (
        "example.com/no-scheme",
        "http:///no-host",
        "http://exa mple.com/space",
        "https://example.com/" + "a" * 2100,
    ):
        resp = create_link(client, destination)
        assert resp.status_code == 400, f"{destination[:60]!r} -> {resp.status_code}: {resp.text[:200]}"
    assert db.count() == 0
