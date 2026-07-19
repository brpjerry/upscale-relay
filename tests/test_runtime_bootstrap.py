from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

from relay_server import runtime_bootstrap as runtime


def _use_temp_runtime(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("UPSCALE_RELAY_RUNTIME_DIR", str(tmp_path))
    return tmp_path / runtime.RUNTIME_STACK_ID


def test_runtime_ready_requires_matching_marker(monkeypatch, tmp_path):
    target = _use_temp_runtime(monkeypatch, tmp_path)
    target.mkdir()
    assert not runtime.runtime_ready()

    (target / ".runtime-ready.json").write_text(
        json.dumps({"stack_id": "old-stack"}), encoding="utf-8",
    )
    assert not runtime.runtime_ready()

    (target / ".runtime-ready.json").write_text(
        json.dumps({"stack_id": runtime.RUNTIME_STACK_ID}), encoding="utf-8",
    )
    assert runtime.runtime_ready()


def test_install_publishes_only_after_validation(monkeypatch, tmp_path):
    target = _use_temp_runtime(monkeypatch, tmp_path)
    events = []

    def fake_pip(staging):
        events.append(("pip", staging))
        (staging / "onnxruntime").mkdir()
        return 0

    def fake_validate(staging):
        events.append(("validate", staging))
        return 0

    monkeypatch.setattr(runtime, "_run_pip", fake_pip)
    monkeypatch.setattr(runtime, "_run_validation_process", fake_validate)

    assert runtime.install_runtime() == 0
    assert runtime.runtime_ready(target)
    assert (target / "onnxruntime").is_dir()
    assert [event[0] for event in events] == ["pip", "validate"]
    assert not list(tmp_path.glob("*.installing-*"))


def test_failed_install_is_not_activated(monkeypatch, tmp_path):
    target = _use_temp_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "_run_pip", lambda _target: 9)

    assert runtime.install_runtime() == 1
    assert not target.exists()
    assert not runtime.runtime_ready(target)


def test_activate_adds_external_packages_and_native_dll_dirs(monkeypatch, tmp_path):
    target = _use_temp_runtime(monkeypatch, tmp_path)
    dll_dir = target / "nvidia" / "cudnn" / "bin"
    dll_dir.mkdir(parents=True)
    (target / ".runtime-ready.json").write_text(
        json.dumps({"stack_id": runtime.RUNTIME_STACK_ID}), encoding="utf-8",
    )
    handles = []
    monkeypatch.setattr(
        os, "add_dll_directory", lambda path: handles.append(path) or object(),
        raising=False,
    )
    monkeypatch.setattr(sys, "path", list(sys.path))
    runtime._dll_directory_handles.clear()

    assert runtime.activate_runtime()
    assert str(target) in sys.path
    assert str(dll_dir) in os.environ["PATH"]
    assert handles == [str(dll_dir)]
    assert runtime._dll_directory_handles


def test_frozen_installer_dispatch_and_command(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "install_runtime", lambda: 7)
    assert runtime.maybe_run_runtime_installer(["ordinary"]) is None
    assert runtime.maybe_run_runtime_installer([runtime.RUNTIME_INSTALL_ARG]) == 7
    assert runtime.maybe_run_runtime_installer([
        runtime.RUNTIME_INSTALL_CHECK_ARG,
    ]) == 0

    validated = []
    monkeypatch.setattr(runtime, "_validate_runtime", validated.append)
    assert runtime.maybe_run_runtime_installer([
        runtime.RUNTIME_VALIDATE_ARG, "C:/runtime-staging",
    ]) == 0
    assert validated == [Path("C:/runtime-staging")]

    pip_calls = []
    monkeypatch.setattr(
        runtime, "_run_pip",
        lambda target, packages, ignore_installed=False:
            pip_calls.append((target, packages, ignore_installed)) or 0,
    )
    smoke_target = tmp_path / "pip-smoke"
    assert runtime.maybe_run_runtime_installer([
        runtime.RUNTIME_INSTALL_SMOKE_ARG, str(smoke_target),
    ]) == 0
    assert pip_calls == [(
        smoke_target, ("humanfriendly==10.0",), True,
    )]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "server.exe")
    assert runtime.installer_command() == ["server.exe", runtime.RUNTIME_INSTALL_ARG]
    assert runtime._validation_command(Path("C:/stage")) == [
        "server.exe", runtime.RUNTIME_VALIDATE_ARG, str(Path("C:/stage")),
    ]


def test_source_extra_matches_first_run_package_pins():
    with open(Path(__file__).parents[1] / "pyproject.toml", "rb") as stream:
        project = tomllib.load(stream)
    assert set(project["project"]["optional-dependencies"]["nvidia"]) == set(
        runtime.NVIDIA_RUNTIME_PACKAGES
    )
