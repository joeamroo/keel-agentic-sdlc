"""Short code collisions are retried; they never 500 and never hang."""
import re

from _pipeline_support import (  # noqa: F401 - fixtures are imported by name
    assert_error_envelope,
    force_codes,
    make_app,
    service,
    stub_dns,
)

CODE_RE = re.compile(r"^[A-Za-z0-9]{7}$")


def test_a_generator_returning_an_existing_code_is_retried_until_it_is_unique(service, force_codes):
    first = service.create("https://example.com/first")
    assert first.status_code == 201, first.text[:200]
    taken = first.json()["code"]

    stub = force_codes(service, [taken, taken, "Zq7Xk2M"])
    second = service.create("https://example.com/second")

    assert stub.calls >= 2, (
        "the code generator was consulted {0} time(s): the service did not retry "
        "after the UNIQUE collision".format(stub.calls)
    )
    assert second.status_code == 201, (
        "a collision must be retried, not surfaced as {0}: {1}".format(
            second.status_code, second.text[:200]
        )
    )
    code = second.json()["code"]
    assert code != taken, "the second link was handed the first link's code"
    assert CODE_RE.match(code), code

    stored = sorted(row["code"] for row in service.link_rows())
    assert stored == sorted([taken, code])
    assert len(set(stored)) == 2


def test_both_links_still_resolve_to_their_own_destination_after_a_collision(service, force_codes):
    taken = service.create("https://example.com/first").json()["code"]
    force_codes(service, [taken, "Zq7Xk2M"])

    second = service.create("https://example.com/second")
    assert second.status_code == 201, second.text[:200]
    code = second.json()["code"]

    assert service.visit(taken).headers["location"] == "https://example.com/first"
    assert service.visit(code).headers["location"] == "https://example.com/second"


def test_a_permanently_colliding_generator_gives_up_with_503_after_a_bounded_retry(make_app, force_codes):
    svc = make_app(LINKS_CODE_MAX_ATTEMPTS="3")
    taken = svc.create("https://example.com/first").json()["code"]

    stub = force_codes(svc, [taken] * 200)
    response = svc.create("https://example.com/second")

    assert stub.calls >= 2, (
        "generator consulted {0} time(s); the bounded retry loop never ran".format(
            stub.calls
        )
    )
    assert stub.calls <= 20, (
        "the retry loop is unbounded ({0} attempts): a hostile collision would "
        "hang the request".format(stub.calls)
    )
    assert response.status_code == 503, (
        "exhausted attempts must give up with 503, got {0}: {1}".format(
            response.status_code, response.text[:200]
        )
    )
    error = assert_error_envelope(response)
    assert error["code"] == "code_generation_failed", error
    assert svc.link_count() == 1, "the failed creation must not have written a row"
