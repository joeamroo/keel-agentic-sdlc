"""Click analytics: one increment per successful redirect, none for failures."""
from datetime import datetime, timedelta, timezone

from _pipeline_support import (  # noqa: F401 - fixtures are imported by name
    assert_error_envelope,
    make_app,
    service,
    stub_dns,
)


def test_a_successful_redirect_increments_the_click_count_exactly_once(service):
    code = service.create("https://example.com/a").json()["code"]
    assert service.stats(code).json()["total_clicks"] == 0

    assert service.visit(code).status_code == 307

    assert service.click_count() == 1, "exactly one click row per successful redirect"
    assert service.stats(code).json()["total_clicks"] == 1


def test_three_successful_redirects_increment_the_count_to_three(service):
    code = service.create("https://example.com/a").json()["code"]

    for _ in range(3):
        assert service.visit(code).status_code == 307

    assert service.click_count() == 3
    assert service.stats(code).json()["total_clicks"] == 3


def test_a_redirect_that_404s_does_not_increment_any_click_count(service):
    code = service.create("https://example.com/a").json()["code"]
    assert service.visit(code).status_code == 307

    assert service.visit("Qq1Ww2E").status_code == 404

    assert service.click_count() == 1, "a failed redirect was counted as a click"
    assert service.stats(code).json()["total_clicks"] == 1


def test_a_redirect_refused_for_expiry_does_not_increment_the_click_count(service):
    code = service.create("https://example.com/a").json()["code"]
    assert service.visit(code).status_code == 307
    service.set_expiry(code, datetime.now(timezone.utc) - timedelta(minutes=5))

    for _ in range(3):
        assert service.visit(code).status_code == 410

    assert service.click_count() == 1, "a refused redirect was counted as a click"
    assert service.stats(code).json()["total_clicks"] == 1


def test_clicks_are_attributed_to_the_link_that_was_followed(service):
    a = service.create("https://example.com/a").json()["code"]
    b = service.create("https://example.com/b").json()["code"]

    service.visit(a)
    service.visit(a)
    service.visit(b)

    assert service.stats(a).json()["total_clicks"] == 2
    assert service.stats(b).json()["total_clicks"] == 1
    assert service.click_count() == 3


def test_the_clicks_table_has_no_column_able_to_hold_a_visitor_address(service):
    code = service.create("https://example.com/a").json()["code"]
    service.visit(code)

    for column in service.columns("clicks"):
        low = column.lower()
        assert "ip" not in low, column
        assert "addr" not in low, column
        assert "remote" not in low, column
        assert "client" not in low, column
