"""Shared configuration edits must preserve the user's file and external changes."""
import os
from pathlib import Path

import pytest
pytest.importorskip("PySide6")
from desktop_client.mpv_config import MpvConfig


def test_comments_profiles_aliases_includes_and_crlf_survive(tmp_path):
    include = tmp_path / "extra.conf"
    include.write_text("slang=ja,en\nsub=no\n[cinema]\ntscale=mitchell\n")
    path = tmp_path / "mpv.conf"
    original = f'# personal settings\r\nvolume=65\r\nsid=3\r\ninclude="{include}"\r\nsid=auto # latest\r\n[film]\ninterpolation=yes\r\n'.encode()
    path.write_bytes(original)
    config = MpvConfig(path)
    assert config.read()["sid"] == "auto"
    assert config.values["slang"] == "ja,en"
    assert config.values["interpolation"] == "no"
    assert config.values["tscale"] == "oversample"
    values = config.write("sid", "no")
    assert values["sid"] == "no"
    assert path.read_bytes() == original + b'[default]\r\nsid="no"\r\n'
    assert include.read_text().startswith("slang=ja,en\nsub=no")


def test_edits_merge_external_changes_and_preserve_symlink(tmp_path):
    target = tmp_path / "real.conf"
    target.write_text("# first\nvideo-sync=display-desync\n")
    path = tmp_path / "mpv.conf"
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    config = MpvConfig(path)
    assert config.read()["video-sync"] == "display-desync"
    target.write_text("# external replacement\nvolume=72\ntscale=custom-kernel\n")
    config.write("slang", "en,ja")
    assert path.is_symlink()
    assert target.read_text().startswith("# external replacement\nvolume=72\ntscale=custom-kernel\n")
    assert config.values["tscale"] == "custom-kernel"


def test_missing_config_is_read_without_creating_it_and_unreadable_write_is_safe(tmp_path):
    path = tmp_path / "new" / "mpv.conf"
    config = MpvConfig(path)
    assert config.read()["sid"] == "auto"
    assert not path.exists()
    config.write("interpolation", "yes")
    before = path.read_bytes()
    path.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            config.write("sid", "no")
        assert path.read_bytes() == before
    finally:
        path.chmod(0o644)


def test_concurrent_edit_rejects_commit(tmp_path, monkeypatch):
    path = tmp_path / "mpv.conf"
    path.write_text("volume=50\n")
    config = MpvConfig(path)
    original_fsync = os.fsync
    def external_change(fd):
        path.write_text("volume=99\n")
        original_fsync(fd)
    monkeypatch.setattr(os, "fsync", external_change)
    with pytest.raises(RuntimeError, match="changed"):
        config.write("sid", "no")
    assert path.read_text() == "volume=99\n"
    assert not list(tmp_path.glob(".mpv-conf-*"))


def test_recursive_includes_fail_without_writing(tmp_path):
    path = tmp_path / "mpv.conf"
    path.write_text(f'include="{path}"\n')
    original = path.read_bytes()
    with pytest.raises(ValueError, match="Recursive"):
        MpvConfig(path).write("sid", "no")
    assert path.read_bytes() == original


def test_global_values_are_distinct_from_applied_profile_and_ordered_lists(tmp_path):
    path = tmp_path / "mpv.conf"
    path.write_text('sid=auto\nslang=ja\nslang-add=en\nslang-pre=fr\nslang-del=ja\n'
                    'profile=cinema\n[cinema]\nsid=no\nvideo-sync=display-resample\n'
                    '[default]\ntscale=%6%linear\n')
    config = MpvConfig(path)
    values = config.read()
    assert values["sid"] == "auto"
    assert config.effective_values["sid"] == "no"
    assert config.effective_values["video-sync"] == "display-resample"
    assert values["slang"] == "fr,en"
    assert values["tscale"] == "linear"
    config.write("sid", "2")
    config.read()
    assert config.effective_values["sid"] == "2"


def test_file_watcher_sees_atomic_replacement_and_missing_parent(tmp_path):
    import asyncio
    from PySide6.QtWidgets import QApplication
    from qt_helpers import playback_loop
    from desktop_client.mpv_config import ConfigWatcher
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "missing" / "mpv.conf"
    watcher = ConfigWatcher(MpvConfig(path))
    changes = []
    watcher.changed.connect(changes.append)
    async def wait_for(value):
        async with asyncio.timeout(3):
            while not changes or changes[-1]["sid"] != value:
                await asyncio.sleep(0.02)
    async def scenario():
        path.parent.mkdir()
        path.write_text("sid=no\n")
        await wait_for("no")
        replacement = path.with_suffix(".tmp")
        replacement.write_text("sid=3\n")
        replacement.replace(path)
        await wait_for("3")
        assert path.read_text() == "sid=3\n"  # refresh never writes back
    with playback_loop(app) as loop:
        loop.run_until_complete(scenario())
    watcher.timer.stop()
    watcher.deleteLater()
