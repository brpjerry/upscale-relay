"""The desktop asks for HDR at 10 bits, and for every video at 10 bits when
switched on."""
import asyncio

import pytest
pytest.importorskip("PySide6")

from test_desktop_features_gui import LifecycleClient, setup_lifecycle
from test_server_library_gui import window  # noqa: F401


class DeepClient(LifecycleClient):
    """Reports a downlink as RelayClient reads it from session_opened."""

    hdr = False
    source_bits = 10
    capabilities = {"high_bit_depth": True}

    async def open_session(self, config):
        session = await super().open_session(config)
        session.downlink_hdr = self.hdr
        session.source_bit_depth = self.source_bits
        return session


def setup_deep(window, monkeypatch, *, hdr=False, source_bits=10, deep_server=True):
    events, errors = setup_lifecycle(window, monkeypatch)
    DeepClient.hdr, DeepClient.source_bits = hdr, source_bits
    window.client = DeepClient(events)
    window.client.capabilities = {"high_bit_depth": deep_server}
    restarts = []

    async def restart():
        restarts.append(window.settings.ten_bit_video)

    monkeypatch.setattr(window, "_restart_for_playback_setting", restart)
    return errors, restarts


def test_hdr_always_and_every_video_at_10_bits_on_request(window, monkeypatch):
    errors, _ = setup_deep(window, monkeypatch)

    async def scenario():
        assert not window.ten_bit_video_check.isChecked()
        await window._start_session("show.mkv", "server_file")
        assert window.client.opened_config.high_bit_depth == "hdr"
        window.ten_bit_video_check.setChecked(True)
        await asyncio.sleep(0)
        assert window.settings.ten_bit_video
        await window._start_session("show.mkv", "uplink")
        assert window.client.opened_config.high_bit_depth == "all"
        assert not errors

    asyncio.run(scenario())


@pytest.mark.parametrize(("hdr", "source_bits", "deep_server", "expected"), [
    (False, 10, True, [True, False]),   # 10-bit SDR
    (False, 8, True, [True, False]),    # 8-bit SDR: widened to 10 bits too
    (False, None, True, [True, False]),  # a server that does not report the source's depth
    (True, 10, True, []),               # HDR stays 10-bit either way
    (False, 8, False, []),              # a server without deep colour streams 8 bits anyway
])
def test_toggle_restarts_every_sdr_stream(window, monkeypatch, hdr, source_bits, deep_server, expected):
    errors, restarts = setup_deep(window, monkeypatch, hdr=hdr, source_bits=source_bits, deep_server=deep_server)

    async def scenario():
        await window._start_session("show.mkv", "server_file")
        await window.on_ten_bit_video_changed(True)
        await window.on_ten_bit_video_changed(False)
        assert restarts == expected
        assert not errors

    asyncio.run(scenario())


@pytest.mark.parametrize(("hdr", "mpv_read", "expected"), [
    (True, False, True), (False, False, False),
    (False, True, True),  # an HLG alternative-transfer SEI: only mpv saw it, in the frames
])
def test_playing_locally_keeps_what_the_server_read_of_the_source(window, monkeypatch, hdr, mpv_read, expected):
    """The local fallback plays an HDR source on the HDR surface too: the
    server read the file's colour, and its downlink is HDR for an HDR file."""
    errors, _ = setup_deep(window, monkeypatch, hdr=hdr)
    window.player.source_hdr = mpv_read

    async def scenario():
        await window._start_session("/videos/show.mkv", "uplink")
        await window.on_fallback()
        assert window._session_source == "local"
        assert window.player.local_hdr is expected
        assert not errors

    asyncio.run(scenario())


def test_the_hdr_switch_is_a_setting_and_says_when_it_applies(window):
    assert window.hdr_output_check.isChecked()
    window.hdr_output_check.setChecked(False)
    assert not window.settings.hdr_output
    window.hdr_output_check.setChecked(True)
    assert window.settings.hdr_output
    assert window.hdr_output_hint.text()  # what the platform allows, or the display's state


def test_an_hdr_surface_reload_replays_the_local_file_or_waits_for_the_transition(window):
    async def scenario():
        window._session_source, window._session_path, window._position_s = "local", "/videos/show.mkv", 12.0
        window.player.source_hdr = True
        await window._reload_for_hdr_surface()
        assert window.player.local_playback == ("/videos/show.mkv", 12.0, window._paused)
        assert window.player.local_hdr is True  # as the player read the stream
        retried = []
        window.player.retry_hdr_check = lambda: retried.append(True)
        window._transitioning = True
        window.player.local_playback = None
        await window._reload_for_hdr_surface()
        window._transitioning = False
        assert retried == [True] and window.player.local_playback is None  # asked again later

    asyncio.run(scenario())


def test_fullscreen_tells_the_player_once_settled_and_at_once_when_it_ends(window):
    calls = []
    window.player.fullscreen_changed = lambda: calls.append(window.isFullScreen())
    window.show()
    window.toggle_fullscreen()
    assert calls == [] and not window.fullscreen_settled  # the slides first
    window._on_fullscreen_settled()
    assert calls == [True] and window.fullscreen_settled
    window.toggle_fullscreen()
    assert calls[-1] is False and calls.count(True) == 1 and not window.fullscreen_settled


def test_fullscreen_from_the_compositor_settles_the_same_way(window):
    """A Hyprland binding fullscreens the window without toggle_fullscreen."""
    calls = []
    window.player.fullscreen_changed = lambda: calls.append(window.isFullScreen())
    window.show()
    window.showFullScreen()
    assert window._pacing_timer.isActive() and not window.fullscreen_settled
    window._on_fullscreen_settled()
    assert calls == [True] and window.fullscreen_settled
    window.showNormal()
    assert calls == [True, False] and not window.fullscreen_settled


def test_the_hdr_hint_says_what_applies(window, monkeypatch):
    from desktop_client import hdr_output
    from desktop_client.hdr_output import DisplayHdr
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: True)
    monkeypatch.setattr(hdr_output, "compositor_auto_hdr", lambda: False)
    player = window.player
    player.hdr_output, player.display_hdr = True, DisplayHdr(True, 1107.0, 203.0)
    window._show_hdr_output_hint()
    assert window.hdr_output_hint.text() == "Playing in HDR (up to 1107 nits)."
    player.hdr_output = False
    window._show_hdr_output_hint()
    assert window.hdr_output_hint.text() == "HDR videos play as HDR while the display is in HDR mode."
    monkeypatch.setattr(hdr_output, "compositor_auto_hdr", lambda: True)
    window._show_hdr_output_hint()
    assert "in fullscreen" in window.hdr_output_hint.text()
    player.hdr_output_failed = True
    window._show_hdr_output_hint()
    assert "Switch it off and on" in window.hdr_output_hint.text()
    player.hdr_output_failed = False
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: False)
    window._show_hdr_output_hint()
    assert "RELAY_HDR_WINDOWS=1" in window.hdr_output_hint.text()


def test_screens_that_did_not_switch_are_remembered_until_the_setting_switches(window):
    window._remember_auto_hdr_refusal("wayland eDP-1")
    window._remember_auto_hdr_refusal("wayland eDP-1")
    assert window.settings.hdr_auto_refused == ["wayland eDP-1"]
    window.on_hdr_output_changed(True)
    assert window.settings.hdr_auto_refused == []
