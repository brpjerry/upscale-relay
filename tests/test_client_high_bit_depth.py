"""SessionConfig.high_bit_depth: asked for only of a capable server, and the
downlink's depth and colour read back from session_opened (PROTOCOL §3.5)."""

import asyncio
from types import SimpleNamespace

import pytest

from relay_client_core import RelayClient, SessionConfig
from relay_client_core import client as module

PQ = {"primaries": 9, "transfer": 16, "matrix": 9, "range": "limited"}


def _open(monkeypatch, cfg, *, capabilities, opened):
    monkeypatch.setattr(
        module, "_open_local_source",
        lambda path: (SimpleNamespace(close=lambda: None),
                      {"codec": "hevc", "width": 64, "height": 64, "time_base": [1, 1000]},
                      None, []),
    )

    async def scenario():
        client = RelayClient("media-server", 8590)
        client.capabilities = capabilities
        sent = {}

        async def request(expect, type_, timeout=30, keepalive=False, **fields):
            sent.update(fields)
            return {
                "session_id": "s", "media_port": 1, "uplink_token": None,
                "downlink_token": "d", "downlink_codec": "hevc",
                "downlink_width": 64, "downlink_height": 64, **opened,
            }

        client._request = request
        try:
            return sent, await client.open_session(cfg)
        finally:
            await client.close()

    return asyncio.run(scenario())


def test_deep_colour_is_requested_and_reported(monkeypatch):
    sent, info = _open(
        monkeypatch, SessionConfig(path="a.mkv", high_bit_depth="hdr"),
        capabilities={"high_bit_depth": True},
        opened={"downlink_bit_depth": 10, "downlink_color": PQ,
                "downlink_dolby_vision": True, "source_bit_depth": 10},
    )
    assert sent["high_bit_depth"] == "hdr"
    assert (info.downlink_bit_depth, info.downlink_color, info.downlink_dolby_vision) == (10, PQ, True)
    assert info.downlink_hdr and info.source_bit_depth == 10


def test_older_servers_are_not_asked_and_read_as_8_bit(monkeypatch):
    sent, info = _open(
        monkeypatch, SessionConfig(path="a.mkv", high_bit_depth="all"),
        capabilities={}, opened={},
    )
    assert "high_bit_depth" not in sent
    assert (info.downlink_bit_depth, info.downlink_color, info.source_bit_depth) == (8, None, None)
    assert not info.downlink_hdr and not info.downlink_dolby_vision


def test_unknown_mode_is_refused_before_anything_is_sent(monkeypatch):
    with pytest.raises(ValueError, match="high bit depth"):
        _open(monkeypatch, SessionConfig(path="a.mkv", high_bit_depth="12bit"),
              capabilities={"high_bit_depth": True}, opened={})
