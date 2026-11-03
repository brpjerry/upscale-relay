"""Platform policy for libmpv hardware decode in the embedded render path."""

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("mpv")

from desktop_client.mpv_view import _render_hwdec_mode


def test_linux_on_intel_decodes_zero_copy_with_copy_back_to_fall_back_on():
    assert _render_hwdec_mode(False, "linux", {}, vendor="0x8086") == "vaapi,auto-copy-safe"


@pytest.mark.parametrize("vendor", ["0x10de", "0x1002", None])
def test_other_linux_gpus_keep_copy_back(vendor):
    assert _render_hwdec_mode(False, "linux", {}, vendor=vendor) == "auto-copy-safe"


def test_other_platforms_retain_safe_auto_hwdec():
    assert _render_hwdec_mode(False, "win32") == "auto-safe"


def test_no_hwdec_always_forces_software_decode():
    assert _render_hwdec_mode(True, "linux") == "no"
    assert _render_hwdec_mode(True, "win32") == "no"


def test_relay_hwdec_names_another_mode_for_testing():
    assert _render_hwdec_mode(False, "linux", {"RELAY_HWDEC": "auto-copy-safe"}, vendor="0x8086") == "auto-copy-safe"
    assert _render_hwdec_mode(False, "linux", {"RELAY_HWDEC": " "}, vendor=None) == "auto-copy-safe"
    assert _render_hwdec_mode(True, "linux", {"RELAY_HWDEC": "vaapi"}) == "no"  # --no-hwdec wins


@pytest.mark.parametrize(("mode", "expected"), [
    ("vaapi", True), ("auto-safe", True), ("vaapi,auto-copy-safe", True),
    ("nvdec-copy,vaapi", True), ("nvdec-copy,vaapi-copy", False),
    ("auto-copy-safe", False), ("vaapi-copy", False), ("no", False), ("", False),
])
def test_which_hwdec_modes_hand_over_decoder_surfaces(mode, expected):
    from desktop_client.mpv_view import _zero_copy

    assert _zero_copy(mode) is expected


def test_an_unreadable_render_node_reads_as_no_vendor(monkeypatch):
    from desktop_client import mpv_view

    class Missing:
        def __init__(self, _path):
            pass

        def read_text(self):
            raise FileNotFoundError

    monkeypatch.setattr(mpv_view, "Path", Missing)
    assert mpv_view._vaapi_vendor() is None
    assert mpv_view._render_hwdec_mode(False, "linux", {}) == "auto-copy-safe"


def _freezable(**fields):
    from types import SimpleNamespace

    from desktop_client.mpv_view import MpvPlayerView

    updates = []
    player = SimpleNamespace(
        _freeze_on_stop=True, _frozen=False, _frozen_fbo="old", _on_presenter=False, _ctx=None,
        _presenter=None, _picture_due=False, _thaw_entry=None, update=lambda: updates.append(True),
        _presenting=lambda: False,
    )
    player.__dict__.update(fields)
    for name in ("_freeze_picture", "_note_load", "_on_video_restarted"):
        setattr(player, name, getattr(MpvPlayerView, name).__get__(player))
    return player, updates


def test_a_stop_under_zero_copy_freezes_until_the_loaded_file_restarts():
    player, updates = _freezable()
    player._freeze_picture()
    assert player._frozen and player._frozen_fbo is None  # nothing on screen: black meanwhile
    player._on_video_restarted(3)  # the stopped file's late restart, before any load
    assert player._frozen
    player._note_load(5)
    player._on_video_restarted(4)  # still an earlier file's
    assert player._frozen
    player._on_video_restarted(5)
    assert not player._frozen and player._picture_due and updates == [True]


def test_a_second_stop_before_the_restart_waits_for_the_next_load():
    player, _updates = _freezable()
    player._freeze_picture()
    player._note_load(5)
    player._freeze_picture()  # a newer seek retired file 5 before it restarted
    player._on_video_restarted(5)
    assert player._frozen
    player._note_load(6)
    player._on_video_restarted(6)
    assert not player._frozen


def test_a_load_without_an_entry_id_never_thaws_on_some_restart():
    player, _updates = _freezable()
    player._freeze_picture()
    player._note_load(None)
    player._on_video_restarted(3)
    player._on_video_restarted(None)
    assert player._frozen


@pytest.mark.parametrize(("version", "arguments"), [
    ((0, 41, 0), ("loadfile", b"/v.mkv", "replace", -1, "start=4.0,pause=yes")),
    ((0, 37, 0), ("loadfile", b"/v.mkv", "replace", "start=4.0,pause=yes")),
])
def test_loadfile_hands_the_new_entry_id_to_the_freeze(version, arguments):
    from types import SimpleNamespace

    from desktop_client.mpv_view import MpvPlayerView

    commands = []

    def command(*args):
        commands.append(args)
        return {"playlist_entry_id": 9}

    player, _updates = _freezable(mpv=SimpleNamespace(mpv_version_tuple=version, command=command))
    player._loadfile = MpvPlayerView._loadfile.__get__(player)
    player._freeze_picture()
    player._loadfile(b"/v.mkv", "start=4.0,pause=yes")
    assert commands == [arguments] and player._thaw_entry == 9


class _RenderContext:
    def __init__(self):
        self.renders = []

    def update(self):
        return True

    def render(self, **params):
        self.renders.append(params)


def _frame_ready_view(monkeypatch, **fields):
    from types import SimpleNamespace

    from desktop_client import mpv_view

    context = object()  # the view's own context, made current by makeCurrent
    monkeypatch.setattr(mpv_view, "QOpenGLContext", SimpleNamespace(currentContext=lambda: context))
    updates = []
    view = SimpleNamespace(
        _ctx=_RenderContext(), _frozen=False, _on_presenter=False, _report_display=False, _display_rate=0,
        _presenting=lambda: True, update=lambda: updates.append(True), makeCurrent=lambda: None,
        doneCurrent=lambda: None, context=lambda: context,
    )
    view.__dict__.update(fields)
    view._on_frame_ready = mpv_view.MpvPlayerView._on_frame_ready.__get__(view)
    return view, updates


def test_a_frozen_view_takes_mpvs_frames_without_drawing_them(monkeypatch):
    # mpv waits ~200 ms for each frame to be taken before it reports the
    # restart that ends the freeze; skip_rendering never maps a surface.
    view, updates = _frame_ready_view(monkeypatch, _frozen=True)
    view._on_frame_ready()
    assert view._ctx.renders == [{"skip_rendering": True}] and updates == []


def test_a_visible_view_repaints_for_each_frame_when_not_frozen(monkeypatch):
    view, updates = _frame_ready_view(monkeypatch)
    view._on_frame_ready()
    assert view._ctx.renders == [] and updates == [True]


@pytest.mark.parametrize(("exposed", "frozen", "takes", "repaints"), [
    (True, False, False, True), (True, True, True, False), (False, False, True, False), (False, True, True, False),
])
def test_the_hdr_surface_takes_frames_without_drawing_them_while_hidden_or_frozen(exposed, frozen, takes, repaints):
    from types import SimpleNamespace

    from desktop_client.hdr_view import HdrPresenter

    updates = []
    surface = SimpleNamespace(
        ctx=_RenderContext(), _view=SimpleNamespace(_frozen=frozen), isExposed=lambda: exposed,
        update=lambda: updates.append(True), makeCurrent=lambda: None, doneCurrent=lambda: None,
    )
    HdrPresenter.frame_ready(surface)
    assert (surface.ctx.renders == [{"skip_rendering": True}]) is takes and (updates == [True]) is repaints


def test_copy_back_never_freezes():
    player, _updates = _freezable(_freeze_on_stop=False)
    player._freeze_picture()
    assert not player._frozen


def test_on_the_hdr_surface_the_surface_keeps_its_own_picture():
    from types import SimpleNamespace

    calls = []
    presenter = SimpleNamespace(picture_due=lambda: calls.append("due"), update=lambda: calls.append("update"))
    player, _updates = _freezable(_on_presenter=True, _presenter=presenter, _frozen_fbo=None)
    player._freeze_picture()
    assert player._frozen and player._frozen_fbo is None  # the widget renders nothing for it
    player._note_load(7)
    player._on_video_restarted(7)
    # A paused seek queues no further frame: the surface must repaint now.
    assert not player._frozen and calls == ["due", "update"]
