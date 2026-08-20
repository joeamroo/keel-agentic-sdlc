"""Short codes are unpredictable, well formed and unique."""
import re

CODE_RE = re.compile("^[A-Za-z0-9]{7}$")
N = 300


def test_many_creations_yield_distinct_seven_character_base62_codes(app):
    codes = []
    for i in range(N):
        response = app.create("https://example.com/{0}".format(i))
        assert response.status_code == 201, (i, response.text[:200])
        codes.append(response.json()["code"])

    assert len(set(codes)) == N, "duplicate codes were handed out"
    bad = [c for c in codes if not CODE_RE.match(c)]
    assert bad == [], bad
    assert len(app.link_rows()) == N


def test_consecutive_codes_are_not_sequential_or_incrementable(app):
    codes = [app.create("https://example.com/{0}".format(i)).json()["code"] for i in range(30)]

    for previous, current in zip(codes, codes[1:]):
        assert previous[:-1] != current[:-1], (
            "consecutive codes {0!r} and {1!r} differ only in the last character".format(
                previous, current
            )
        )
    assert codes != sorted(codes), "codes came out in sorted order, which is guessable"


def test_links_code_length_is_read_from_the_environment(app_factory):
    app = app_factory(LINKS_CODE_LENGTH="10")

    code = app.create("https://example.com/a").json()["code"]

    assert re.match("^[A-Za-z0-9]{10}$", code), code
    assert app.visit(code).status_code == 307
