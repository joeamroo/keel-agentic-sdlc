"""Click analytics counts successful redirects and only successful redirects."""
from datetime import datetime, timedelta, timezone

from conftest import TS_FORMAT, parse_ts


def test_a_successful_redirect_increments_the_click_count_exactly_once(app):
    code = app.create_ok("https://example.com/a")
    assert app.stats(code).json()["total_clicks"] == 0

    assert app.visit(code).status_code == 307

    assert app.click_count() == 1, "exactly one click row per successful redirect"
    assert app.stats(code).json()["total_clicks"] == 1


def test_three_redirects_produce_exactly_three_clicks(app):
    code = app.create_ok("https://example.com/a")

    for _ in range(3):
        assert app.visit(code).status_code == 307

    assert app.click_count() == 3
    assert app.stats(code).json()["total_clicks"] == 3


def test_a_404_redirect_does_not_increment_any_click_count(app):
    code = app.create_ok("https://example.com/a")
    assert app.visit(code).status_code == 307

    assert app.visit("Qq1Ww2E").status_code == 404

    assert app.click_count() == 1, "an unknown code was counted as a click"
    assert app.stats(code).json()["total_clicks"] == 1


def test_a_410_redirect_does_not_increment_the_click_count(app):
    code = app.create_ok("https://example.com/a")
    assert app.visit(code).status_code == 307
    app.set_expiry(code, datetime.now(timezone.utc) - timedelta(minutes=5))

    for _ in range(3):
        assert app.visit(code).status_code == 410

    assert app.click_count() == 1, "a refused redirect was counted as a click"
    assert app.stats(code).json()["total_clicks"] == 1


def test_clicks_are_attributed_to_their_own_link_only(app):
    first = app.create_ok("https://example.com/a")
    second = app.create_ok("https://example.com/b")

    app.visit(first)
    app.visit(first)
    app.visit(second)

    assert app.stats(first).json()["total_clicks"] == 2
    assert app.stats(second).json()["total_clicks"] == 1


def test_a_click_records_the_referer_and_user_agent_and_a_utc_timestamp(app):
    code = app.create_ok("https://example.com/a")

    app.visit(
        code,
        headers={"Referer": "https://news.test/story", "User-Agent": "pinning-agent/1.0"},
    )

    entries = app.stats(code).json()["clicks"]
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["referrer"] == "https://news.test/story"
    assert entry["user_agent"] == "pinning-agent/1.0"
    datetime.strptime(app.click_rows()[0]["clicked_at"], TS_FORMAT)
    assert parse_ts(entry["timestamp"]) <= datetime.now(timezone.utc) + timedelta(seconds=5)


def test_stats_pages_clicks_newest_first_without_changing_the_total(app):
    code = app.create_ok("https://example.com/a")
    for i in (1, 2, 3):
        app.visit(code, headers={"Referer": "https://ref{0}.test/".format(i)})

    body = app.stats(code).json()
    assert body["total_clicks"] == 3
    assert [c["referrer"] for c in body["clicks"]] == [
        "https://ref3.test/",
        "https://ref2.test/",
        "https://ref1.test/",
    ]

    page_one = app.stats(code, params={"limit": 2, "offset": 0}).json()
    page_two = app.stats(code, params={"limit": 2, "offset": 2}).json()
    assert page_one["total_clicks"] == 3 and page_two["total_clicks"] == 3
    assert len(page_one["clicks"]) == 2 and len(page_two["clicks"]) == 1


def test_the_clicks_table_has_no_column_capable_of_holding_a_client_address(app):
    code = app.create_ok("https://example.com/a")
    app.visit(code)

    for column in app.columns("clicks"):
        low = column.lower()
        assert "ip" not in low, column
        assert "addr" not in low, column
        assert "remote" not in low, column
        assert "client" not in low, column
