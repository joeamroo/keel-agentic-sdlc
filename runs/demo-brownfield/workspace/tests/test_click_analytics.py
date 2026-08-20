"""Click analytics: one row per successful redirect, none for failures."""
from datetime import datetime, timedelta, timezone

from conftest import TS_FORMAT, assert_error_envelope, parse_ts


def test_a_successful_redirect_increments_the_click_count_exactly_once(app):
    code = app.create("https://example.com/a").json()["code"]

    assert app.stats(code).json()["total_clicks"] == 0
    assert app.visit(code).status_code == 307

    assert len(app.click_rows()) == 1, "exactly one click row per redirect"
    assert app.stats(code).json()["total_clicks"] == 1


def test_three_redirects_produce_three_clicks_and_a_total_of_three(app):
    code = app.create("https://example.com/a").json()["code"]

    for _ in range(3):
        assert app.visit(code).status_code == 307

    assert len(app.click_rows()) == 3
    assert app.stats(code).json()["total_clicks"] == 3


def test_a_404_redirect_does_not_increment_any_click_count(app):
    code = app.create("https://example.com/a").json()["code"]
    assert app.visit(code).status_code == 307

    assert app.visit("Qq1Ww2E").status_code == 404

    assert len(app.click_rows()) == 1
    assert app.stats(code).json()["total_clicks"] == 1


def test_a_410_redirect_does_not_increment_the_click_count(app):
    code = app.create("https://example.com/a").json()["code"]
    assert app.visit(code).status_code == 307
    app.set_expiry(code, datetime.now(timezone.utc) - timedelta(minutes=5))

    for _ in range(3):
        assert app.visit(code).status_code == 410

    assert len(app.click_rows()) == 1, "clicks were recorded for a refused redirect"
    assert app.stats(code).json()["total_clicks"] == 1


def test_clicks_are_attributed_to_their_own_link_only(app):
    a = app.create("https://example.com/a").json()["code"]
    b = app.create("https://example.com/b").json()["code"]

    app.visit(a)
    app.visit(a)
    app.visit(b)

    assert app.stats(a).json()["total_clicks"] == 2
    assert app.stats(b).json()["total_clicks"] == 1


def test_a_click_records_the_referer_and_user_agent_headers_verbatim(app):
    code = app.create("https://example.com/a").json()["code"]

    app.visit(code, headers={"Referer": "https://news.test/story", "User-Agent": "pinning-agent/1.0"})

    entries = app.stats(code).json()["clicks"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["referrer"] == "https://news.test/story"
    assert entry["user_agent"] == "pinning-agent/1.0"
    datetime.strptime(app.click_rows()[0]["clicked_at"], TS_FORMAT)
    assert parse_ts(entry["timestamp"]) <= datetime.now(timezone.utc) + timedelta(seconds=5)


def test_absent_referer_and_user_agent_are_recorded_as_null(app):
    code = app.create("https://example.com/a").json()["code"]
    try:
        del app.client.headers["user-agent"]
    except Exception:  # pragma: no cover - header container differences
        pass

    app.visit(code)

    entry = app.stats(code).json()["clicks"][0]
    assert entry["referrer"] is None, "absent Referer must be null, not empty string"
    assert entry["user_agent"] is None, "absent User-Agent must be null, not empty string"


def test_the_clicks_table_has_no_column_capable_of_holding_a_client_ip(app):
    code = app.create("https://example.com/a").json()["code"]
    app.visit(code)

    for column in app.columns("clicks"):
        low = column.lower()
        assert "ip" not in low, column
        assert "addr" not in low, column
        assert "remote" not in low, column
        assert "client" not in low, column


def test_stats_returns_the_link_metadata_alongside_newest_first_clicks(app):
    target = "https://example.com/a?x=1"
    created = app.create(target).json()
    code = created["code"]
    for i in (1, 2, 3):
        app.visit(code, headers={"Referer": "https://ref{0}.test/".format(i)})

    body = app.stats(code).json()

    assert body["code"] == code
    assert body["url"] == target
    assert parse_ts(body["created_at"]) == parse_ts(created["created_at"])
    assert parse_ts(body["expires_at"]) == parse_ts(created["expires_at"])
    assert body["total_clicks"] == 3
    assert [c["referrer"] for c in body["clicks"]] == [
        "https://ref3.test/",
        "https://ref2.test/",
        "https://ref1.test/",
    ]


def test_stats_limit_and_offset_page_the_clicks_without_changing_total_clicks(app):
    code = app.create("https://example.com/a").json()["code"]
    for i in (1, 2, 3):
        app.visit(code, headers={"Referer": "https://ref{0}.test/".format(i)})

    one = app.stats(code, limit=1).json()
    assert one["total_clicks"] == 3
    assert len(one["clicks"]) == 1

    page1 = app.stats(code, limit=2, offset=0).json()["clicks"]
    page2 = app.stats(code, limit=2, offset=2).json()["clicks"]
    assert len(page1) == 2 and len(page2) == 1
    assert [c["referrer"] for c in page1 + page2] == [
        "https://ref3.test/",
        "https://ref2.test/",
        "https://ref1.test/",
    ]


def test_a_stats_limit_above_the_configured_maximum_is_a_validation_error(app):
    code = app.create("https://example.com/a").json()["code"]

    response = app.stats(code, limit=501)

    assert response.status_code == 422, response.text[:200]
    assert_error_envelope(response)
