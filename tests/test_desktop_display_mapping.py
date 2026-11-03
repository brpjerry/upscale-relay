"""The desktop and a display-mapping server (PROTOCOL §3.6): the display
target the player names, the reload when an epoch was mapped for another
display, and what the window sends at open and with each seek."""
import asyncio
from types import SimpleNamespace

import pytest
pytest.importorskip("PySide6")
pytest.importorskip("mpv")

from desktop_client import hdr_output
from desktop_client.hdr_output import DisplayHdr
from desktop_client.main_window import _mapping_rows
from desktop_client.mpv_view import MpvPlayerView
from relay_client_core import DisplayTarget
from test_desktop_features_gui import LifecycleClient, setup_lifecycle
from test_desktop_hdr_output import HDR_DISPLAY, SDR_DISPLAY, _bind, _player, _window
from test_server_library_gui import window  # noqa: F401

MAPPED_PQ = {"transfer": "pq", "peak_nits": 1107, "sdr_white_nits": 203, "applied": ["dolby_vision"]}
MAPPED_SDR = {"transfer": "sdr", "sdr_white_nits": 80, "applied": ["dolby_vision"]}


@pytest.fixture()
def displays(monkeypatch):
    """The display hdr_output reports, settable by the test."""
    shown = [SDR_DISPLAY]
    monkeypatch.setenv("RELAY_HDR_SURFACE", "")
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: True)
    monkeypatch.setattr(hdr_output, "compositor_auto_hdr", lambda: True)
    monkeypatch.setattr(hdr_output, "configured_hdr", lambda name: True)
    monkeypatch.setattr(hdr_output, "_seen_hdr", {})
    monkeypatch.setattr(hdr_output, "query", lambda window=None: shown[0])
    return shown


def _mapped_player(mapping, **fields):
    defaults = dict(
        _session=SimpleNamespace(downlink_mapping=mapping), _local_playback=False,
        _hdr_stale_reported=False, _task=object(), _on_presenter=False, _presenter=None,
        _display_hdr=SDR_DISPLAY, _observed={}, _epoch_released=True, window=_window(False),
    )
    defaults.update(fields)
    stale = []
    player = _player(hdr_surface_stale=SimpleNamespace(emit=lambda: stale.append(True)), **defaults)
    _bind(player, "_wanted_surface", "_follow_auto_hdr", "display_target", "_mapping")
    return player, stale


@pytest.mark.parametrize(("display", "fields", "expected"), [
    (HDR_DISPLAY, {}, DisplayTarget("pq", 1107, 203)),
    (SDR_DISPLAY, {}, DisplayTarget("sdr", None, 80)),
    # Fullscreen on an SDR screen the compositor switches to HDR: the peak
    # that switch is expected to bring, so the stream is HDR when it comes.
    (SDR_DISPLAY, {"window": _window(True)}, DisplayTarget("pq", 1000, 203)),
    # HDR presentation off: SDR, the display not even read.
    (HDR_DISPLAY, {"_hdr_enabled": False}, DisplayTarget("sdr")),
    (HDR_DISPLAY, {"_presenter_broken": True}, DisplayTarget("sdr")),
    # An epoch mapped to SDR is not PQ, but an HDR video all the same.
    (HDR_DISPLAY, {"_source_hdr": False}, DisplayTarget("pq", 1107, 203)),
])
def test_the_target_is_where_an_hdr_video_would_be_shown(displays, display, fields, expected):
    displays[0] = display
    player, _stale = _mapped_player(None, **fields)
    assert player.display_target() == expected


def test_without_hdr_presentation_the_target_is_sdr(displays, monkeypatch):
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: False)
    displays[0] = HDR_DISPLAY
    player, _stale = _mapped_player(None)
    assert player.display_target().transfer == "sdr"


@pytest.mark.parametrize(("mapping", "display", "fields", "expected"), [
    (MAPPED_SDR, SDR_DISPLAY, {"_source_hdr": False}, []),
    (MAPPED_SDR, HDR_DISPLAY, {"_source_hdr": False}, [True]),     # switched to HDR
    (MAPPED_PQ, HDR_DISPLAY, {"_on_presenter": True, "_display_hdr": HDR_DISPLAY,
                              "_presenter": SimpleNamespace(pq=True)}, []),
    (MAPPED_PQ, SDR_DISPLAY, {"_on_presenter": True,
                              "_presenter": SimpleNamespace(pq=True)}, [True]),  # back to SDR
    # Mapped for the expected 1000 nits; the switch brought 1107: it fits.
    ({**MAPPED_PQ, "peak_nits": 1000}, HDR_DISPLAY,
     {"_on_presenter": True, "_display_hdr": HDR_DISPLAY, "_presenter": SimpleNamespace(pq=True)}, []),
    # Fullscreen on an SDR screen: the SDR epoch reloads to be PQ for the switch.
    (MAPPED_SDR, SDR_DISPLAY, {"_source_hdr": False, "window": _window(True)}, [True]),
])
def test_an_epoch_mapped_for_another_display_reloads_once(displays, mapping, display, fields, expected):
    displays[0] = display
    player, stale = _mapped_player(mapping, **fields)
    MpvPlayerView._check_hdr_surface(player)
    MpvPlayerView._check_hdr_surface(player)
    assert stale == expected


def test_local_playback_is_never_mapped(displays):
    displays[0] = HDR_DISPLAY
    player, stale = _mapped_player(MAPPED_SDR, _local_playback=True, _source_hdr=False)
    assert player._mapping() is None
    MpvPlayerView._check_hdr_surface(player)
    assert not stale


@pytest.mark.parametrize(("mapping", "expected"), [(MAPPED_PQ, True), (MAPPED_SDR, False)])
def test_a_mapped_epoch_goes_on_the_surface_only_when_it_is_pq(displays, mapping, expected):
    displays[0] = HDR_DISPLAY
    moved = []

    async def move(display):
        moved.append(display)
        return True

    player, _stale = _mapped_player(
        mapping, _source_hdr=True, _load_generation=0, hdr_output=False,
        _move_to_presenter=move, _move_to_widget=lambda: None, _apply_output_target=lambda: None,
        hdr_output_changed=SimpleNamespace(emit=lambda hdr: None),
    )
    asyncio.run(MpvPlayerView._prepare_surface(player))
    assert player._source_hdr is expected and bool(moved) is expected


class MappingClient(LifecycleClient):
    """A display-mapping server: the target negotiated, seeks recorded."""

    async def open_session(self, config):
        session = await super().open_session(config)
        session.display_target = config.display_target
        session.downlink_mapping = MAPPED_SDR
        return session

    async def seek(self, pts, display_target=None):
        self.events.append(("seek", pts, display_target))


def test_the_window_names_the_display_at_open_and_with_each_seek(window, monkeypatch):
    events, errors = setup_lifecycle(window, monkeypatch)
    window.client = MappingClient(events)
    targets = iter([DisplayTarget("sdr", None, 80), DisplayTarget("pq", 1107, 203)])
    monkeypatch.setattr(window.player, "display_target", lambda: next(targets), raising=False)

    async def scenario():
        await window._start_session("film.mkv", "server_file")
        assert window.client.opened_config.display_target == DisplayTarget("sdr", None, 80)
        assert ("Dynamic HDR", "Dolby Vision, applied by the server for this display") in window._now_info[1]
        await window._seek_to_seconds(10.0, announce=False)
        seeks = [event for event in events if event[0] == "seek"]
        assert seeks[-1][2] == DisplayTarget("pq", 1107, 203)
        assert not errors

    asyncio.run(scenario())


def test_sessions_without_mapping_seek_as_before(window, monkeypatch):
    events, errors = setup_lifecycle(window, monkeypatch)
    calls = []

    class PlainClient(LifecycleClient):
        async def seek(self, pts):  # no display_target parameter at all
            calls.append(pts)

    window.client = PlainClient(events)

    async def scenario():
        await window._start_session("film.mkv", "server_file")
        await window._seek_to_seconds(10.0, announce=False)
        assert calls and not errors

    asyncio.run(scenario())


@pytest.mark.parametrize(("mapping", "expected"), [
    (None, []),
    ({"applied": ["dolby_vision", "hdr10plus"]},
     [("Dynamic HDR", "Dolby Vision and HDR10+, applied by the server for this display")]),
    ({"applied": []}, [("Dynamic HDR", "Dynamic HDR metadata, applied by the server for this display")]),
])
def test_info_card_line(mapping, expected):
    assert _mapping_rows(mapping) == expected
