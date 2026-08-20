"""Private, loopback, link-local and reserved destinations never become links."""

import pytest

PRIVATE_ERRORS = {
    "destination_not_routable",
    "private_destination",
    "forbidden_destination",
    "invalid_destination",
}


@pytest.mark.parametrize(
    "destination",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
    ],
    ids=[
        "cloud_metadata_169_254_169_254",
        "loopback_v4",
        "loopback_v4_with_port",
        "loopback_v6",
        "private_10_8",
        "private_172_16_12",
        "private_192_168_16",
        "cgnat_100_64_10",
        "unspecified_0_0_0_0",
        "multicast_224_4",
        "ipv6_unique_local_fc00_7",
        "ipv6_link_local_fe80_10",
    ],
)
def test_non_routable_ip_literal_is_rejected_and_no_row_is_written(
    client, create_link, db, destination
):
    resp = create_link(client, destination)

    assert resp.status_code == 400, f"{destination} accepted with {resp.status_code}: {resp.text}"
    assert db.count() == 0, f"a row was written for {destination}"


def test_loopback_rejection_carries_a_machine_readable_error_code(client, create_link, db, helpers):
    resp = create_link(client, "http://127.0.0.1/")

    assert resp.status_code == 400, resp.text
    code = helpers.error_code(resp)
    assert code in PRIVATE_ERRORS, f"expected a not-routable error code, got {code!r}: {resp.text}"
    assert db.count() == 0


@pytest.mark.parametrize(
    "destination",
    [
        "http://metadata.google.internal/",
        "http://internal.corp.example/admin",
        "http://loopback.example.com/",
    ],
    ids=["metadata_google_internal", "hostname_resolving_to_10_8", "hostname_resolving_to_loopback"],
)
def test_hostname_resolving_to_a_denied_range_is_rejected_and_no_row_is_written(
    client, create_link, db, destination, _no_outbound_connections
):
    resp = create_link(client, destination)

    assert resp.status_code == 400, f"{destination} accepted with {resp.status_code}: {resp.text}"
    assert db.count() == 0, f"a row was written for {destination}"
    assert _no_outbound_connections == [], "validation opened a connection to the destination"


def test_public_destination_is_accepted_without_any_outbound_connection(
    client, create_link, db, _no_outbound_connections
):
    resp = create_link(client, "https://example.com/a")

    assert resp.status_code == 201, resp.text
    assert db.count() == 1
    assert _no_outbound_connections == [], "creation opened a connection to the destination"


def test_redirect_revalidates_and_refuses_a_destination_that_now_resolves_to_loopback(
    client, create_link, db, helpers, monkeypatch
):
    """DNS rebinding: the host was public at creation and is loopback at click time."""
    import conftest

    resp = create_link(client, "http://other.example.com/x")
    assert resp.status_code == 201, resp.text
    code = helpers.code_of(resp.json())

    monkeypatch.setitem(conftest.HOST_MAP, "other.example.com", ["127.0.0.1"])

    redirect = client.get(f"/{code}", follow_redirects=False)

    assert redirect.status_code in (410, 502), (
        f"rebound destination still redirected with {redirect.status_code} "
        f"-> {redirect.headers.get('location')!r}"
    )
    assert "location" not in {k.lower() for k in redirect.headers.keys()}
