"""HDR output: which surface a video plays on, what mpv is told, and the
overlays drawn over the HDR surface (desktop_client/hdr_view.py)."""
import asyncio
import ctypes
import time
from fractions import Fraction
from types import SimpleNamespace

import pytest
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QColor, QColorSpace, QPainter
from PySide6.QtWidgets import QApplication, QFrame, QGraphicsOpacityEffect, QWidget

from desktop_client import hdr_output
from desktop_client.hdr_output import DisplayHdr, _Protocol
from desktop_client.hdr_view import HdrPresenter, OverlayMirror

HDR_DISPLAY = DisplayHdr(True, 1107.0, 203.0, "test")
SDR_DISPLAY = DisplayHdr(False, 80.0, 80.0, "test")


@pytest.fixture()
def app():
    return QApplication.instance() or QApplication([])


# -- choosing the surface ------------------------------------------------------

def _player(**fields):
    defaults = dict(
        options=SimpleNamespace(headless=False), _presenter_broken=False, _hdr_enabled=True,
        _source_hdr=True, window=lambda: None, _presenting=lambda: True,
        _auto_hdr=False, _auto_hdr_deadline=None, _auto_hdr_refused=False,
    )
    defaults.update(fields)
    return SimpleNamespace(**defaults)


def _bind(player, *names):
    """MpvPlayerView's own methods on a fake, for those that call others."""
    from desktop_client.mpv_view import MpvPlayerView
    for name in names:
        setattr(player, name, getattr(MpvPlayerView, name).__get__(player))
    player._pq_for = MpvPlayerView._pq_for
    return player


def _window(fullscreen):
    return lambda: SimpleNamespace(isFullScreen=lambda: fullscreen)


@pytest.mark.parametrize(("fields", "env", "display", "expected"), [
    ({}, "", HDR_DISPLAY, True),
    ({}, "", SDR_DISPLAY, False),                        # an SDR display: tone-mapped, as before
    ({"_source_hdr": False}, "", HDR_DISPLAY, False),    # an SDR video never moves
    ({"_hdr_enabled": False}, "", HDR_DISPLAY, False),   # "Show HDR videos in HDR" off
    ({"_presenter_broken": True}, "", HDR_DISPLAY, False),
    ({"options": SimpleNamespace(headless=True)}, "", HDR_DISPLAY, False),
    ({}, "off", HDR_DISPLAY, False),
    ({"_source_hdr": False}, "force", SDR_DISPLAY, True),  # the test mode, any video and display
])
def test_the_hdr_surface_is_for_hdr_videos_on_hdr_displays(monkeypatch, fields, env, display, expected):
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    monkeypatch.setenv("RELAY_HDR_SURFACE", env)
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: True)
    queries = []
    monkeypatch.setattr(hdr_output, "query", lambda window=None: queries.append(window) or display)
    want, _display = MpvPlayerView._wanted_surface(_player(**fields))
    assert want is expected
    if not fields.get("_source_hdr", True) and env != "force":
        assert not queries  # SDR videos cost no display query


def test_an_unsupported_platform_keeps_hdr_videos_on_the_widget(monkeypatch):
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    monkeypatch.setenv("RELAY_HDR_SURFACE", "")
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: False)
    monkeypatch.setattr(hdr_output, "query", lambda window=None: HDR_DISPLAY)
    assert MpvPlayerView._wanted_surface(_player())[0] is False


def test_mpv_outputs_pq_only_on_the_hdr_surface():
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    sets = []
    player = _player(
        hdr_output=False, _display_hdr=HDR_DISPLAY,
        _output_target={"target-trc": "auto", "target-prim": "auto", "target-peak": "auto"},
        mpv=SimpleNamespace(command=lambda *args: sets.append(args[1:])),
        log_message=SimpleNamespace(emit=lambda *args: None),
    )
    MpvPlayerView._apply_output_target(player)
    assert sets == []  # SDR loads send mpv nothing: its defaults stand
    player.hdr_output = True
    MpvPlayerView._apply_output_target(player)
    assert sets == [("target-trc", "pq"), ("target-prim", "bt.2020"), ("target-peak", "1107")]
    player.hdr_output = False
    MpvPlayerView._apply_output_target(player)
    assert sets[3:] == [("target-trc", "auto"), ("target-prim", "auto"), ("target-peak", "auto")]


def test_a_display_switching_hdr_during_an_hdr_video_asks_for_one_reload(monkeypatch):
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    monkeypatch.setenv("RELAY_HDR_SURFACE", "")
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: True)
    display = [SDR_DISPLAY]
    monkeypatch.setattr(hdr_output, "query", lambda window=None: display[0])
    stale = []
    player = _player(
        _hdr_stale_reported=False, _task=object(), _local_playback=False, _on_presenter=False,
        _presenter=None, _display_hdr=SDR_DISPLAY, _observed={}, _epoch_released=True,
        hdr_surface_stale=SimpleNamespace(emit=lambda: stale.append(True)),
    )
    _bind(player, "_wanted_surface", "_follow_auto_hdr")
    MpvPlayerView._check_hdr_surface(player)
    assert not stale  # SDR display, widget surface: fits
    display[0] = HDR_DISPLAY  # the user switched HDR on
    MpvPlayerView._check_hdr_surface(player)
    MpvPlayerView._check_hdr_surface(player)
    assert stale == [True]  # once, until the reload decides again


@pytest.mark.parametrize(("gamma", "expected"), [("hlg", [True]), ("pq", [True]), ("bt.1886", [])])
def test_a_stream_mpv_reads_as_hdr_moves_though_the_session_said_sdr(monkeypatch, gamma, expected):
    """An HLG alternative-transfer SEI over BT.2020 tags reaches only the
    frames: downlink_color can say SDR for an HLG stream (PROTOCOL §3.5)."""
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    monkeypatch.setenv("RELAY_HDR_SURFACE", "")
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: True)
    monkeypatch.setattr(hdr_output, "query", lambda window=None: HDR_DISPLAY)
    stale = []
    player = _player(
        _source_hdr=False, _hdr_stale_reported=False, _task=object(), _local_playback=False,
        _on_presenter=False, _presenter=None, _display_hdr=SDR_DISPLAY, _epoch_released=True,
        _observed={"video-params": {"gamma": gamma, "primaries": "bt.2020"}},
        hdr_surface_stale=SimpleNamespace(emit=lambda: stale.append(True)),
    )
    _bind(player, "_wanted_surface", "_follow_auto_hdr")
    MpvPlayerView._check_hdr_surface(player)
    assert stale == expected and player._source_hdr is bool(expected)


def test_no_reload_is_asked_while_the_window_is_hidden(monkeypatch):
    """A hidden window never exposes a new surface: every reload would time
    out and ask again, a relay seek every few seconds."""
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    monkeypatch.setenv("RELAY_HDR_SURFACE", "")
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: True)
    monkeypatch.setattr(hdr_output, "query", lambda window=None: HDR_DISPLAY)
    stale = []
    presenting = [False]
    player = _player(
        _presenting=lambda: presenting[0], _hdr_stale_reported=False, _task=object(), _local_playback=False,
        _on_presenter=False, _presenter=None, _display_hdr=SDR_DISPLAY, _observed={}, _epoch_released=True,
        hdr_surface_stale=SimpleNamespace(emit=lambda: stale.append(True)),
    )
    _bind(player, "_wanted_surface", "_follow_auto_hdr")
    MpvPlayerView._check_hdr_surface(player)
    assert not stale
    presenting[0] = True  # shown again: now the reload can start the surface
    MpvPlayerView._check_hdr_surface(player)
    assert stale == [True]


class _Presenter:
    """An HDR surface that never starts, or whose load is replaced meanwhile."""

    def __init__(self, player, replaced=False):
        self.player, self.replaced, self.pq, self.broken = player, replaced, True, False
        self.calls = []

    @property
    def initialized(self):
        if self.replaced:
            self.player._load_generation += 1  # a stop() while it waited
        return False

    def __getattr__(self, name):
        return lambda *args: self.calls.append(name)


@pytest.mark.parametrize(("presenting", "replaced", "broken", "hidden"), [
    (True, False, True, True),    # shown and never started: broken, SDR from now on
    (False, False, False, True),  # hidden: it could not start; the next check retries
    (True, True, False, False),   # its load was stopped or replaced: left to the next one
])
def test_a_surface_that_does_not_start(monkeypatch, presenting, replaced, broken, hidden):
    pytest.importorskip("mpv")
    from desktop_client import mpv_view
    monkeypatch.setattr(mpv_view, "_HDR_SURFACE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: True)
    player = _player(
        _on_presenter=False, _load_generation=0, _presenting=lambda: presenting,
        window=lambda: SimpleNamespace(windowHandle=lambda: object()),
        log_message=SimpleNamespace(emit=lambda *args: None),
    )
    player._presenter = presenter = _Presenter(player, replaced)
    player._pq_for = mpv_view.MpvPlayerView._pq_for
    moved = asyncio.run(mpv_view.MpvPlayerView._move_to_presenter(player, HDR_DISPLAY))
    assert moved is False and player._presenter_broken is broken
    assert ("hide" in presenter.calls) is hidden


def test_stop_takes_down_a_surface_that_never_started():
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    calls = []
    player = _player(
        _presenter=SimpleNamespace(isVisible=lambda: True), _on_presenter=False, _task=None,
        _local_playback=False, hdr_output=False,
        _move_to_widget=lambda: calls.append("widget"), _apply_output_target=lambda: calls.append("target"),
        hdr_output_changed=SimpleNamespace(emit=lambda on: calls.append(("hdr_output", on))),
    )
    MpvPlayerView._release_surface_if_idle(player)
    assert calls == ["widget", "target"]  # hidden; it never showed HDR, so nothing to announce


def test_a_failing_hdr_surface_falls_back_to_the_widget():
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    moved, logged = [], []

    async def broken(_display):
        raise RuntimeError("no surface")

    player = _player(
        hdr_output=False, _presenter_broken=False, _on_presenter=False, _load_generation=0,
        _follow_auto_hdr=lambda *_: None,
        _wanted_surface=lambda: (True, HDR_DISPLAY), _move_to_presenter=broken,
        _move_to_widget=lambda: moved.append("widget"), _apply_output_target=lambda: None,
        log_message=SimpleNamespace(emit=lambda *args: logged.append(args)),
        hdr_output_changed=SimpleNamespace(emit=lambda *args: None),
    )
    asyncio.run(MpvPlayerView._prepare_surface(player))
    assert moved == ["widget"] and player._presenter_broken and logged


# -- fullscreen on an SDR screen that the compositor switches to HDR ------------------

@pytest.mark.parametrize(("fullscreen", "compositor", "refused", "expected"), [
    (True, True, False, True),
    (False, True, False, False),  # windowed: tone-mapped, as before
    (True, False, False, False),  # a compositor that does not switch
    (True, True, True, False),    # it did not switch last time
])
def test_fullscreen_on_an_sdr_screen_wants_the_surface_where_the_compositor_switches_to_hdr(
        monkeypatch, fullscreen, compositor, refused, expected):
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    monkeypatch.setenv("RELAY_HDR_SURFACE", "")
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: True)
    monkeypatch.setattr(hdr_output, "compositor_auto_hdr", lambda: compositor)
    monkeypatch.setattr(hdr_output, "_seen_hdr", {})
    monkeypatch.setattr(hdr_output, "query", lambda window=None: SDR_DISPLAY)
    want, display = MpvPlayerView._wanted_surface(_player(window=_window(fullscreen), _auto_hdr_refused=refused))
    assert want is expected
    if expected:  # what the switch is expected to bring, until it does
        assert display == DisplayHdr(True, 1000.0, 203.0, "test", auto=True)


def test_the_expected_hdr_state_is_the_screens_last_hdr_reading(monkeypatch):
    monkeypatch.setattr(hdr_output, "_seen_hdr", {})
    monkeypatch.setattr(hdr_output, "platform_kind", lambda app=None: "wayland")
    monkeypatch.setattr(hdr_output, "_wayland_query", lambda name: DisplayHdr(True, 1107.0, 203.0, "wayland eDP-1"))
    hdr_output.query(None)
    sdr = DisplayHdr(False, 80.0, 80.0, "wayland eDP-1")
    assert hdr_output.expected_hdr(sdr) == DisplayHdr(True, 1107.0, 203.0, "wayland eDP-1", auto=True)
    assert hdr_output.expected_hdr(DisplayHdr(False, source="wayland DP-1")).peak_nits == 1000.0


def _auto_player(monkeypatch, display, fullscreen):
    from desktop_client.mpv_view import MpvPlayerView  # noqa: F401 - mpv present
    monkeypatch.setenv("RELAY_HDR_SURFACE", "")
    monkeypatch.setattr(hdr_output, "presentation_supported", lambda app=None: True)
    monkeypatch.setattr(hdr_output, "compositor_auto_hdr", lambda: True)
    monkeypatch.setattr(hdr_output, "_seen_hdr", {})
    monkeypatch.setattr(hdr_output, "query", lambda window=None: display[0])
    events = []
    player = _player(
        window=lambda: SimpleNamespace(isFullScreen=lambda: fullscreen[0]),
        _hdr_stale_reported=False, _task=object(), _local_playback=False, _on_presenter=False,
        _presenter=None, _display_hdr=SDR_DISPLAY, _observed={}, _epoch_released=True,
        hdr_surface_stale=SimpleNamespace(emit=lambda: events.append("reload")),
        hdr_output_changed=SimpleNamespace(emit=lambda on: events.append(("hdr_output", on))),
        log_message=SimpleNamespace(emit=lambda *args: events.append(("log",) + args)),
        _apply_output_target=lambda asynchronous=False: events.append("target"), hdr_output=True,
    )
    _bind(player, "_wanted_surface", "_follow_auto_hdr")
    return player, events


def _moved_to_surface(player):
    """What _prepare_surface does after the reload the check asked for."""
    from desktop_client.mpv_view import MpvPlayerView
    want, display = player._wanted_surface()
    player._hdr_stale_reported = False
    player._display_hdr = display
    player._on_presenter = want
    player._presenter = SimpleNamespace(pq=True, peak_nits=display.peak_nits, sdr_white_nits=display.sdr_white_nits,
                                        update=lambda: None) if want else None
    MpvPlayerView._follow_auto_hdr(player, want, display)


def test_fullscreen_hdr_moves_waits_for_the_switch_adopts_its_peak_and_leaves(monkeypatch):
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    display, fullscreen = [SDR_DISPLAY], [False]
    player, events = _auto_player(monkeypatch, display, fullscreen)
    MpvPlayerView._check_hdr_surface(player)
    assert events == []  # windowed on SDR: the widget fits
    fullscreen[0] = True
    MpvPlayerView._check_hdr_surface(player)
    assert events == ["reload"]
    _moved_to_surface(player)
    assert player._auto_hdr and player._auto_hdr_deadline is not None
    assert player._presenter.peak_nits == 1000.0  # until the screen says
    events.clear()
    MpvPlayerView._check_hdr_surface(player)
    assert events == []  # still SDR: waiting, on the surface
    display[0] = HDR_DISPLAY  # Hyprland switched
    MpvPlayerView._check_hdr_surface(player)
    assert events == ["target", ("hdr_output", True)]
    assert player._presenter.peak_nits == 1107.0 and player._auto_hdr_deadline is None
    events.clear()
    fullscreen[0] = False  # the screen still reads HDR for a moment
    MpvPlayerView._check_hdr_surface(player)
    assert events == ["reload"]
    _moved_to_surface(player)
    assert not player._on_presenter and player._auto_hdr  # its HDR is still the switch's
    events.clear()
    MpvPlayerView._check_hdr_surface(player)
    assert events == []  # windowed on the widget: no pull back to the surface
    display[0] = SDR_DISPLAY
    MpvPlayerView._check_hdr_surface(player)
    assert not player._auto_hdr and events == []


def test_a_switch_that_never_comes_sends_the_video_back_tone_mapped(monkeypatch):
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    display, fullscreen = [SDR_DISPLAY], [True]
    player, events = _auto_player(monkeypatch, display, fullscreen)
    _moved_to_surface(player)
    player._auto_hdr_deadline = time.monotonic() - 0.1  # a no_auto_hdr window rule, say
    MpvPlayerView._check_hdr_surface(player)
    assert player._auto_hdr_refused and events[-1] == "reload" and events[0][0] == "log"
    _moved_to_surface(player)
    assert not player._on_presenter  # and fullscreen again does not ask a second time


def test_back_on_the_widget_the_surface_drops_its_description_before_hiding():
    """Hyprland finds a fullscreen window's HDR surface even hidden."""
    pytest.importorskip("mpv")
    from desktop_client.mpv_view import MpvPlayerView
    calls = []
    presenter = SimpleNamespace(
        release_render_context=lambda: calls.append("release_ctx"), isVisible=lambda: True,
        release_description=lambda: calls.append("release_description"), hide=lambda: calls.append("hide"))
    player = _player(_presenter=presenter, _on_presenter=True, _ctx=object(),
                     options=SimpleNamespace(headless=True), context=lambda: None, update=lambda: None)
    MpvPlayerView._move_to_widget(player)
    assert calls == ["release_ctx", "release_description", "hide"]


def test_hyprland_reports_its_auto_hdr_option(monkeypatch):
    assert hdr_output._option_value({"option": "render:cm_auto_hdr", "int": 0, "set": False}) == 0
    assert hdr_output._option_value({"value": 2}) == 2
    assert hdr_output._option_value({"int": True}) is None and hdr_output._option_value([]) is None
    monkeypatch.delenv("HYPRLAND_INSTANCE_SIGNATURE", raising=False)
    assert not hdr_output.compositor_auto_hdr()  # not Hyprland
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "abc")
    monkeypatch.setattr(hdr_output, "platform_kind", lambda app=None: "wayland")
    monkeypatch.setattr(hdr_output, "_hyprland_option", lambda signature, name: 0)
    assert not hdr_output.compositor_auto_hdr()  # switched off
    monkeypatch.setattr(hdr_output, "_hyprland_option", lambda signature, name: None)
    assert hdr_output.compositor_auto_hdr()  # unknown: Hyprland's default, on


def test_hdr_output_is_on_by_default(tmp_path):
    from PySide6.QtCore import QSettings
    from desktop_client.settings import AppSettings
    QSettings("upscale-relay", "test-hdr-output-default").clear()
    settings = AppSettings(scope="test-hdr-output-default")
    assert settings.hdr_output
    settings.hdr_output = False
    assert not AppSettings(scope="test-hdr-output-default").hdr_output
    QSettings("upscale-relay", "test-hdr-output-default").clear()


# -- display queries -------------------------------------------------------------

def test_color_management_interfaces_follow_the_protocol():
    """libwayland dispatches by opcode: every request and event in order."""
    protocol = _Protocol()
    assert (protocol.manager.method_count, protocol.manager.event_count) == (7, 5)
    assert (protocol.output.method_count, protocol.output.event_count) == (2, 1)
    assert (protocol.description.method_count, protocol.description.event_count) == (2, 2)
    assert (protocol.info.method_count, protocol.info.event_count) == (0, 11)
    assert (protocol.creator.method_count, protocol.creator.event_count) == (10, 0)
    assert (protocol.surface.method_count, protocol.surface.event_count) == (3, 0)
    assert protocol.manager.methods[2].types[0] == ctypes.addressof(protocol.surface)
    assert protocol.manager.methods[5].types[0] == ctypes.addressof(protocol.creator)
    assert protocol.creator.methods[0].types[0] == ctypes.addressof(protocol.description)
    assert [(protocol.creator.methods[i].name, protocol.creator.methods[i].signature) for i in (5, 7)] == [
        (b"set_luminances", b"uuu"), (b"set_mastering_luminance", b"uu")]
    get_output = protocol.manager.methods[1]
    assert (get_output.name, get_output.signature) == (b"get_output", b"no")
    assert get_output.types[0] == ctypes.addressof(protocol.output) and get_output.types[1] is None
    assert protocol.description.methods[1].types[0] == ctypes.addressof(protocol.info)
    assert [protocol.info.events[i].name for i in (5, 6, 8)] == [b"tf_named", b"luminances", b"target_luminance"]


def test_a_pq_output_reads_as_hdr_with_its_luminances(monkeypatch):
    monkeypatch.setattr(hdr_output, "wayland_outputs", lambda display=None: [
        {"name": "DP-1", "complete": True, "tf_named": 9, "max_lum": 80, "reference_lum": 80},
        {"name": "eDP-1", "complete": True, "tf_named": 11, "max_lum": 10000, "reference_lum": 203,
         "target_max_lum": 1107},
    ])
    assert hdr_output._wayland_query("eDP-1") == DisplayHdr(True, 1107.0, 203.0, "wayland eDP-1")
    assert not hdr_output._wayland_query("DP-1").hdr
    assert hdr_output._wayland_query("HDMI-A-1") == hdr_output.SDR  # unknown, and not the only one


# -- describing the HDR surface (Wayland) -----------------------------------------------

class FakeWayland:
    """_WaylandOutputs' request plumbing, recording what it would send."""

    def __init__(self, answer):
        self.client = client = object.__new__(hdr_output._WaylandOutputs)
        client.protocol, client.display, client.manager = _Protocol(), 0, 1
        client.intents, client.features, client.tfs, client.primaries = {0}, {1, 4, 5}, {9, 11}, {1, 6}
        client.lib = SimpleNamespace(wl_proxy_get_class=lambda _p: b"wl_surface", wl_display_flush=lambda _d: 0)
        client.request, client.listen, client.roundtrip = self.request, self.listen, self.roundtrip
        self.sent, self.listeners, self.next_id, self.answer = [], {}, 100, answer

    def request(self, proxy, opcode, interface=None, *args, destroy=False, version=None):
        self.sent.append((proxy, opcode, tuple(value for _kind, value in args), destroy))
        if interface is not None:
            self.next_id += 1
            return self.next_id
        return None

    def listen(self, proxy, _signatures, handlers, _keep):
        self.listeners[proxy] = handlers

    def roundtrip(self):
        failed, ready = self.listeners[max(self.listeners)]
        if self.answer == "ready":
            ready(None, None, 7)
        else:
            failed(None, None, 1, b"unsupported")


def test_the_surface_declares_the_display_peak_as_its_range():
    """Hyprland tone-maps a surface whose luminance range exceeds the
    output's peak: PQ's default 10000 nits put mpv's output (already mapped
    to the 1107-nit panel) through a second curve, white 203 -> 111 nits."""
    wayland = FakeWayland("ready")
    assert wayland.client.can_describe_pq()
    cm_surface = wayland.client.describe_pq(0xABC000, None, 1107, 203)
    creator, description = 101, 102
    assert wayland.sent == [
        (1, 5, (0,), False),                       # create_parametric_creator
        (creator, 1, (11,), False),                # set_tf_named st2084_pq
        (creator, 3, (6,), False),                 # set_primaries_named bt2020
        (creator, 5, (50, 1107, 203), False),      # set_luminances: 0.005 .. peak, SDR white
        (creator, 7, (50, 1107), False),           # set_mastering_luminance
        (creator, 0, (0,), True),                  # create
        (1, 2, (0, 0xABC000), False),              # get_surface
        (cm_surface, 1, (description, 0), False),  # set_image_description, perceptual
        (description, 0, (), True),
    ]
    wayland.sent.clear()
    assert wayland.client.describe_pq(0xABC000, cm_surface, 1107, 250) == cm_surface
    assert [entry[:2] for entry in wayland.sent if entry[0] == 1] == [(1, 5)]  # no second get_surface


def test_a_refused_description_leaves_the_surface_alone():
    wayland = FakeWayland("failed")
    with pytest.raises(RuntimeError, match="unsupported"):
        wayland.client.describe_pq(0xABC000, None, 1107, 203)
    assert not [entry for entry in wayland.sent if entry[:2] == (1, 2)]  # no colour management surface
    assert wayland.sent[-1][1:] == (0, (), True)  # the description went


def test_without_parametric_luminances_qt_tags_the_surface():
    wayland = FakeWayland("ready")
    wayland.client.features = {1}  # parametric, no set_luminances
    assert not wayland.client.can_describe_pq()


@pytest.mark.parametrize(("describes", "color_space"), [
    (True, QColorSpace()),  # ours: Qt must not create a second colour management surface
    (False, QColorSpace(QColorSpace.NamedColorSpace.Bt2100Pq)),
])
def test_the_hdr_surface_is_tagged_by_qt_only_when_it_cannot_describe_itself(
        monkeypatch, scene, describes, color_space):
    monkeypatch.setattr(hdr_output, "can_describe_surfaces", lambda: describes)
    presenter = HdrPresenter(scene.view, scene.top.windowHandle(), pq=True, cover=True)
    assert presenter.format().colorSpace() == color_space
    assert presenter.format().redBufferSize() == 10


def test_the_hdr_surface_describes_itself_again_when_the_display_or_its_surface_changes(monkeypatch, scene):
    monkeypatch.setattr(hdr_output, "can_describe_surfaces", lambda: True)
    ids = {"surface": 3}
    calls = []
    monkeypatch.setattr(hdr_output, "surface_identity", lambda surface: (surface, ids["surface"]))

    def describe(surface, cm_surface, peak, white):
        calls.append(("describe", surface, cm_surface, peak, white))
        return cm_surface or len(calls)
    monkeypatch.setattr(hdr_output, "describe_pq_surface", describe)
    monkeypatch.setattr(hdr_output, "release_surface", lambda cm: calls.append(("release", cm)))
    presenter = HdrPresenter(scene.view, scene.top.windowHandle(), pq=True, cover=True,
                             peak_nits=1107.0, sdr_white_nits=203.0)
    presenter.winId = lambda: 0x50000000
    presenter._describe_surface()
    presenter._describe_surface()  # each frame: nothing new
    assert calls == [("describe", 0x50000000, None, 1107, 203)]
    presenter.sdr_white_nits = 250.0  # the brightness setting
    presenter._describe_surface()
    assert calls[-1] == ("describe", 0x50000000, 1, 1107, 250)
    ids["surface"] = 9  # Qt made a new wl_surface in the old one's memory
    presenter._describe_surface()
    assert calls[-2:] == [("release", 1), ("describe", 0x50000000, None, 1107, 250)]
    presenter.release_description()
    assert calls[-1] == ("release", 4)


def test_mpv_renders_bottom_up_for_the_gl_surface(monkeypatch, scene):
    """The picture is blitted into the window's framebuffer as GL counts,
    bottom-up: mpv must render flipped (its FBO 0 case), as the widget does.
    Unflipped, the laptop showed the picture upside down."""
    monkeypatch.setattr(hdr_output, "can_describe_surfaces", lambda: False)
    presenter = HdrPresenter(scene.view, scene.top.windowHandle(), pq=True)
    presenter._view = SimpleNamespace(_picture_aspect=lambda: None, _scaling=False)
    presenter._fbo = SimpleNamespace(width=lambda: 300, height=lambda: 180, handle=lambda: 5)
    renders, blits = [], []
    presenter.ctx = SimpleNamespace(update=lambda: True, render=lambda **kw: renders.append(kw))
    gl = SimpleNamespace(glBindFramebuffer=lambda *_: None, glBlitFramebuffer=lambda *a: blits.append(a[:8]),
                         glEnable=lambda *_: None, glScissor=lambda *_: None)
    presenter._paint_video(gl, 0, QRect(10, 20, 300, 180), 400)
    assert renders[0]["flip_y"] is True
    assert blits == [(0, 0, 300, 180, 10, 200, 310, 380)]  # bottom-left 400 - 20 - 180: not flipped again


def test_the_windows_swap_chain_carries_the_display_peak():
    from desktop_client.hdr_d3d11 import _hdr10_metadata  # noqa: PLC0415
    metadata = _hdr10_metadata(1107.0)
    assert [tuple(metadata.RedPrimary), tuple(metadata.GreenPrimary), tuple(metadata.BluePrimary),
            tuple(metadata.WhitePoint)] == [(35400, 14600), (8500, 39850), (6550, 2300), (15635, 16450)]
    assert (metadata.MaxMasteringLuminance, metadata.MinMasteringLuminance) == (11070000, 50)


@pytest.mark.skipif(not hasattr(ctypes, "WinDLL"), reason="DXGI")
def test_windows_lists_its_outputs():
    outputs = hdr_output.windows_outputs()
    assert all({"device", "monitor", "hdr", "max_lum"} <= set(o) for o in outputs)


# -- the overlay mirror -------------------------------------------------------------

class Card(QFrame):
    """A translucent card that counts its paints."""

    def __init__(self, parent, color):
        super().__init__(parent)
        self.color = QColor(color)
        self.paints = 0

    def paintEvent(self, _event):
        self.paints += 1
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.color)
        painter.end()


@pytest.fixture()
def scene(app):
    """A window: a sidebar and the player side by side, a card over the player."""
    top = QWidget()
    top.resize(400, 200)
    sidebar = QWidget(top)
    sidebar.setGeometry(0, 0, 100, 200)
    page = QWidget(top)
    page.setGeometry(100, 0, 300, 200)
    view = QWidget(page)
    view.setGeometry(0, 20, 300, 180)
    hint = Card(view, QColor(0, 0, 255, 255))  # a child of the player
    hint.setGeometry(10, 10, 20, 20)
    card = Card(top, QColor(255, 0, 0, 128))  # over the player, from higher up
    card.setGeometry(250, 100, 100, 50)
    outside = Card(top, QColor(0, 255, 0, 255))  # over the sidebar only
    outside.setGeometry(10, 10, 50, 50)
    top.show()
    app.processEvents()
    changes = []
    mirror = OverlayMirror(view, lambda: changes.append(True))
    yield SimpleNamespace(top=top, view=view, hint=hint, card=card, outside=outside,
                          mirror=mirror, changes=changes, app=app)
    top.close()
    app.processEvents()


def test_the_mirror_finds_what_stacks_above_the_player(scene):
    candidates = scene.mirror.candidates()
    assert candidates.index(scene.hint) < candidates.index(scene.card)  # bottom to top
    layers = scene.mirror.layers(1.0)
    assert [layer.widget for layer, _pos, _opacity in layers] == [scene.hint, scene.card]
    positions = {layer.widget: pos for layer, pos, _opacity in layers}
    assert positions[scene.card] == QPoint(150, 80)  # over the player, in its coordinates


def test_the_mirror_draws_the_overlay_premultiplied(scene):
    layer = next(layer for layer, _pos, _opacity in scene.mirror.layers(2.0) if layer.widget is scene.card)
    assert (layer.image.width(), layer.image.height()) == (200, 100)  # device pixels
    pixel = layer.image.pixelColor(50, 50)
    assert pixel.alpha() == 128 and pixel.red() >= 254 and pixel.green() == 0


def _count_renders(widget):
    """How many times the mirror renders ``widget`` (QWidget.render calls)."""
    calls = []
    render = widget.render
    widget.render = lambda *args: calls.append(args) or render(*args)
    return calls


def test_a_repaint_renders_again_but_a_move_does_not(scene):
    mirror, card = scene.mirror, scene.card
    mirror.layers(1.0)
    renders = _count_renders(card)
    card.update()
    scene.app.processEvents()
    assert scene.changes  # the surface is asked to compose
    mirror.layers(1.0)
    assert len(renders) == 1
    card.move(card.pos() + QPoint(0, 5))
    scene.app.processEvents()
    mirror.layers(1.0)
    assert len(renders) == 1  # the picture moves; it is not drawn again


def test_a_fade_applies_its_opacity_when_composing(scene):
    mirror, card = scene.mirror, scene.card
    effect = QGraphicsOpacityEffect(card)
    effect.setOpacity(0.25)
    card.setGraphicsEffect(effect)
    scene.app.processEvents()
    layer, _pos, opacity = next(item for item in mirror.layers(1.0) if item[0].widget is card)
    assert opacity == 0.25
    assert layer.image.pixelColor(50, 25).alpha() == 128  # drawn without the effect
    assert card.graphicsEffect() is effect and effect.isEnabled()
    scene.app.processEvents()
    renders = _count_renders(card)
    for step in (0.5, 0.75):
        effect.setOpacity(step)
        scene.app.processEvents()
        _layer, _pos, opacity = next(item for item in mirror.layers(1.0) if item[0].widget is card)
        assert opacity == step
    assert not renders  # fade steps are composed, not drawn again


def test_the_end_of_a_fade_draws_the_overlay_again(scene):
    """Fade steps are not drawn again, nor a repaint after a move. A fade
    that ends with a move (FadeSlide._place) would leave a change that came
    with a step unmirrored, unless the end of the fade draws it in full."""
    mirror, card = scene.mirror, scene.card
    effect = QGraphicsOpacityEffect(card)
    effect.setOpacity(0.5)
    card.setGraphicsEffect(effect)
    scene.app.processEvents()
    mirror.layers(1.0)
    scene.app.processEvents()  # the effect's own first paint
    mirror.layers(1.0)
    renders = _count_renders(card)
    effect.setOpacity(0.75)
    scene.app.processEvents()
    mirror.layers(1.0)
    assert not renders  # a fade step
    card.move(card.pos() + QPoint(0, 5))
    card.setGraphicsEffect(None)  # the fade ends where it lands
    scene.app.processEvents()
    _layer, _pos, opacity = next(item for item in mirror.layers(1.0) if item[0].widget is card)
    assert opacity == 1.0 and len(renders) == 1


def test_a_hidden_overlay_leaves_the_surface(scene):
    scene.mirror.layers(1.0)  # each compose watches what may come over the player
    scene.card.hide()
    scene.app.processEvents()
    assert scene.changes
    assert [layer.widget for layer, _pos, _opacity in scene.mirror.layers(1.0)] == [scene.hint]


# -- test media -------------------------------------------------------------------

def test_the_hdr_sample_is_10_bit_pq(tmp_path):
    av = pytest.importorskip("av")
    from upscale_cli.sample import make_hdr_sample, pq_code
    assert (pq_code(0), pq_code(10000)) == (64, 940)
    assert pq_code(100) < pq_code(203) < pq_code(1000)
    path = tmp_path / "hdr.mkv"
    make_hdr_sample(str(path), frames=3, width=128, height=72, fps=24)
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        cc = stream.codec_context
        assert (cc.name, cc.pix_fmt) == ("hevc", "yuv420p10le")
        assert (int(cc.color_primaries), int(cc.color_trc), int(cc.colorspace)) == (9, 16, 9)
        assert stream.average_rate == Fraction(24)


def test_a_widget_that_paints_part_of_itself_is_mirrored_in_part(scene):
    """The busy ring's widget spans the player but draws a disc in the middle:
    the mirror pictures only what mirror_rect names, and nothing when empty."""
    hint, part = scene.hint, [QRect(5, 5, 10, 8)]
    hint.mirror_rect = lambda: part[0]
    layer, pos, _opacity = next(item for item in scene.mirror.layers(2.0) if item[0].widget is hint)
    assert (layer.image.width(), layer.image.height()) == (20, 16)  # the part, in device pixels
    assert pos == QPoint(15, 15)  # the hint's place plus the part's
    assert layer.image.pixelColor(2, 2).blue() == 255
    part[0] = QRect()
    assert hint not in [layer.widget for layer, _pos, _opacity in scene.mirror.layers(2.0)]
