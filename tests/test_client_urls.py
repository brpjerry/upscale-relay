import pytest

from relay_client_core.client import RelayClient


@pytest.mark.parametrize("host, authority", [
    ("server", "server"), ("192.0.2.1", "192.0.2.1"),
    ("::1", "[::1]"), ("2001:db8::123", "[2001:db8::123]"),
])
def test_media_and_control_urls_support_ipv6_and_escape_paths(host, authority):
    client = RelayClient.__new__(RelayClient)
    client.host, client.port = host, 8590
    assert client.base_url == f"http://{authority}:8590"
    assert client.media_url("Shows/A #1.mkv") == f"http://{authority}:8590/media/Shows/A%20%231.mkv"
