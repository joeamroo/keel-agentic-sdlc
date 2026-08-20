"""Rate limiting: per-IP budget, per-key quotas from SHORTENER_API_KEYS.

No test sleeps: every limiter test drives the limit inside one window.
"""
from _pipeline_support import (  # noqa: F401 - fixtures are imported by name
    API_KEY_HEADER,
    assert_error_envelope,
    make_app,
    service,
    stub_dns,
)

ON = {"LINKS_RATE_LIMIT_ENABLED": "true"}


def _url(i):
    return "https://example.com/target-{0}".format(i)


def _burst(svc, count, api_key=None, start=0):
    statuses = []
    for i in range(start, start + count):
        if api_key is None:
            response = svc.create(_url(i))
        else:
            response = svc.create(_url(i), api_key=api_key)
        statuses.append(response.status_code)
    return statuses


def _retry_after_seconds(response):
    raw = response.headers.get("retry-after")
    assert raw is not None, "a 429 must carry a Retry-After header"
    return int(raw)


# ------------------------------------------------------------ per-IP baseline
def test_the_eleventh_unkeyed_creation_is_refused_with_429_and_a_retry_after(make_app):
    svc = make_app(LINKS_RATE_LIMIT_MAX="10", **ON)

    assert _burst(svc, 10) == [201] * 10

    refused = svc.create(_url(10))

    assert refused.status_code == 429, (
        "the 11th creation in the window must be refused, got {0}: {1}".format(
            refused.status_code, refused.text[:200]
        )
    )
    error = assert_error_envelope(refused)
    assert error["code"] == "rate_limited", error
    assert _retry_after_seconds(refused) >= 1
    assert svc.link_count() == 10, "the throttled request must not have written a row"


# ------------------------------------------------------------- per-key quotas
def test_a_recognised_key_receives_its_full_declared_quota_of_one_hundred(make_app):
    svc = make_app(
        SHORTENER_API_KEYS="alpha:100,beta:20", LINKS_RATE_LIMIT_MAX="10", **ON
    )

    for i in range(100):
        response = svc.create(_url(i), api_key="alpha")
        assert response.status_code == 201, (
            "creation {0} of 100 with the recognised key alpha (quota 100, per-IP "
            "max 10) was refused with {1}: {2}".format(
                i + 1, response.status_code, response.text[:200]
            )
        )

    assert svc.link_count() == 100


def test_the_creation_past_a_keys_quota_is_refused_with_429_and_writes_no_row(make_app):
    svc = make_app(
        SHORTENER_API_KEYS="alpha:100,beta:20", LINKS_RATE_LIMIT_MAX="10", **ON
    )
    for i in range(100):
        assert svc.create(_url(i), api_key="alpha").status_code == 201, i

    refused = svc.create(_url(100), api_key="alpha")

    assert refused.status_code == 429, (
        "the 101st alpha creation must be refused, got {0}: {1}".format(
            refused.status_code, refused.text[:200]
        )
    )
    error = assert_error_envelope(refused)
    assert error["code"] == "rate_limited", error
    assert svc.link_count() == 100, "the refused creation wrote a links row"
    assert svc.click_count() == 0, "the refused creation wrote a clicks row"


def test_each_key_carries_its_own_quota_so_beta_stops_at_twenty(make_app):
    svc = make_app(
        SHORTENER_API_KEYS="alpha:100,beta:20", LINKS_RATE_LIMIT_MAX="10", **ON
    )

    assert _burst(svc, 20, api_key="beta") == [201] * 20

    refused = svc.create(_url(20), api_key="beta")

    assert refused.status_code == 429, (
        "beta's quota is 20, so the 21st must be refused, got {0}".format(
            refused.status_code
        )
    )
    assert assert_error_envelope(refused)["code"] == "rate_limited"
    assert svc.link_count() == 20


def test_an_exhausted_key_does_not_block_a_different_key_from_the_same_address(make_app):
    svc = make_app(SHORTENER_API_KEYS="alpha:2,beta:20", LINKS_RATE_LIMIT_MAX="10", **ON)
    assert _burst(svc, 2, api_key="alpha") == [201, 201]
    assert svc.create(_url(2), api_key="alpha").status_code == 429

    other = svc.create(_url(3), api_key="beta")

    assert other.status_code == 201, (
        "beta has its own quota and must not be blocked by alpha's exhaustion: "
        "{0}: {1}".format(other.status_code, other.text[:200])
    )
    assert svc.link_count() == 3


def test_an_exhausted_key_does_not_block_an_unkeyed_creation_from_the_same_address(make_app):
    svc = make_app(SHORTENER_API_KEYS="alpha:2", LINKS_RATE_LIMIT_MAX="10", **ON)
    assert _burst(svc, 2, api_key="alpha") == [201, 201]
    assert svc.create(_url(2), api_key="alpha").status_code == 429

    unkeyed = svc.create(_url(3))

    assert unkeyed.status_code == 201, (
        "the key bucket and the IP bucket are independent, got {0}: {1}".format(
            unkeyed.status_code, unkeyed.text[:200]
        )
    )


def test_keyed_successes_do_not_consume_the_per_ip_budget(make_app):
    svc = make_app(SHORTENER_API_KEYS="alpha:5", LINKS_RATE_LIMIT_MAX="10", **ON)

    assert _burst(svc, 5, api_key="alpha") == [201] * 5
    assert _burst(svc, 10, start=100) == [201] * 10, (
        "the five keyed creations were charged to the IP bucket as well"
    )

    refused = svc.create(_url(200))
    assert refused.status_code == 429
    assert svc.link_count() == 15


def test_an_unknown_key_is_limited_by_the_per_ip_budget_of_ten(make_app):
    svc = make_app(SHORTENER_API_KEYS="alpha:100", LINKS_RATE_LIMIT_MAX="10", **ON)

    assert _burst(svc, 10, api_key="gamma") == [201] * 10

    refused = svc.create(_url(10), api_key="gamma")

    assert refused.status_code == 429, (
        "an unknown key must fall back to the per-IP budget of 10, got {0}".format(
            refused.status_code
        )
    )
    assert assert_error_envelope(refused)["code"] == "rate_limited"
    assert svc.link_count() == 10


def test_a_blank_api_key_header_is_treated_as_no_key_at_all(make_app):
    svc = make_app(SHORTENER_API_KEYS="alpha:100", LINKS_RATE_LIMIT_MAX="3", **ON)

    assert svc.create(_url(0), api_key="").status_code == 201
    assert svc.create(_url(1), api_key="   ").status_code == 201
    assert svc.create(_url(2)).status_code == 201

    refused = svc.create(_url(3), api_key="  ")

    assert refused.status_code == 429, (
        "blank and absent keys share the per-IP budget of 3, got {0}".format(
            refused.status_code
        )
    )
    assert svc.link_count() == 3


def test_key_matching_is_case_sensitive_so_upper_case_alpha_gets_only_the_ip_budget(make_app):
    svc = make_app(SHORTENER_API_KEYS="alpha:100", LINKS_RATE_LIMIT_MAX="3", **ON)

    assert _burst(svc, 3, api_key="ALPHA") == [201] * 3

    refused = svc.create(_url(3), api_key="ALPHA")

    assert refused.status_code == 429, (
        "ALPHA must not match the declared key alpha, got {0}".format(
            refused.status_code
        )
    )
    assert svc.link_count() == 3


def test_a_key_limited_429_carries_retry_after_within_the_minute_and_no_store(make_app):
    svc = make_app(SHORTENER_API_KEYS="alpha:1", LINKS_RATE_LIMIT_MAX="10", **ON)
    assert svc.create(_url(0), api_key="alpha").status_code == 201

    refused = svc.create(_url(1), api_key="alpha")

    assert refused.status_code == 429, refused.text[:200]
    seconds = _retry_after_seconds(refused)
    assert 1 <= seconds <= 60, (
        "the key window is one minute, so Retry-After must be in [1, 60], got "
        + repr(seconds)
    )
    assert "no-store" in refused.headers.get("cache-control", "").lower(), (
        "a 429 must not be cached; Cache-Control was "
        + repr(refused.headers.get("cache-control"))
    )


def test_redirects_are_never_refused_because_a_key_is_over_its_creation_quota(make_app):
    svc = make_app(SHORTENER_API_KEYS="alpha:1", LINKS_RATE_LIMIT_MAX="10", **ON)
    created = svc.create(_url(0), api_key="alpha")
    assert created.status_code == 201, created.text[:200]
    code = created.json()["code"]
    assert svc.create(_url(1), api_key="alpha").status_code == 429

    for i in range(50):
        hop = svc.visit(code, api_key="alpha")
        assert hop.status_code == 307, (
            "redirect {0} of 50 with an over-quota key was refused with {1}".format(
                i + 1, hop.status_code
            )
        )

    assert svc.stats(code).json()["total_clicks"] == 50


# ------------------------------------------------------ SHORTENER_API_KEYS parsing
def test_junk_entries_do_not_stop_the_valid_ones_from_being_honoured(make_app):
    svc = make_app(
        SHORTENER_API_KEYS="alpha:100,broken,beta:notanumber,gamma:0, :5 ,delta:7",
        LINKS_RATE_LIMIT_MAX="2",
        **ON
    )

    assert svc.health().status_code == 200, "the service must boot with junk entries"
    assert _burst(svc, 7, api_key="delta") == [201] * 7, (
        "delta:7 survives the junk entries around it"
    )

    refused = svc.create(_url(7), api_key="delta")
    assert refused.status_code == 429, (
        "delta's quota is 7, so the 8th must be refused, got {0}".format(
            refused.status_code
        )
    )


def test_a_malformed_entry_leaves_its_key_on_the_per_ip_budget(make_app):
    svc = make_app(
        SHORTENER_API_KEYS="alpha:100,broken,beta:notanumber,gamma:0, :5 ,delta:7",
        LINKS_RATE_LIMIT_MAX="2",
        **ON
    )

    assert _burst(svc, 2, api_key="beta") == [201, 201]

    refused = svc.create(_url(2), api_key="beta")

    assert refused.status_code == 429, (
        "beta:notanumber is skipped, so beta gets the per-IP budget of 2, got "
        "{0}".format(refused.status_code)
    )
    assert svc.link_count() == 2


def test_whitespace_around_a_key_declaration_is_stripped(make_app):
    svc = make_app(SHORTENER_API_KEYS=" alpha : 100 ", LINKS_RATE_LIMIT_MAX="2", **ON)

    statuses = _burst(svc, 5, api_key="alpha")

    assert statuses == [201] * 5, (
        "' alpha : 100 ' must register alpha at quota 100, got " + repr(statuses)
    )


def test_the_last_declaration_of_a_duplicate_key_name_wins(make_app):
    svc = make_app(SHORTENER_API_KEYS="alpha:5,alpha:100", LINKS_RATE_LIMIT_MAX="2", **ON)

    assert svc.health().status_code == 200
    statuses = _burst(svc, 6, api_key="alpha")

    assert statuses == [201] * 6, (
        "the later alpha:100 must win over alpha:5, got " + repr(statuses)
    )


def test_disabling_the_limiter_ignores_key_quotas_entirely(make_app):
    svc = make_app(
        SHORTENER_API_KEYS="alpha:2",
        LINKS_RATE_LIMIT_ENABLED="false",
        LINKS_RATE_LIMIT_MAX="10",
    )

    statuses = _burst(svc, 15, api_key="alpha")

    assert statuses == [201] * 15, (
        "LINKS_RATE_LIMIT_ENABLED=false must disable the key limiter too, got "
        + repr(statuses)
    )
    assert svc.link_count() == 15


def test_the_key_window_stays_one_minute_when_the_ip_window_is_one_second(make_app):
    svc = make_app(
        SHORTENER_API_KEYS="alpha:2",
        LINKS_RATE_LIMIT_WINDOW_SECONDS="1",
        LINKS_RATE_LIMIT_MAX="10",
        **ON
    )

    assert _burst(svc, 2, api_key="alpha") == [201, 201]

    refused = svc.create(_url(2), api_key="alpha")

    assert refused.status_code == 429, (
        "the key window is a fixed minute, so a 1s IP window must not multiply "
        "alpha's quota; got {0}".format(refused.status_code)
    )
    assert svc.link_count() == 2


def test_stats_and_health_answer_normally_while_a_key_is_over_quota(make_app):
    svc = make_app(SHORTENER_API_KEYS="alpha:1", LINKS_RATE_LIMIT_MAX="10", **ON)
    code = svc.create(_url(0), api_key="alpha").json()["code"]
    assert svc.create(_url(1), api_key="alpha").status_code == 429

    stats = svc.stats(code, api_key="alpha")
    health = svc.health(api_key="alpha")

    assert stats.status_code == 200, stats.text[:200]
    assert stats.json()["code"] == code
    assert health.status_code == 200, health.text[:200]
    assert health.json()["status"] == "ok"
