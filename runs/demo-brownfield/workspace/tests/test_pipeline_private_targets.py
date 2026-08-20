"""SSRF surface: loopback, private and link-local destinations are refused."""
import pytest

from _pipeline_support import (  # noqa: F401 - fixtures are imported by name
    assert_error_envelope,
    make_app,
    service,
    stub_dns,
)

BLOCKED = [
    ("cloud_metadata", "http://169.254.169.254/latest/meta-data/iam/"),
    ("link_local", "http://169.254.0.1/x"),
    ("loopback_v4", "http://127.0.0.1:8000/admin"),
    ("loopback_v4_alias", "http://127.9.9.9/x"),
    ("loopback_v6", "http://[::1]/x"),
    ("ipv4_mapped_loopback", "http://[::ffff:127.0.0.1]/x"),
    ("private_10", "http://10.0.0.5/x"),
    ("private_172", "http://172.16.0.5/x"),
    ("private_192", "http://192.168.1.1/x"),
    ("cgnat", "http://100.64.0.1/x"),
    ("unspecified", "http://0.0.0.0/x"),
    ("unique_local_v6", "http://[fd00::1]/x"),
    ("link_local_v6", "http://[fe80::1]/x"),
]


@pytest.mark.parametrize("target", [t for _, t in BLOCKED], ids=[i for i, _ in BLOCKED])
def test_a_private_or_link_local_target_is_refused_and_leaves_no_row_behind(service, target):
    response = service.create(target)

    assert response.status_code == 400, (
        "{0!r} must be refused, got {1}: {2}".format(
            target, response.status_code, response.text[:200]
        )
    )
    error = assert_error_envelope(response)
    assert error["code"] == "blocked_destination", error
    assert service.link_rows() == [], (
        "a blocked destination must write no row; found " + repr(service.link_rows())
    )


def test_a_hostname_that_resolves_to_the_metadata_address_is_refused(service, stub_dns):
    stub_dns.set("metadata.internal.test", ["169.254.169.254"])

    response = service.create("http://metadata.internal.test/latest/meta-data/")

    assert response.status_code == 400, response.text[:200]
    assert assert_error_envelope(response)["code"] == "blocked_destination"
    assert service.link_rows() == []
    assert "metadata.internal.test" in stub_dns.lookups, (
        "the host was never resolved, so it cannot have been denylist checked"
    )


def test_a_hostname_that_resolves_to_loopback_is_refused_and_leaves_no_row(service, stub_dns):
    stub_dns.set("evil.test", ["127.0.0.1"])

    response = service.create("https://evil.test/x")

    assert response.status_code == 400, response.text[:200]
    assert assert_error_envelope(response)["code"] == "blocked_destination"
    assert service.link_rows() == []


def test_a_hostname_with_one_private_address_among_public_ones_is_refused(service, stub_dns):
    stub_dns.set("mixed.test", ["93.184.216.34", "10.0.0.5"])

    response = service.create("https://mixed.test/x")

    assert response.status_code == 400, response.text[:200]
    assert assert_error_envelope(response)["code"] == "blocked_destination"
    assert service.link_rows() == []


def test_a_decimal_ipv4_literal_for_loopback_is_refused(service):
    response = service.create("http://2130706433/x")

    assert response.status_code == 400, (
        "http://2130706433/ is 127.0.0.1 in decimal form, got {0}: {1}".format(
            response.status_code, response.text[:200]
        )
    )
    assert assert_error_envelope(response)["code"] == "blocked_destination"
    assert service.link_rows() == []


def test_a_public_address_and_a_public_hostname_are_still_accepted(service, stub_dns):
    stub_dns.set("good.test", ["93.184.216.34"])

    literal = service.create("http://93.184.216.34/x")
    named = service.create("https://good.test/x")

    assert literal.status_code == 201, literal.text[:200]
    assert named.status_code == 201, named.text[:200]
    assert service.link_count() == 2
