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


def _write_dll(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def test_activate_adds_external_packages_and_native_dll_dirs(monkeypatch, tmp_path):
    target = _use_temp_runtime(monkeypatch, tmp_path)
    # cuDNN keeps its libraries directly under bin/, while the CUDA 13 wheels
    # nest theirs one level deeper; both directories must be exposed.
    cudnn_dir = _write_dll(
        target / "nvidia" / "cudnn" / "bin" / "cudnn64_9.dll"
    ).parent
    cuda_dir = _write_dll(
        target / "nvidia" / "cu13" / "bin" / "x86_64" / "cudart64_13.dll"
    ).parent
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
    assert str(cudnn_dir) in os.environ["PATH"]
    assert str(cuda_dir) in os.environ["PATH"]
    assert set(handles) == {str(cudnn_dir), str(cuda_dir)}
    assert runtime._dll_directory_handles


def _populate_native_libraries(target: Path, builder_resources: tuple[str, ...]) -> None:
    major = runtime._TENSORRT_MAJOR
    for name in (
        f"nvinfer_{major}.dll",
        f"nvinfer_plugin_{major}.dll",
        f"nvonnxparser_{major}.dll",
        *builder_resources,
    ):
        _write_dll(target / "tensorrt_libs" / name)
    _write_dll(
        target / "onnxruntime" / "capi" / "onnxruntime_providers_tensorrt.dll"
    )


def test_native_library_check_accepts_per_architecture_builder_resources(tmp_path):
    major = runtime._TENSORRT_MAJOR
    # TensorRT ships the builder resource split per GPU architecture; sm120 is
    # the Blackwell one.  The older single-file name must still satisfy it.
    for resources in (
        (f"nvinfer_builder_resource_sm120_{major}.dll",
         f"nvinfer_builder_resource_ptx_{major}.dll"),
        (f"nvinfer_builder_resource_{major}.dll",),
    ):
        target = tmp_path / f"runtime-{len(resources)}"
        _populate_native_libraries(target, resources)
        assert runtime._native_library_check(target)[1] == []


def test_native_library_check_reports_absent_builder_resource(tmp_path):
    target = tmp_path / "runtime"
    _populate_native_libraries(target, ())
    _, missing = runtime._native_library_check(target)
    assert len(missing) == 1
    assert "nvinfer_builder_resource" in missing[0]


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
