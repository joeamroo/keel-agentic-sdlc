"""Create a link, follow it, and pin the redirect contract exactly."""


def test_created_link_is_persisted_once_with_the_submitted_destination(client, create_link, db, helpers):
    destination = "https://example.com/a"

    resp = create_link(client, destination)

    assert resp.status_code == 201, resp.text
    assert resp.headers.get("content-type", "").startswith("application/json"), resp.headers
    body = resp.json()
    code = helpers.code_of(body)
    assert code, f"create response exposes no short code: {body}"
    assert helpers.CODE_RE.match(code), f"code {code!r} is not base62"
    assert len(code) == 7, f"LINKS_CODE_LENGTH is 7, got {code!r}"
    assert helpers.short_url_of(body) == f"https://short.example.com/{code}", body

    rows = db.rows()
    assert len(rows) == 1, f"expected exactly one persisted link row, got {rows}"
    assert db.destination_of(rows[0]) == destination
    assert rows[0].get("code") == code


def test_following_a_fresh_code_returns_302_with_location_equal_to_the_stored_destination(
    client, create_link, db, helpers
):
    destination = "https://example.com/a?x=1&y=2"
    code = helpers.code_of(create_link(client, destination).json())

    resp = client.get(f"/{code}", follow_redirects=False)

    assert resp.status_code == 302, f"expected 302 Found, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("location") == destination
    assert db.destination_of(db.row_for(code)) == destination


def test_redirect_response_sets_cache_control_no_store(client, create_link, helpers):
    code = helpers.code_of(create_link(client, "https://example.com/a").json())

    resp = client.get(f"/{code}", follow_redirects=False)

    assert resp.status_code == 302, resp.text
    assert "no-store" in resp.headers.get("cache-control", "").lower(), dict(resp.headers)


def test_two_consecutive_creations_return_different_unpredictable_codes(client, create_link, db, helpers):
    first = helpers.code_of(create_link(client, "https://example.com/a").json())
    second = helpers.code_of(create_link(client, "https://example.com/a").json())

    assert first and second
    assert first != second, "two creations reused the same code"
    assert abs(len(first) - len(second)) == 0
    # sequential integers rendered as codes would be adjacent in base62
    assert not (first.isdigit() and second.isdigit() and int(second) - int(first) == 1)
    assert db.count() == 2, "the same destination twice must produce two rows"
