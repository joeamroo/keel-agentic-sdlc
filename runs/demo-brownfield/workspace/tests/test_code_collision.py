"""Short code collisions are retried, bounded, and never hang or 500."""
import re

from conftest import assert_error

CODE_RE = re.compile(r"^[A-Za-z0-9]{7}$")
FRESH = "Kq7Xm2P"


def test_a_generator_returning_an_existing_code_is_retried_until_it_is_unique(
    app, scripted_codes
):
    taken = app.create_ok("https://example.com/first")
    assert taken != FRESH

    stub = scripted_codes([taken, taken, FRESH])
    second = app.create("https://example.com/second")

    assert second.status_code == 201, (
        "a UNIQUE collision must be retried, not surfaced as {0}: {1}".format(
            second.status_code, second.text[:200]
        )
    )
    assert stub.calls == 3, (
        "the generator was consulted {0} time(s); the retry loop did not run "
        "exactly once per collision".format(stub.calls)
    )
    code = second.json()["code"]
    assert code == FRESH, code
    assert CODE_RE.match(code), code

    stored = sorted(row["code"] for row in app.link_rows())
    assert stored == sorted([taken, FRESH])
    assert len(set(stored)) == 2, "the collision produced a duplicate code"


def test_after_a_retry_each_link_still_redirects_to_its_own_destination(app, scripted_codes):
    taken = app.create_ok("https://example.com/first")
    scripted_codes([taken, FRESH])

    second = app.create("https://example.com/second")
    assert second.status_code == 201, second.text[:200]

    assert app.visit(taken).headers["location"] == "https://example.com/first"
    assert app.visit(second.json()["code"]).headers["location"] == "https://example.com/second"


def test_a_permanently_colliding_generator_gives_up_with_503_after_bounded_attempts(
    app_factory, scripted_codes
):
    app = app_factory(LINKS_CODE_MAX_ATTEMPTS="3")
    taken = app.create_ok("https://example.com/first")

    stub = scripted_codes([taken] * 50)
    response = app.create("https://example.com/second")

    assert stub.calls == 3, (
        "LINKS_CODE_MAX_ATTEMPTS=3 must bound the retry loop at 3 attempts, saw "
        "{0}".format(stub.calls)
    )
    assert response.status_code == 503, (
        "an unresolvable collision must fail with 503, not {0}: {1}".format(
            response.status_code, response.text[:200]
        )
    )
    error = assert_error(response)
    assert error["code"] == "code_generation_failed", error
    assert app.link_count() == 1, "the failed creation wrote a row anyway"
