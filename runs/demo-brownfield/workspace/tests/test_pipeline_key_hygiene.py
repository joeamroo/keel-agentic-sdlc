"""Key material never lands on disk, in logs or in responses; schema unchanged."""
import logging

from _pipeline_support import (  # noqa: F401 - fixtures are imported by name
    API_KEY_HEADER,
    assert_error_envelope,
    make_app,
    service,
    stub_dns,
)

# Exactly the columns app/db.py SCHEMA_STATEMENTS creates today.
LINKS_COLUMNS = ["id", "code", "url", "created_at", "expires_at"]
CLICKS_COLUMNS = ["id", "link_id", "clicked_at", "referrer", "user_agent"]


def test_no_configured_key_name_reaches_the_database_logs_or_a_response(make_app, caplog):
    caplog.set_level(logging.DEBUG)
    svc = make_app(
        SHORTENER_API_KEYS="alpha:2,delta:7",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
    )

    responses = []
    for i in range(3):
        responses.append(svc.create("https://example.com/t{0}".format(i), api_key="alpha"))
    for i in range(3):
        responses.append(svc.create("https://example.com/u{0}".format(i), api_key="delta"))
    created = [r for r in responses if r.status_code == 201]
    assert created, "no creation succeeded, so this test proves nothing"
    code = created[0].json()["code"]
    responses.append(svc.visit(code, api_key="delta"))
    responses.append(svc.stats(code, api_key="alpha"))

    blob = svc.disk_bytes()
    log_text = caplog.text
    for name in ("alpha", "delta"):
        assert name.encode() not in blob, (
            "the key name {0!r} was written to a file under LINKS_DB_PATH".format(name)
        )
        assert name not in log_text, (
            "the key name {0!r} appears in a log record".format(name)
        )
        for response in responses:
            assert name not in response.text, (
                "the key name {0!r} was echoed in a response body".format(name)
            )
            for header, value in response.headers.items():
                assert name not in value, (
                    "the key name {0!r} was echoed in header {1}".format(name, header)
                )


def test_keyed_traffic_adds_no_table_and_no_column_to_the_database(make_app):
    svc = make_app(
        SHORTENER_API_KEYS="alpha:2",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
    )
    for i in range(4):
        svc.create("https://example.com/t{0}".format(i), api_key="alpha")

    assert svc.columns("links") == LINKS_COLUMNS, svc.columns("links")
    assert svc.columns("clicks") == CLICKS_COLUMNS, svc.columns("clicks")
    tables = sorted(n for n in svc.table_names() if not n.startswith("sqlite_"))
    assert tables == ["clicks", "links"], (
        "rate limiter state must stay in process, not in SQLite; found " + repr(tables)
    )


def test_a_creation_refused_by_the_limiter_writes_neither_a_link_nor_a_click(make_app):
    svc = make_app(
        SHORTENER_API_KEYS="alpha:1",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
    )
    assert svc.create("https://example.com/first", api_key="alpha").status_code == 201

    refused = svc.create("https://example.com/second", api_key="alpha")

    assert refused.status_code == 429, refused.text[:200]
    assert assert_error_envelope(refused)["code"] == "rate_limited"
    rows = svc.link_rows()
    assert len(rows) == 1, "the refused creation wrote a links row: " + repr(rows)
    assert svc.click_rows() == []
