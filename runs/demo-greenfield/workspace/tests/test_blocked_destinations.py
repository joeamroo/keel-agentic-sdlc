"""SSRF surface: loopback, private, link local and metadata targets."""
import pytest

from conftest import assert_error_envelope

LOOPBACK = [
    "http://127.0.0.1/x",
    "http://127.5.5.5/x",
    "http://[::1]/x",
    "http://[::ffff:127.0.0.1]/x",
]

PRIVATE_OR_RESERVED = [
    "http://10.0.0.1/x",
    "http://172.16.0.5/x",
    "http://192.168.1.1/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/x",
    "http://0.0.0.0/x",
    "http://[fd00::1]/x",
    "http://[fe80::1]/x",
]


@pytest.mark.parametrize("target", LOOPBACK)
def test_loopback_literal_is_rejected_with_blocked_destination_and_writes_no_row(app, target):
    response = app.create(target)

    assert response.status_code == 400, (target, response.status_code, response.text[:200])
    error = assert_error_envelope(response)
    assert error["code"] == "blocked_destination", error
    assert app.link_rows() == []


@pytest.mark.parametrize("target", PRIVATE_OR_RESERVED)
def test_private_or_reserved_literal_is_rejected_with_blocked_destination_and_writes_no_row(app, target):
    response = app.create(target)

    assert response.status_code == 400, (target, response.status_code, response.text[:200])
    error = assert_error_envelope(response)
    assert error["code"] == "blocked_destination", error
    assert app.link_rows() == []


def test_the_cloud_metadata_address_is_blocked_even_behind_a_hostname(app, fake_dns):
    fake_dns.set("metadata.internal.test", ["169.254.169.254"])

    response = app.create("http://metadata.internal.test/latest/meta-data/")

    assert response.status_code == 400, response.text
    assert assert_error_envelope(response)["code"] == "blocked_destination"
    assert app.link_rows() == []


def test_hostname_resolving_to_loopback_is_rejected_and_writes_no_row(app, fake_dns):
    fake_dns.set("evil.test", ["127.0.0.1"])

    response = app.create("https://evil.test/x")

    assert response.status_code == 400, response.text
    assert assert_error_envelope(response)["code"] == "blocked_destination"
    assert app.link_rows() == []
    assert "evil.test" in fake_dns.lookups, (
        "the service never resolved the hostname, so it cannot have denylist-checked it"
    )


def test_hostname_with_one_blocked_address_among_several_is_rejected(app, fake_dns):
    fake_dns.set("mixed.test", ["93.184.216.34", "10.0.0.5"])

    response = app.create("https://mixed.test/x")

    assert response.status_code == 400, response.text
    assert assert_error_envelope(response)["code"] == "blocked_destination"
    assert app.link_rows() == []


def test_unresolvable_hostname_fails_closed_and_writes_no_row(app, fake_dns):
    fake_dns.fail("nowhere.invalid")

    response = app.create("https://nowhere.invalid/x")

    assert response.status_code == 400, response.text
    assert assert_error_envelope(response)["code"] == "blocked_destination"
    assert app.link_rows() == []


def test_public_destinations_are_still_accepted_so_the_filter_is_not_deny_everything(app, fake_dns):
    fake_dns.set("good.test", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])

    literal = app.create("http://93.184.216.34/x")
    named = app.create("https://good.test/x")

    assert literal.status_code == 201, literal.text
    assert named.status_code == 201, named.text
    assert len(app.link_rows()) == 2
