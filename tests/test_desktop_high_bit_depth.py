"""The desktop asks for HDR at 10 bits, and for 10-bit SDR when switched on."""
import asyncio

import pytest
pytest.importorskip("PySide6")

from test_desktop_features_gui import LifecycleClient, setup_lifecycle
from test_server_library_gui import window  # noqa: F401


class DeepClient(LifecycleClient):
    """Reports a downlink as RelayClient reads it from session_opened."""

    hdr = False
    source_bits = 10

    async def open_session(self, config):
        session = await super().open_session(config)
        session.downlink_hdr = self.hdr
        session.source_bit_depth = self.source_bits
        return session


def setup_deep(window, monkeypatch, *, hdr=False, source_bits=10):
    events, errors = setup_lifecycle(window, monkeypatch)
    DeepClient.hdr, DeepClient.source_bits = hdr, source_bits
    window.client = DeepClient(events)
    restarts = []

    async def restart():
        restarts.append(window.settings.ten_bit_sdr)

    monkeypatch.setattr(window, "_restart_for_playback_setting", restart)
    return errors, restarts


def test_hdr_always_and_10_bit_sdr_on_request(window, monkeypatch):
    errors, _ = setup_deep(window, monkeypatch)

    async def scenario():
        assert not window.ten_bit_sdr_check.isChecked()
        await window._start_session("show.mkv", "server_file")
        assert window.client.opened_config.high_bit_depth == "hdr"
        window.ten_bit_sdr_check.setChecked(True)
        await asyncio.sleep(0)
        assert window.settings.ten_bit_sdr
        await window._start_session("show.mkv", "uplink")
        assert window.client.opened_config.high_bit_depth == "all"
        assert not errors

    asyncio.run(scenario())


@pytest.mark.parametrize(("hdr", "source_bits", "expected"), [
    (False, 10, [True, False]),  # 10-bit SDR: the switch changes the stream
    (False, 8, []),              # 8-bit source: nothing more to keep
    (True, 10, []),              # HDR stays 10-bit either way
    (False, None, []),           # a server that does not report depth
])
def test_toggle_restarts_only_a_10_bit_sdr_stream(window, monkeypatch, hdr, source_bits, expected):
    errors, restarts = setup_deep(window, monkeypatch, hdr=hdr, source_bits=source_bits)

    async def scenario():
        await window._start_session("show.mkv", "server_file")
        await window.on_ten_bit_sdr_changed(True)
        await window.on_ten_bit_sdr_changed(False)
        assert restarts == expected
        assert not errors

    asyncio.run(scenario())
