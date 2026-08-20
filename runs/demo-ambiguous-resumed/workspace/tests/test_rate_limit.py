"""The creation rate limit is enforced in-process and keyed per client."""

KEY_A = "198.51.100.7"
KEY_B = "198.51.100.8"
RATE_ERRORS = {"rate_limited", "rate_limit_exceeded", "too_many_requests"}


def test_requests_past_the_window_limit_return_429_with_retry_after_and_write_no_row(
    make_client, create_link, db, helpers
):
    client = make_client(LINKS_RATE_LIMIT_MAX=3, LINKS_RATE_LIMIT_WINDOW_SECONDS=60,
                         LINKS_TRUST_PROXY_HEADER="true")
    headers = {"X-Forwarded-For": KEY_A}

    for i in range(3):
        resp = create_link(client, f"https://example.com/a{i}", headers=headers)
        assert resp.status_code == 201, f"request {i + 1} of 3 under the limit failed: {resp.text}"
    assert db.count() == 3

    blocked = create_link(client, "https://example.com/over", headers=headers)

    assert blocked.status_code == 429, (
        f"4th request with LINKS_RATE_LIMIT_MAX=3 returned {blocked.status_code}: {blocked.text}"
    )
    retry_after = blocked.headers.get("retry-after")
    assert retry_after is not None, f"429 carried no Retry-After: {dict(blocked.headers)}"
    assert retry_after.strip().isdigit(), f"Retry-After is not an integer: {retry_after!r}"
    assert 1 <= int(retry_after) <= 60, f"Retry-After {retry_after} outside the 60s window"
    assert helpers.error_code(blocked) in RATE_ERRORS, blocked.text
    assert db.count() == 3, "the throttled request still wrote a row"


def test_the_limit_is_per_client_key_so_a_different_key_still_creates(
    make_client, create_link, db, helpers
):
    client = make_client(LINKS_RATE_LIMIT_MAX=3, LINKS_RATE_LIMIT_WINDOW_SECONDS=60,
                         LINKS_TRUST_PROXY_HEADER="true")

    for i in range(3):
        assert create_link(
            client, f"https://example.com/a{i}", headers={"X-Forwarded-For": KEY_A}
        ).status_code == 201
    assert create_link(
        client, "https://example.com/over", headers={"X-Forwarded-For": KEY_A}
    ).status_code == 429

    other = create_link(client, "https://example.com/b", headers={"X-Forwarded-For": KEY_B})

    assert other.status_code == 201, (
        f"a second client key was throttled by the first key's bucket: "
        f"{other.status_code} {other.text}"
    )
    assert helpers.code_of(other.json())
    assert db.count() == 4

    # and the exhausted key is still blocked afterwards
    again = create_link(client, "https://example.com/still-over", headers={"X-Forwarded-For": KEY_A})
    assert again.status_code == 429, again.text
    assert db.count() == 4


def test_redirects_are_not_rate_limited(make_client, create_link, helpers):
    client = make_client(LINKS_RATE_LIMIT_MAX=2, LINKS_RATE_LIMIT_WINDOW_SECONDS=60,
                         LINKS_TRUST_PROXY_HEADER="true")
    headers = {"X-Forwarded-For": KEY_A}
    code = helpers.code_of(create_link(client, "https://example.com/a", headers=headers).json())

    for _ in range(10):
        resp = client.get(f"/{code}", headers=headers, follow_redirects=False)
        assert resp.status_code == 302, f"a shared link was throttled: {resp.status_code}"
