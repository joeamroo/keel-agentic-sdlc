"""Creation rate limiting is per client key, returns 429, and persists no IP."""
from conftest import assert_error_envelope

CALLER = {"X-Forwarded-For": "203.0.113.10"}
OTHER = {"X-Forwarded-For": "198.51.100.7"}


def _limited_app(app_factory, **extra):
    return app_factory(
        LINKS_RATE_LIMIT_ENABLED="true", LINKS_TRUST_FORWARDED_FOR="true", **extra
    )


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
