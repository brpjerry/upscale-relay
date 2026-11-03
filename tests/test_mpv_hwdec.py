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
    ("auto-copy-safe", False), ("vaapi-copy", False), ("no", False), ("", False),
])
def test_which_hwdec_modes_hand_over_decoder_surfaces(mode, expected):
    from desktop_client.mpv_view import _zero_copy

    assert _zero_copy(mode) is expected


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


def test_a_libmpv_without_entry_ids_thaws_on_the_next_restart_after_the_load():
    player, _updates = _freezable()
    player._freeze_picture()
    player._on_video_restarted(None)
    assert player._frozen
    player._note_load(None)
    player._on_video_restarted(None)
    assert not player._frozen


def test_copy_back_never_freezes():
    player, _updates = _freezable(_freeze_on_stop=False)
    player._freeze_picture()
    assert not player._frozen


def test_on_the_hdr_surface_the_surface_keeps_its_own_picture():
    from types import SimpleNamespace

    due = []
    player, _updates = _freezable(_on_presenter=True, _presenter=SimpleNamespace(picture_due=lambda: due.append(1)))
    player._freeze_picture()
    assert player._frozen and player._frozen_fbo == "old"  # the widget's is not touched
    player._note_load(7)
    player._on_video_restarted(7)
    assert not player._frozen and due == [1]
