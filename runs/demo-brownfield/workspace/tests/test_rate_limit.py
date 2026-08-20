"""Per-address creation throttling: 429, Retry-After, per-key isolation."""
from conftest import DEFAULT_HOST, OTHER_HOST, assert_error, assert_retry_after


def test_creation_beyond_links_rate_limit_max_returns_429_and_writes_no_row(app_factory):
    app = app_factory(LINKS_RATE_LIMIT_ENABLED="true", LINKS_RATE_LIMIT_MAX="10")

    for i in range(10):
        response = app.create("https://example.com/{0}".format(i), host=DEFAULT_HOST)
        assert response.status_code == 201, (i, response.status_code, response.text[:200])

    blocked = app.create("https://example.com/eleven", host=DEFAULT_HOST)

    assert blocked.status_code == 429, (
        "the 11th creation in the window must be refused, got {0}: {1}".format(
            blocked.status_code, blocked.text[:200]
        )
    )
    assert assert_error(blocked)["code"] == "rate_limited"
    assert_retry_after(blocked, window=60)
    assert app.link_count() == 10, "the throttled request wrote a links row"


def test_the_creation_limit_is_per_key_so_a_second_address_still_gets_201(app_factory):
    app = app_factory(LINKS_RATE_LIMIT_ENABLED="true", LINKS_RATE_LIMIT_MAX="10")
    for i in range(10):
        assert app.create("https://example.com/{0}".format(i), host=DEFAULT_HOST).status_code == 201
    assert app.create("https://example.com/over", host=DEFAULT_HOST).status_code == 429

    other = app.create("https://example.com/other-caller", host=OTHER_HOST)

    assert other.status_code == 201, (
        "a different client address must not inherit the first one's exhausted "
        "bucket: " + other.text[:200]
    )
    assert app.link_count() == 11


def test_links_rate_limit_max_is_read_from_the_environment(app_factory):
    app = app_factory(LINKS_RATE_LIMIT_ENABLED="true", LINKS_RATE_LIMIT_MAX="2")

    first = app.create("https://example.com/1")
    second = app.create("https://example.com/2")
    third = app.create("https://example.com/3")

    assert (first.status_code, second.status_code) == (201, 201)
    assert third.status_code == 429, third.text[:200]
    assert app.link_count() == 2


def test_the_window_reopens_after_links_rate_limit_window_seconds_without_sleeping(
    app_factory, limiter_clock
):
    app = app_factory(
        LINKS_RATE_LIMIT_ENABLED="true",
        LINKS_RATE_LIMIT_MAX="2",
        LINKS_RATE_LIMIT_WINDOW_SECONDS="60",
    )
    assert app.create("https://example.com/1").status_code == 201
    assert app.create("https://example.com/2").status_code == 201
    assert app.create("https://example.com/3").status_code == 429

    limiter_clock.advance(61)

    after = app.create("https://example.com/4")
    assert after.status_code == 201, (
        "the fixed window must reopen once it has elapsed, got {0}: {1}".format(
            after.status_code, after.text[:200]
        )
    )
    assert app.link_count() == 3


def test_a_throttled_creation_does_not_block_redirects(app_factory):
    app = app_factory(LINKS_RATE_LIMIT_ENABLED="true", LINKS_RATE_LIMIT_MAX="1")
    code = app.create_ok("https://example.com/target")
    assert app.create("https://example.com/second").status_code == 429

    followed = app.visit(code)

    assert followed.status_code == 307, followed.text[:200]
    assert followed.headers["location"] == "https://example.com/target"


def test_setting_links_rate_limit_enabled_false_turns_the_limiter_off(app_factory):
    app = app_factory(LINKS_RATE_LIMIT_ENABLED="false", LINKS_RATE_LIMIT_MAX="2")

    for i in range(15):
        response = app.create("https://example.com/{0}".format(i))
        assert response.status_code == 201, (i, response.text[:200])

    assert app.link_count() == 15


def test_the_limiter_persists_no_client_address_to_disk(app_factory):
    app = app_factory(LINKS_RATE_LIMIT_ENABLED="true", LINKS_RATE_LIMIT_MAX="10")
    address = "203.0.113.77"
    for i in range(12):
        app.create("https://example.com/{0}".format(i), host=address)

    blob = app.disk_bytes()

    assert address.encode() not in blob, "a client IP reached persistent storage"
    for table in ("links", "clicks"):
        for column in app.columns(table):
            low = column.lower()
            assert "ip" not in low and "addr" not in low and "remote" not in low, (
                "{0}.{1} looks like it can hold a client address".format(table, column)
            )
