"""Short code collisions are retried instead of blowing up or hanging."""
import re

from conftest import assert_error_envelope

CODE_RE = re.compile("^[A-Za-z0-9]{7}$")


def test_a_generator_that_repeats_an_existing_code_is_retried_until_it_is_unique(app, force_codes):
    first = app.create("https://example.com/first")
    assert first.status_code == 201, first.text
    taken = first.json()["code"]

    stub = force_codes(app, [taken, taken, "Zq7Xk2M"])
    second = app.create("https://example.com/second")

    assert stub.calls >= 2, (
        "the code generator was consulted {0} time(s): either the service did not "
        "retry after the UNIQUE collision, or the generator could not be "
        "intercepted by the test".format(stub.calls)
    )
    assert second.status_code == 201, (
        "a collision must be retried, not surfaced as {0}: {1}".format(
            second.status_code, second.text[:200]
        )
    )
    code = second.json()["code"]
    assert code != taken, "the second link reused the first link's code"
    assert CODE_RE.match(code), code

    stored = [row["code"] for row in app.link_rows()]
    assert sorted(stored) == sorted([taken, code])
    assert len(set(stored)) == 2


def test_the_retried_link_still_redirects_to_its_own_destination(app, force_codes):
    first = app.create("https://example.com/first")
    taken = first.json()["code"]
    force_codes(app, [taken, "Zq7Xk2M"])

    second = app.create("https://example.com/second")
    assert second.status_code == 201, second.text
    code = second.json()["code"]

    assert app.visit(taken).headers["location"] == "https://example.com/first"
    assert app.visit(code).headers["location"] == "https://example.com/second"


def test_exhausting_links_code_max_attempts_fails_with_503_not_500(app_factory, force_codes):
    app = app_factory(LINKS_CODE_MAX_ATTEMPTS="3")
    taken = app.create("https://example.com/first").json()["code"]

    stub = force_codes(app, [taken] * 50)
    response = app.create("https://example.com/second")

    assert stub.calls >= 2, (
        "generator consulted {0} time(s); the bounded retry loop never ran".format(stub.calls)
    )
    assert response.status_code == 503, (
        "a permanently colliding generator must give up with 503, got {0}: {1}".format(
            response.status_code, response.text[:200]
        )
    )
    error = assert_error_envelope(response)
    assert error["code"] == "code_generation_failed", error
    assert len(app.link_rows()) == 1, "the failed creation must not have written a row"
