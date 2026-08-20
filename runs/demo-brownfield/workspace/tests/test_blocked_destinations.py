"""SSRF surface: loopback, private and link local targets never get stored."""
import pytest

from conftest import assert_error

BLOCKED = [
    ("cloud_metadata", "http://169.254.169.254/latest/meta-data/"),
    ("link_local", "http://169.254.10.1/x"),
    ("loopback_v4", "http://127.0.0.1:8080/admin"),
    ("loopback_v6", "http://[::1]/x"),
    ("loopback_decimal", "http://2130706433/x"),
    ("private_10", "http://10.0.0.7/x"),
    ("private_172", "http://172.16.5.4/x"),
    ("private_192", "http://192.168.1.1/router"),
    ("unique_local_v6", "http://[fd00::1]/x"),
    ("unspecified", "http://0.0.0.0/x"),
]


@pytest.mark.parametrize("label,target", BLOCKED, ids=[b[0] for b in BLOCKED])
def test_a_private_or_link_local_literal_is_rejected_and_writes_no_row(app, label, target):
    response = app.create(target)

    assert response.status_code == 400, (
        "{0} target {1!r} produced {2}: {3}".format(
            label, target, response.status_code, response.text[:200]
        )
    )
    error = assert_error(response)
    assert error["code"] == "blocked_destination", error
    assert app.link_rows() == [], "a blocked destination left a row behind"


def test_the_cloud_metadata_address_is_blocked_behind_a_hostname_too(app, fake_dns):
    fake_dns.set("metadata.internal.test", ["169.254.169.254"])

    response = app.create("http://metadata.internal.test/latest/meta-data/")

    assert response.status_code == 400, response.text[:200]
    assert assert_error(response)["code"] == "blocked_destination"
    assert app.link_count() == 0
    assert "metadata.internal.test" in fake_dns.lookups, (
        "the host was never resolved, so it cannot have been denylist checked"
    )


def test_a_hostname_resolving_to_loopback_is_rejected_and_writes_no_row(app, fake_dns):
    fake_dns.set("evil.test", ["127.0.0.1"])

    response = app.create("https://evil.test/x")

    assert response.status_code == 400, response.text[:200]
    assert assert_error(response)["code"] == "blocked_destination"
    assert app.link_count() == 0


def test_a_hostname_with_one_private_address_among_public_ones_is_rejected(app, fake_dns):
    fake_dns.set("mixed.test", ["93.184.216.34", "10.0.0.5"])

    response = app.create("https://mixed.test/x")

    assert response.status_code == 400, response.text[:200]
    assert assert_error(response)["code"] == "blocked_destination"
    assert app.link_count() == 0


def test_an_unresolvable_hostname_fails_closed_and_writes_no_row(app, fake_dns):
    fake_dns.fail("nowhere.invalid")

    response = app.create("https://nowhere.invalid/x")

    assert response.status_code == 400, response.text[:200]
    assert assert_error(response)["code"] == "blocked_destination"
    assert app.link_count() == 0


def test_public_addresses_are_still_accepted(app, fake_dns):
    fake_dns.set("good.test", ["93.184.216.34"])

    literal = app.create("http://93.184.216.34/x")
    named = app.create("https://good.test/x")

    assert literal.status_code == 201, literal.text[:200]
    assert named.status_code == 201, named.text[:200]
    assert app.link_count() == 2
