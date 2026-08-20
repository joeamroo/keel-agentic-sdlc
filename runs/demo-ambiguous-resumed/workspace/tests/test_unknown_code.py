"""Unknown and malformed codes are dead ends that create nothing."""


def test_unknown_code_returns_404_with_no_location_and_creates_nothing(client, db, helpers):
    resp = client.get("/aB3xyz9", follow_redirects=False)

    assert resp.status_code == 404, f"unknown code answered {resp.status_code}: {resp.text}"
    assert "location" not in {k.lower() for k in resp.headers.keys()}, dict(resp.headers)
    assert db.count() == 0, "a lookup for an unissued code wrote a row"
    assert helpers.error_code(resp) in {"not_found", None} or resp.status_code == 404


def test_unknown_code_is_distinguishable_from_an_expired_one(client, create_link, db, helpers):
    code = helpers.code_of(create_link(client, "https://example.com/a").json())
    db.set_expiry(code, "2000-01-01T00:00:00Z")

    expired = client.get(f"/{code}", follow_redirects=False)
    missing = client.get("/aB3xyz9", follow_redirects=False)

    assert expired.status_code == 410, expired.text
    assert missing.status_code == 404, missing.text
    assert expired.status_code != missing.status_code


def test_code_outside_the_base62_alphabet_is_refused_without_a_datastore_error(client, db):
    for bad in ("!!!!!!!", "abc def", "%27%20OR%201%3D1", "..%2f..%2fetc"):
        resp = client.get(f"/{bad}", follow_redirects=False)
        assert resp.status_code in (400, 404), f"{bad!r} -> {resp.status_code}: {resp.text[:200]}"
        assert "location" not in {k.lower() for k in resp.headers.keys()}
    assert db.count() == 0


def test_unknown_code_body_is_json_and_echoes_no_markup(client):
    resp = client.get("/%3Cscript%3E", follow_redirects=False)

    assert resp.status_code in (400, 404), resp.text
    assert "<script>" not in resp.text
