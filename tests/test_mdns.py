"""mDNS advertisement payload and address selection."""

from relay_server.mdns import SERVICE_TYPE, primary_ipv4, txt_properties


def test_txt_properties_are_strings():
    props = txt_properties(protocol_version=1, media_port=8591, server_name="upscale-relay")
    assert props == {"protocol": "1", "media_port": "8591", "server": "upscale-relay"}
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in props.items())


def test_service_type_is_dns_sd_shaped():
    assert SERVICE_TYPE.startswith("_upscalerelay._tcp.")
    assert SERVICE_TYPE.endswith(".local.")


def test_primary_ipv4_is_none_or_non_loopback():
    address = primary_ipv4()
    assert address is None or not address.startswith("127.")
