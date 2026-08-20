"""Per-API-key creation quotas configured through SHORTENER_API_KEYS."""
import logging

import pytest

from conftest import (
    CLICK_COLUMNS,
    LINK_COLUMNS,
    assert_error,
    assert_retry_after,
)

TWO_KEYS = "alpha:100,beta:20"


def test_a_known_key_gets_its_own_quota_and_the_next_creation_is_429_with_no_row(app_factory):
    app = app_factory(
        SHORTENER_API_KEYS=TWO_KEYS,
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
    )

    for i in range(100):
        response = app.create("https://example.com/{0}".format(i), api_key="alpha")
        assert response.status_code == 201, (
            "creation {0} of 100 with X-API-Key: alpha (quota 100) returned {1}: "
            "{2}".format(i + 1, response.status_code, response.text[:200])
        )

    blocked = app.create("https://example.com/overflow", api_key="alpha")

    assert blocked.status_code == 429, (
        "the 101st keyed creation must be refused, got {0}: {1}".format(
            blocked.status_code, blocked.text[:200]
        )
    )
    assert assert_error(blocked)["code"] == "rate_limited"
    assert app.link_count() == 100, "the throttled keyed request wrote a links row"


def test_key_buckets_are_independent_so_exhausting_beta_leaves_alpha_usable(app_factory):
    app = app_factory(
        SHORTENER_API_KEYS=TWO_KEYS,
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
    )

    for i in range(20):
        response = app.create("https://example.com/b{0}".format(i), api_key="beta")
        assert response.status_code == 201, (i, response.status_code, response.text[:200])

    over = app.create("https://example.com/b-over", api_key="beta")
    assert over.status_code == 429, over.text[:200]
    assert assert_error(over)["code"] == "rate_limited"

    still_open = app.create("https://example.com/a-after-beta", api_key="alpha")
    assert still_open.status_code == 201, (
        "beta's exhausted bucket must not affect alpha: " + still_open.text[:200]
    )
    assert app.link_count() == 21


def test_a_known_key_is_not_also_charged_against_the_per_ip_creation_bucket(app_factory):
    app = app_factory(
        SHORTENER_API_KEYS="alpha:100",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="2",
    )

    for i in range(5):
        response = app.create("https://example.com/{0}".format(i), api_key="alpha")
        assert response.status_code == 201, (
            "request {0} from one address with a recognised key and "
            "LINKS_RATE_LIMIT_MAX=2 returned {1}: {2}".format(
                i + 1, response.status_code, response.text[:200]
            )
        )

    assert app.link_count() == 5


def test_an_unknown_key_falls_back_to_the_per_ip_limit_without_401_or_403(app_factory):
    app = app_factory(
        SHORTENER_API_KEYS="alpha:100",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
    )

    seen = []
    for i in range(10):
        response = app.create("https://example.com/{0}".format(i), api_key="gamma")
        seen.append(response.status_code)
        assert response.status_code == 201, (i, response.status_code, response.text[:200])

    eleventh = app.create("https://example.com/eleven", api_key="gamma")
    seen.append(eleventh.status_code)

    assert eleventh.status_code == 429, (
        "an unrecognised key must not buy extra budget, got {0}: {1}".format(
            eleventh.status_code, eleventh.text[:200]
        )
    )
    assert 401 not in seen and 403 not in seen, (
        "throttling must not turn into authentication: " + repr(seen)
    )
    assert app.link_count() == 10


def test_anonymous_creations_keep_the_legacy_per_ip_behaviour_when_keys_are_configured(
    app_factory,
):
    app = app_factory(
        SHORTENER_API_KEYS=TWO_KEYS,
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
    )

    for i in range(10):
        response = app.create("https://example.com/{0}".format(i))
        assert response.status_code == 201, (i, response.status_code, response.text[:200])

    blocked = app.create("https://example.com/eleven")

    assert blocked.status_code == 429, blocked.text[:200]
    assert assert_error(blocked)["code"] == "rate_limited"
    assert_retry_after(blocked, window=60)
    assert app.link_count() == 10


def test_a_keyed_429_carries_whole_second_retry_after_within_the_window_and_no_store(
    app_factory,
):
    app = app_factory(
        SHORTENER_API_KEYS="alpha:1",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
        LINKS_RATE_LIMIT_WINDOW_SECONDS="60",
    )
    assert app.create("https://example.com/first", api_key="alpha").status_code == 201

    blocked = app.create("https://example.com/second", api_key="alpha")

    assert blocked.status_code == 429, blocked.text[:200]
    raw = blocked.headers.get("retry-after")
    assert raw is not None and "." not in raw, repr(raw)
    assert_retry_after(blocked, window=60)


def test_an_exhausted_key_window_reopens_once_the_clock_advances(app_factory, limiter_clock):
    app = app_factory(
        SHORTENER_API_KEYS="alpha:1",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
        LINKS_RATE_LIMIT_WINDOW_SECONDS="60",
    )
    assert app.create("https://example.com/first", api_key="alpha").status_code == 201
    assert app.create("https://example.com/second", api_key="alpha").status_code == 429

    limiter_clock.advance(61)

    after = app.create("https://example.com/third", api_key="alpha")
    assert after.status_code == 201, (
        "the key window must reopen after LINKS_RATE_LIMIT_WINDOW_SECONDS, got "
        "{0}: {1}".format(after.status_code, after.text[:200])
    )
    assert app.link_count() == 2


def test_redirects_survive_an_exhausted_key_quota_and_use_the_per_ip_redirect_bucket(
    app_factory,
):
    app = app_factory(
        SHORTENER_API_KEYS="alpha:100",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="1",
    )
    target = "https://example.com/target"
    code = app.create_ok(target, api_key="alpha")

    for i in range(100):
        response = app.visit(code, api_key="alpha")
        assert response.status_code == 307, (
            "redirect {0} of 100 (bucket is LINKS_RATE_LIMIT_MAX * 100) returned "
            "{1}: {2}".format(i + 1, response.status_code, response.text[:200])
        )
        assert response.headers["location"] == target

    overflow = app.visit(code, api_key="alpha")
    assert overflow.status_code == 429, (
        "the redirect bucket must still apply at MAX * 100, got "
        + str(overflow.status_code)
    )


def test_stats_are_never_rate_limited_before_or_after_the_key_quota_is_spent(app_factory):
    app = app_factory(
        SHORTENER_API_KEYS="alpha:1",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="1",
    )
    code = app.create_ok("https://example.com/target", api_key="alpha")
    assert app.stats(code, api_key="alpha").status_code == 200
    assert app.create("https://example.com/second", api_key="alpha").status_code == 429

    last = None
    for i in range(25):
        last = app.stats(code, api_key="alpha")
        assert last.status_code == 200, (
            "stats call {0} returned {1}: {2}".format(i + 1, last.status_code, last.text[:200])
        )
    assert last.json()["code"] == code


def test_with_no_keys_configured_a_key_header_changes_nothing(app_factory):
    app = app_factory(LINKS_RATE_LIMIT_ENABLED="true", LINKS_RATE_LIMIT_MAX="10")

    for i in range(10):
        response = app.create("https://example.com/{0}".format(i), api_key="alpha")
        assert response.status_code == 201, (i, response.status_code, response.text[:200])

    blocked = app.create("https://example.com/eleven", api_key="alpha")

    assert blocked.status_code == 429, blocked.text[:200]
    assert assert_error(blocked)["code"] == "rate_limited"
    assert app.link_count() == 10


MALFORMED = "alpha,beta:abc,gamma:0,:5,delta:20"


def test_a_partly_malformed_key_list_still_honours_the_one_valid_quota(app_factory):
    app = app_factory(
        SHORTENER_API_KEYS=MALFORMED,
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
    )

    for i in range(20):
        response = app.create(
            "https://example.com/d{0}".format(i), api_key="delta", host="203.0.113.31"
        )
        assert response.status_code == 201, (
            "delta creation {0} returned {1}: {2}".format(
                i + 1, response.status_code, response.text[:200]
            )
        )

    over = app.create("https://example.com/d-over", api_key="delta", host="203.0.113.31")
    assert over.status_code == 429, over.text[:200]
    assert "beta:abc" not in over.text and "delta:20" not in over.text, over.text[:200]


@pytest.mark.parametrize(
    "key,host",
    [("alpha", "203.0.113.41"), ("beta", "203.0.113.42"), ("gamma", "203.0.113.43")],
)
def test_an_unparsable_key_entry_falls_back_to_the_per_ip_limit(app_factory, key, host):
    app = app_factory(
        SHORTENER_API_KEYS=MALFORMED,
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
    )

    for i in range(10):
        response = app.create("https://example.com/{0}".format(i), api_key=key, host=host)
        assert response.status_code == 201, (key, i, response.status_code, response.text[:200])

    blocked = app.create("https://example.com/over", api_key=key, host=host)

    assert blocked.status_code == 429, (
        "{0!r} is not a valid quota entry and must fall back to the per-IP limit, "
        "got {1}".format(key, blocked.status_code)
    )
    assert blocked.status_code not in (401, 403)


def test_whitespace_around_key_names_and_quotas_is_trimmed(app_factory):
    app = app_factory(
        SHORTENER_API_KEYS=" alpha : 100 , beta:20 ",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="2",
    )

    for i in range(5):
        response = app.create("https://example.com/{0}".format(i), api_key="alpha")
        assert response.status_code == 201, (
            "request {0} with a whitespace-padded key entry returned {1}: {2}".format(
                i + 1, response.status_code, response.text[:200]
            )
        )

    assert app.link_count() == 5


def test_key_matching_is_case_sensitive_so_uppercase_falls_back_to_the_ip_limit(app_factory):
    app = app_factory(
        SHORTENER_API_KEYS="alpha:100",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="2",
    )

    first = app.create("https://example.com/1", api_key="ALPHA")
    second = app.create("https://example.com/2", api_key="ALPHA")
    third = app.create("https://example.com/3", api_key="ALPHA")

    assert (first.status_code, second.status_code) == (201, 201)
    assert third.status_code == 429, (
        "'ALPHA' is not 'alpha' and must not inherit its quota, got "
        + str(third.status_code)
    )
    assert app.link_count() == 2


def test_disabling_the_limiter_also_disables_key_quotas(app_factory):
    app = app_factory(SHORTENER_API_KEYS="alpha:1", LINKS_RATE_LIMIT_ENABLED="false")

    for i in range(15):
        response = app.create("https://example.com/{0}".format(i), api_key="alpha")
        assert response.status_code == 201, (i, response.status_code, response.text[:200])

    assert app.link_count() == 15


def test_the_key_limiter_adds_no_table_and_writes_no_key_or_address_to_disk(app_factory):
    host = "203.0.113.55"
    app = app_factory(
        SHORTENER_API_KEYS="alpha:100",
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
    )
    codes = []
    for i in range(12):
        response = app.create("https://example.com/{0}".format(i), api_key="alpha", host=host)
        if response.status_code == 201:
            codes.append(response.json()["code"])
    for code in codes[:3]:
        app.visit(code, api_key="alpha", host=host)

    assert app.columns("links") == LINK_COLUMNS, app.columns("links")
    assert app.columns("clicks") == CLICK_COLUMNS, app.columns("clicks")
    assert set(app.tables()) - {"sqlite_sequence"} == {"links", "clicks"}, app.tables()

    blob = app.disk_bytes()
    assert b"alpha" not in blob, "an API key value reached persistent storage"
    assert host.encode() not in blob, "a client IP reached persistent storage"


def test_a_keyed_429_never_echoes_the_key_in_its_body_headers_or_debug_logs(
    app_factory, caplog
):
    secret = "s3cret-partner-key"
    caplog.set_level(logging.DEBUG)
    app = app_factory(
        SHORTENER_API_KEYS="{0}:1".format(secret),
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="10",
        LINKS_LOG_LEVEL="DEBUG",
    )
    assert app.create("https://example.com/first", api_key=secret).status_code == 201

    blocked = app.create("https://example.com/second", api_key=secret)

    assert blocked.status_code == 429, blocked.text[:200]
    assert secret not in blocked.text, "the 429 body echoed the API key"
    for name, value in blocked.headers.items():
        assert secret not in value, "header {0} echoed the API key".format(name)
    assert secret not in caplog.text, "a DEBUG log record contained the API key"
