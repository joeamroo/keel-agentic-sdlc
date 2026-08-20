"""Creation rate limiting: per client address, per API key, 429 shape, no leaks.

Every test drives the limiter through the HTTP surface only, against a fresh
database and a freshly imported application (so the in-process bucket state
starts empty). Nothing sleeps: the window is never waited out, it is only ever
filled.
"""
import logging

from conftest import assert_error_envelope

CALLER = {"X-Forwarded-For": "203.0.113.10"}
OTHER = {"X-Forwarded-For": "198.51.100.7"}

ADDRESS_A = "203.0.113.10"
ADDRESS_B = "198.51.100.7"
ADDRESS_C = "203.0.113.200"


def _limited_app(app_factory, **extra):
    """Build an app with the limiter on, forwarded-for trusted, no keys by default."""
    env = {
        "LINKS_RATE_LIMIT_ENABLED": "true",
        "LINKS_TRUST_FORWARDED_FOR": "true",
        # Empty means "no key is recognised", i.e. today's behaviour. Set here so
        # an ambient SHORTENER_API_KEYS in the developer's shell cannot change a
        # result.
        "SHORTENER_API_KEYS": "",
    }
    env.update({k: str(v) for k, v in extra.items()})
    return app_factory(**env)


def _headers(address, key=None):
    headers = {"X-Forwarded-For": address}
    if key is not None:
        headers["X-API-Key"] = key
    return headers


def _retry_after_seconds(response):
    raw = response.headers.get("retry-after")
    assert raw is not None, "a 429 must carry Retry-After"
    assert raw.strip().isdigit(), (
        "Retry-After must be a whole number of seconds, got " + repr(raw)
    )
    return int(raw.strip())


# ---------------------------------------------------------------------------
# per-IP creation limiting (unchanged behaviour)
# ---------------------------------------------------------------------------
def test_creation_beyond_the_configured_limit_returns_429_with_retry_after_and_writes_no_row(app_factory):
    app = _limited_app(app_factory)

    for i in range(10):
        response = app.create("https://example.com/{0}".format(i), headers=CALLER)
        assert response.status_code == 201, (i, response.status_code, response.text[:200])

    blocked = app.create("https://example.com/eleven", headers=CALLER)

    assert blocked.status_code == 429, (
        "the 11th creation in the window must be refused, got {0}".format(blocked.status_code)
    )
    assert_error_envelope(blocked)
    retry_after = blocked.headers.get("retry-after")
    assert retry_after is not None, "a 429 must carry Retry-After"
    assert int(retry_after) >= 1, retry_after
    assert len(app.link_rows()) == 10, "the throttled request must not have written a row"


def test_the_limit_is_per_client_key_so_another_address_still_gets_201(app_factory):
    app = _limited_app(app_factory)
    for i in range(10):
        assert app.create("https://example.com/{0}".format(i), headers=CALLER).status_code == 201
    assert app.create("https://example.com/over", headers=CALLER).status_code == 429

    other = app.create("https://example.com/other-client", headers=OTHER)

    assert other.status_code == 201, (
        "a different client address must not be locked out by the first one's burst: "
        + other.text[:200]
    )
    assert len(app.link_rows()) == 11


def test_links_rate_limit_max_is_read_from_the_environment_rather_than_hard_coded(app_factory):
    app = _limited_app(app_factory, LINKS_RATE_LIMIT_MAX="2")

    first = app.create("https://example.com/1", headers=CALLER)
    second = app.create("https://example.com/2", headers=CALLER)
    third = app.create("https://example.com/3", headers=CALLER)

    assert (first.status_code, second.status_code) == (201, 201)
    assert third.status_code == 429, third.text[:200]
    assert len(app.link_rows()) == 2


def test_the_limiter_can_be_disabled_with_links_rate_limit_enabled_false(app_factory):
    app = app_factory(LINKS_RATE_LIMIT_ENABLED="false", LINKS_TRUST_FORWARDED_FOR="true")

    for i in range(15):
        response = app.create("https://example.com/{0}".format(i), headers=CALLER)
        assert response.status_code == 201, (i, response.text[:200])

    assert len(app.link_rows()) == 15


def test_exercising_the_limiter_writes_no_client_address_to_disk(app_factory):
    app = _limited_app(app_factory)
    address = "203.0.113.77"
    for i in range(12):
        app.create("https://example.com/{0}".format(i), headers={"X-Forwarded-For": address})

    blob = app.disk_bytes()

    assert address.encode() not in blob, "a client IP reached persistent storage"
    for table in ("links", "clicks"):
        for column in app.columns(table):
            low = column.lower()
            assert "ip" not in low and "addr" not in low and "remote" not in low, (
                "{0}.{1} looks like it can hold a client address".format(table, column)
            )


def test_with_no_api_keys_configured_the_per_ip_limit_behaves_exactly_as_before(app_factory, monkeypatch):
    monkeypatch.delenv("SHORTENER_API_KEYS", raising=False)
    app = app_factory(LINKS_RATE_LIMIT_ENABLED="true", LINKS_TRUST_FORWARDED_FOR="true")

    for i in range(10):
        response = app.create("https://example.com/{0}".format(i), headers=_headers(ADDRESS_A))
        assert response.status_code == 201, (i, response.text[:200])

    keyless = app.create("https://example.com/eleven", headers=_headers(ADDRESS_A))
    keyed = app.create("https://example.com/twelve", headers=_headers(ADDRESS_A, "alpha"))

    assert keyless.status_code == 429, keyless.text[:200]
    assert _retry_after_seconds(keyless) >= 1
    assert keyed.status_code == 429, (
        "with no keys configured an X-API-Key header must buy nothing, got "
        + str(keyed.status_code)
    )
    assert len(app.link_rows()) == 10


# ---------------------------------------------------------------------------
# per-key quotas
# ---------------------------------------------------------------------------
def test_a_recognised_key_spends_its_own_quota_of_one_hundred_and_is_refused_on_the_hundred_and_first(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:100,beta:20")

    statuses = []
    for i in range(100):
        statuses.append(
            app.create(
                "https://example.com/{0}".format(i), headers=_headers(ADDRESS_A, "alpha")
            ).status_code
        )

    assert statuses == [201] * 100, (
        "alpha's quota is 100, but the first refusal came at request "
        + str(statuses.index(429) + 1 if 429 in statuses else "n/a")
    )
    blocked = app.create("https://example.com/101", headers=_headers(ADDRESS_A, "alpha"))
    assert blocked.status_code == 429, blocked.text[:200]
    assert len(app.link_rows()) == 100


def test_the_second_key_is_held_to_its_own_smaller_quota_value(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:100,beta:20")

    for i in range(20):
        response = app.create(
            "https://example.com/b{0}".format(i), headers=_headers(ADDRESS_A, "beta")
        )
        assert response.status_code == 201, (i, response.text[:200])

    blocked = app.create("https://example.com/b21", headers=_headers(ADDRESS_A, "beta"))

    assert blocked.status_code == 429, (
        "beta's quota is 20, the 21st must be refused, got " + str(blocked.status_code)
    )
    assert assert_error_envelope(blocked)["code"] == "rate_limited"
    assert len(app.link_rows()) == 20


def test_a_per_key_429_carries_the_rate_limited_envelope_a_bounded_retry_after_and_writes_no_row(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:2")
    for i in range(2):
        assert app.create(
            "https://example.com/{0}".format(i), headers=_headers(ADDRESS_A, "alpha")
        ).status_code == 201

    blocked = app.create("https://example.com/third", headers=_headers(ADDRESS_A, "alpha"))

    assert blocked.status_code == 429, blocked.text[:200]
    error = assert_error_envelope(blocked)
    assert error["code"] == "rate_limited", error
    seconds = _retry_after_seconds(blocked)
    assert 1 <= seconds <= 60, (
        "Retry-After must be within the 60 second window, got " + str(seconds)
    )
    assert len(app.link_rows()) == 2, "the throttled request wrote a links row"
    assert app.click_rows() == []


def test_retry_after_never_exceeds_the_configured_window_length(app_factory):
    app = _limited_app(
        app_factory, SHORTENER_API_KEYS="alpha:1", LINKS_RATE_LIMIT_WINDOW_SECONDS="5"
    )
    assert app.create("https://example.com/1", headers=_headers(ADDRESS_A, "alpha")).status_code == 201

    blocked = app.create("https://example.com/2", headers=_headers(ADDRESS_A, "alpha"))

    assert blocked.status_code == 429, blocked.text[:200]
    seconds = _retry_after_seconds(blocked)
    assert 1 <= seconds <= 5, (
        "with a 5 second window Retry-After must be in 1..5, got " + str(seconds)
    )


def test_exhausting_one_key_leaves_the_other_key_usable_from_the_same_address(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:100,beta:20")
    for i in range(100):
        assert app.create(
            "https://example.com/a{0}".format(i), headers=_headers(ADDRESS_A, "alpha")
        ).status_code == 201
    assert app.create(
        "https://example.com/a-over", headers=_headers(ADDRESS_A, "alpha")
    ).status_code == 429

    beta = app.create("https://example.com/b1", headers=_headers(ADDRESS_A, "beta"))

    assert beta.status_code == 201, (
        "buckets must be keyed by API key, not by address: beta was refused after "
        "alpha exhausted its quota from the same address (" + beta.text[:200] + ")"
    )
    assert len(app.link_rows()) == 101


def test_key_limited_creations_never_consume_the_per_ip_creation_budget(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:100,beta:20")
    for i in range(15):
        assert app.create(
            "https://example.com/a{0}".format(i), headers=_headers(ADDRESS_A, "alpha")
        ).status_code == 201

    for i in range(10):
        keyless = app.create("https://example.com/k{0}".format(i), headers=_headers(ADDRESS_A))
        assert keyless.status_code == 201, (
            "the anonymous budget for this address was eaten by alpha's traffic: "
            + keyless.text[:200]
        )

    assert app.create("https://example.com/k10", headers=_headers(ADDRESS_A)).status_code == 429
    assert len(app.link_rows()) == 25


def test_an_unrecognised_key_is_counted_in_the_shared_per_ip_bucket(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:100,beta:20")
    for i in range(10):
        assert app.create(
            "https://example.com/{0}".format(i), headers=_headers(ADDRESS_A)
        ).status_code == 201

    gamma = app.create("https://example.com/gamma", headers=_headers(ADDRESS_A, "gamma"))

    assert gamma.status_code == 429, (
        "an invented key must not open a fresh quota, got " + str(gamma.status_code)
    )
    assert assert_error_envelope(gamma)["code"] == "rate_limited"
    assert len(app.link_rows()) == 10


def test_a_thousand_invented_keys_from_one_address_are_throttled_like_keyless_requests(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:100,beta:20")

    statuses = []
    for i in range(1000):
        statuses.append(
            app.create(
                "https://example.com/{0}".format(i),
                headers=_headers(ADDRESS_A, "invented-{0}".format(i)),
            ).status_code
        )

    assert statuses[:10] == [201] * 10, statuses[:12]
    assert set(statuses[10:]) == {429}, (
        "attacker-invented keys must all share the per-IP bucket; saw statuses "
        + repr(sorted(set(statuses[10:])))
    )
    assert len(app.link_rows()) == 10
    assert app.create("https://example.com/keyless", headers=_headers(ADDRESS_A)).status_code == 429


# ---------------------------------------------------------------------------
# routes that must ignore the header entirely
# ---------------------------------------------------------------------------
def test_redirects_are_never_limited_by_api_key_even_when_that_key_has_quota_one(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:1")
    created = app.create("https://example.com/target", headers=_headers(ADDRESS_A, "alpha"))
    assert created.status_code == 201, created.text[:200]
    code = created.json()["code"]
    assert app.create(
        "https://example.com/second", headers=_headers(ADDRESS_A, "alpha")
    ).status_code == 429, "alpha's quota of 1 was not enforced on creation"

    for attempt in range(5):
        response = app.visit(code, headers=_headers(ADDRESS_A, "alpha"))
        assert response.status_code == 307, (
            "visit {0} was limited by the API key bucket: {1}".format(
                attempt + 1, response.status_code
            )
        )
        assert response.headers["location"] == "https://example.com/target"

    assert len(app.click_rows()) == 5


def test_stats_and_health_are_byte_identical_with_and_without_an_api_key(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:1")
    code = app.create("https://example.com/a", headers=_headers(ADDRESS_A, "alpha")).json()["code"]
    app.visit(code, headers=_headers(ADDRESS_A))

    path = "/api/links/{0}/stats".format(code)
    plain_stats = app.client.get(path, headers=_headers(ADDRESS_A))
    keyed_stats = app.client.get(path, headers=_headers(ADDRESS_A, "alpha"))
    plain_health = app.client.get("/health", headers=_headers(ADDRESS_A))
    keyed_health = app.client.get("/health", headers=_headers(ADDRESS_A, "alpha"))

    assert plain_stats.status_code == 200 and keyed_stats.status_code == 200
    assert keyed_stats.json() == plain_stats.json()
    assert keyed_stats.headers.get("content-type") == plain_stats.headers.get("content-type")
    assert "retry-after" not in {k.lower() for k in keyed_stats.headers.keys()}
    assert plain_health.status_code == 200 and keyed_health.status_code == 200
    assert keyed_health.json() == plain_health.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# configuration parsing
# ---------------------------------------------------------------------------
def test_the_master_switch_disables_per_key_quotas_as_well_as_per_ip_ones(app_factory):
    app = app_factory(
        LINKS_RATE_LIMIT_ENABLED="false",
        LINKS_TRUST_FORWARDED_FOR="true",
        SHORTENER_API_KEYS="alpha:2",
    )

    for i in range(15):
        response = app.create(
            "https://example.com/{0}".format(i), headers=_headers(ADDRESS_A, "alpha")
        )
        assert response.status_code == 201, (
            "request {0} was throttled although the limiter is disabled: {1}".format(
                i + 1, response.text[:200]
            )
        )

    assert len(app.link_rows()) == 15


def test_whitespace_padded_and_empty_entries_still_parse_to_the_two_quotas(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS=" alpha : 100 , beta:20 ,")

    for i in range(11):
        response = app.create(
            "https://example.com/a{0}".format(i), headers=_headers(ADDRESS_A, "alpha")
        )
        assert response.status_code == 201, (
            "' alpha : 100 ' must parse to a quota of 100; request {0} got {1}".format(
                i + 1, response.status_code
            )
        )

    for i in range(20):
        assert app.create(
            "https://example.com/b{0}".format(i), headers=_headers(ADDRESS_A, "beta")
        ).status_code == 201, "beta must have its own quota of 20"
    assert app.create(
        "https://example.com/b21", headers=_headers(ADDRESS_A, "beta")
    ).status_code == 429
    assert len(app.link_rows()) == 31


def test_malformed_key_entries_are_discarded_while_the_valid_entry_is_enforced(app_factory):
    app = _limited_app(
        app_factory, SHORTENER_API_KEYS="alpha:abc,noquota,:5,beta:0,gamma:7"
    )

    assert app.client.get("/health").status_code == 200, "a bad entry must not stop startup"

    for i in range(7):
        response = app.create(
            "https://example.com/g{0}".format(i), headers=_headers(ADDRESS_A, "gamma")
        )
        assert response.status_code == 201, (i, response.text[:200])
    blocked = app.create("https://example.com/g8", headers=_headers(ADDRESS_A, "gamma"))
    assert blocked.status_code == 429, (
        "gamma:7 is the only valid entry and must be enforced, got "
        + str(blocked.status_code)
    )


def test_a_key_whose_quota_does_not_parse_falls_back_to_the_per_ip_limit(app_factory):
    app = _limited_app(
        app_factory, SHORTENER_API_KEYS="alpha:abc,noquota,:5,beta:0,gamma:7"
    )

    for i in range(10):
        response = app.create(
            "https://example.com/a{0}".format(i), headers=_headers(ADDRESS_B, "alpha")
        )
        assert response.status_code == 201, (i, response.text[:200])

    keyless = app.create("https://example.com/after", headers=_headers(ADDRESS_B))

    assert keyless.status_code == 429, (
        "alpha:abc is unusable, so alpha's traffic must have been counted in the "
        "per-IP bucket; the keyless request got " + str(keyless.status_code)
    )
    assert len(app.link_rows()) == 10


def test_a_zero_quota_entry_is_skipped_rather_than_used_to_block_the_key(app_factory):
    app = _limited_app(
        app_factory, SHORTENER_API_KEYS="alpha:abc,noquota,:5,beta:0,gamma:7"
    )

    first = app.create("https://example.com/b0", headers=_headers(ADDRESS_C, "beta"))

    assert first.status_code == 201, (
        "beta:0 must be discarded, not treated as 'deny everything': got "
        + str(first.status_code)
    )
    for i in range(1, 10):
        assert app.create(
            "https://example.com/b{0}".format(i), headers=_headers(ADDRESS_C, "beta")
        ).status_code == 201
    assert app.create(
        "https://example.com/b10", headers=_headers(ADDRESS_C, "beta")
    ).status_code == 429, "beta should fall back to the 10 request per-IP budget"


# ---------------------------------------------------------------------------
# the 429 comes before parsing, and the key never lands anywhere
# ---------------------------------------------------------------------------
def test_a_per_key_429_is_returned_for_a_malformed_body_instead_of_a_422(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:2")
    for i in range(2):
        assert app.create(
            "https://example.com/{0}".format(i), headers=_headers(ADDRESS_A, "alpha")
        ).status_code == 201

    headers = _headers(ADDRESS_A, "alpha")
    headers["Content-Type"] = "application/json"
    blocked = app.client.post("/api/links", content=b"{not valid json", headers=headers)

    assert blocked.status_code == 429, (
        "the limiter must answer before the body is parsed, got {0}: {1}".format(
            blocked.status_code, blocked.text[:200]
        )
    )
    assert assert_error_envelope(blocked)["code"] == "rate_limited"
    assert _retry_after_seconds(blocked) >= 1
    assert len(app.link_rows()) == 2
    assert app.click_rows() == []


def test_the_api_key_value_never_reaches_the_database_files_or_debug_logs(app_factory, caplog):
    secret = "s3cret-alpha"
    app = _limited_app(
        app_factory,
        SHORTENER_API_KEYS=secret + ":3",
        LINKS_LOG_LEVEL="DEBUG",
        LOG_LEVEL="DEBUG",
    )
    caplog.set_level(logging.DEBUG)

    for i in range(4):
        app.create("https://example.com/{0}".format(i), headers=_headers(ADDRESS_A, secret))
    rows = app.link_rows()
    assert len(rows) == 3, "the key's quota of 3 should have admitted exactly 3 creations"
    app.visit(rows[0]["code"], headers=_headers(ADDRESS_A, secret))
    app.client.get("/health", headers={"X-API-Key": secret})

    assert secret.encode() not in app.disk_bytes(), "the API key reached persistent storage"
    assert secret not in caplog.text, "the API key was written to a log record at DEBUG"


def test_exercising_the_key_limiter_leaves_the_links_and_clicks_schema_untouched(app_factory):
    app = _limited_app(app_factory, SHORTENER_API_KEYS="alpha:3,beta:20")
    for i in range(3):
        assert app.create(
            "https://example.com/{0}".format(i), headers=_headers(ADDRESS_A, "alpha")
        ).status_code == 201
    assert app.create("https://example.com/over", headers=_headers(ADDRESS_A, "alpha")).status_code == 429
    app.visit(app.link_rows()[0]["code"], headers=_headers(ADDRESS_A, "alpha"))

    assert app.columns("links") == ["id", "code", "url", "created_at", "expires_at"]
    assert app.columns("clicks") == [
        "id",
        "link_id",
        "clicked_at",
        "referrer",
        "user_agent",
    ]
    names = {
        row["name"]
        for row in app._rows("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not row["name"].startswith("sqlite_")
    }
    assert names == {"links", "clicks"}, (
        "per-key limiting must not add a table; found " + repr(sorted(names))
    )
