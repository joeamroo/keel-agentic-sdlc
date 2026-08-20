"""A generated code that already exists must be retried, not surfaced as an error."""

import random
import secrets

NOVEL_CODE = "Zq7Wm3x"  # 7 base62 characters, matching LINKS_CODE_LENGTH=7


def _run_rng_fallback(make_client, create_link, link_db, helpers, monkeypatch, tmp_path):
    """No named generator seam: drive the RNG so code #2's first attempt collides."""
    scripted = helpers.ScriptedRandom(code_len=7)
    for module in (secrets, random):
        monkeypatch.setattr(module, "choice", scripted.choice, raising=False)
    monkeypatch.setattr(secrets, "randbelow", scripted.randbelow, raising=False)
    monkeypatch.setattr(secrets, "token_bytes", scripted.token_bytes, raising=False)
    monkeypatch.setattr(secrets, "token_hex", scripted.token_hex, raising=False)
    monkeypatch.setattr(secrets, "token_urlsafe", scripted.token_urlsafe, raising=False)

    db_path = tmp_path / "collision.db"
    client = make_client(LINKS_DB_PATH=str(db_path))
    db = link_db(str(db_path))

    first = create_link(client, "https://example.com/one")
    assert first.status_code == 201, first.text
    second = create_link(client, "https://example.com/two")

    assert scripted.calls > 0, (
        "no code-generation seam was found: expose a generate_code()-style function or "
        "derive codes from secrets.choice/randbelow/token_* so a collision can be forced"
    )
    assert second.status_code == 201, (
        f"a colliding code produced {second.status_code} instead of a retry: {second.text}"
    )
    code1 = helpers.code_of(first.json())
    code2 = helpers.code_of(second.json())
    assert code1 and code2
    assert code1 != code2, "the service returned an already-issued code"
    assert db.count() == 2
    assert {r["code"] for r in db.rows()} == {code1, code2}


def test_a_colliding_generated_code_is_retried_and_a_unique_code_is_returned(
    client, make_client, create_link, db, link_db, helpers, monkeypatch, tmp_path
):
    first = create_link(client, "https://example.com/one")
    assert first.status_code == 201, first.text
    existing = helpers.code_of(first.json())

    targets = helpers.find_code_generators()
    if not targets:
        _run_rng_fallback(make_client, create_link, link_db, helpers, monkeypatch, tmp_path)
        return

    state = {"n": 0}

    def _make_fake(orig):
        def _fake(*args, **kwargs):
            state["n"] += 1
            if state["n"] == 1:
                return existing
            if state["n"] == 2:
                return NOVEL_CODE
            return orig(*args, **kwargs)

        return _fake

    for module, attr in targets:
        monkeypatch.setattr(module, attr, _make_fake(getattr(module, attr)))

    second = create_link(client, "https://example.com/two")

    if state["n"] == 0:
        # the discovered name was never called; fall back to driving the RNG
        monkeypatch.undo()
        _run_rng_fallback(make_client, create_link, link_db, helpers, monkeypatch, tmp_path)
        return

    assert second.status_code == 201, (
        f"a forced code collision produced {second.status_code} instead of a retry: {second.text}"
    )
    assert state["n"] >= 2, "the service did not retry after the UNIQUE collision"
    assert helpers.code_of(second.json()) == NOVEL_CODE
    assert db.count() == 2, f"expected both links persisted, got {db.rows()}"
    assert {r["code"] for r in db.rows()} == {existing, NOVEL_CODE}

    for code in (existing, NOVEL_CODE):
        redirect = client.get(f"/{code}", follow_redirects=False)
        assert redirect.status_code == 302, f"{code} did not resolve: {redirect.status_code}"
