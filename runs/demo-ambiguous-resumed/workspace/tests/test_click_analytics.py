"""Click counters move exactly once per successful redirect and never on failures."""

PAST = "2000-01-01T00:00:00Z"


def _pump(client):
    return lambda: client.get("/0000000", follow_redirects=False)


def test_a_successful_redirect_increments_the_click_count_exactly_once(
    client, create_link, db, helpers
):
    code = helpers.code_of(create_link(client, "https://example.com/a").json())
    assert db.click_count_of(code) == 0, "a brand new link should start at zero clicks"

    resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 302, resp.text

    count = helpers.wait_until(lambda: db.click_count_of(code) or 0, _pump(client))
    assert count == 1, f"expected exactly one click after one redirect, got {count!r}"

    row = db.row_for(code)
    last = next((row[k] for k in ("last_clicked_at", "last_click_at") if k in row), "")
    assert last, "last_clicked_at was not stamped after a successful redirect"


def test_two_successful_redirects_increment_the_click_count_twice(
    client, create_link, db, helpers
):
    code = helpers.code_of(create_link(client, "https://example.com/a").json())

    assert client.get(f"/{code}", follow_redirects=False).status_code == 302
    helpers.wait_until(lambda: (db.click_count_of(code) or 0) >= 1, _pump(client))
    assert client.get(f"/{code}", follow_redirects=False).status_code == 302
    count = helpers.wait_until(lambda: (db.click_count_of(code) or 0) >= 2, _pump(client))

    assert db.click_count_of(code) == 2, f"expected 2 clicks, got {db.click_count_of(code)!r}"
    assert count


def test_an_expired_redirect_does_not_increment_the_click_count(client, create_link, db, helpers):
    live = helpers.code_of(create_link(client, "https://example.com/live").json())
    dead = helpers.code_of(create_link(client, "https://example.com/dead").json())
    db.set_expiry(dead, PAST)

    assert client.get(f"/{live}", follow_redirects=False).status_code == 302
    helpers.wait_until(lambda: (db.click_count_of(live) or 0) >= 1, _pump(client))

    expired = client.get(f"/{dead}", follow_redirects=False)
    assert expired.status_code == 410, expired.text
    helpers.wait_until(lambda: False, _pump(client), tries=5)

    assert db.click_count_of(dead) == 0, (
        f"a 410 redirect attempt counted as a click: {db.click_count_of(dead)!r}"
    )
    assert db.click_count_of(live) == 1, "the unrelated live link's counter drifted"


def test_a_404_lookup_does_not_touch_any_click_counter(client, create_link, db, helpers):
    code = helpers.code_of(create_link(client, "https://example.com/a").json())

    assert client.get("/aB3xyz9", follow_redirects=False).status_code == 404
    helpers.wait_until(lambda: False, _pump(client), tries=5)

    assert db.click_count_of(code) == 0
    assert db.count() == 1
