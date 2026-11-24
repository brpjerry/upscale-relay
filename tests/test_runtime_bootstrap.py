from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

import pytest

from relay_server import runtime_bootstrap as runtime


def test_source_runtime_validation_runs_in_an_isolated_process(monkeypatch):
    from types import SimpleNamespace
    commands = []
    monkeypatch.delattr(runtime.sys, "frozen", raising=False)
    monkeypatch.setattr(runtime.subprocess, "run", lambda command, **kwargs: (
        commands.append(command) or SimpleNamespace(returncode=0)
    ))
    assert runtime.source_runtime_ready("cuda")
    assert commands[0][-2:] == [runtime.SOURCE_VALIDATE_ARG, "cuda"]
    monkeypatch.setattr(runtime.sys, "frozen", True, raising=False)
    assert not runtime.source_runtime_ready("cuda")
    assert len(commands) == 1


def test_source_runtime_dispatch_does_not_run_installer(monkeypatch):
    checked = []
    monkeypatch.setattr(runtime, "_validate_source_runtime",
                        lambda ep, *, saved_choice: checked.append((ep, saved_choice)))
    assert runtime.maybe_run_runtime_installer([runtime.SOURCE_VALIDATE_ARG, "cpu"]) == 0
    # The tray probes its saved preference, which may name a missing provider.
    assert checked == [("cpu", True)]


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
    monkeypatch.setattr(runtime, "missing_system_libraries", lambda: [])
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
    monkeypatch.setattr(runtime, "missing_system_libraries", lambda: [])
    monkeypatch.setattr(runtime, "_run_pip", lambda _target: 9)

    assert runtime.install_runtime() == 1
    assert not target.exists()
    assert not runtime.runtime_ready(target)


def _write_dll(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def test_activate_adds_external_packages_and_native_dll_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_LINUX", False)
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
        assert runtime._native_library_check(target, linux=False)[1] == []


def test_native_library_check_reports_absent_builder_resource(tmp_path):
    target = tmp_path / "runtime"
    _populate_native_libraries(target, ())
    _, missing = runtime._native_library_check(target, linux=False)
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


# Bugs caught: 0, False positives: 0
def test_linux_runtime_pins_match_the_source_extra():
    # Linux installs two of the same wheels; CUDA and cuDNN are the system's.
    with open(Path(__file__).parents[1] / "pyproject.toml", "rb") as stream:
        project = tomllib.load(stream)
    assert set(runtime.LINUX_RUNTIME_PACKAGES) <= set(
        project["project"]["optional-dependencies"]["nvidia"]
    )


def _ready_runtime(monkeypatch, tmp_path: Path) -> Path:
    target = _use_temp_runtime(monkeypatch, tmp_path)
    target.mkdir()
    (target / ".runtime-ready.json").write_text(
        json.dumps({"stack_id": runtime.RUNTIME_STACK_ID}), encoding="utf-8",
    )
    return target


# Bugs caught: 0, False positives: 0
def test_linux_activation_puts_the_runtime_first(monkeypatch, tmp_path):
    # A system python-onnxruntime* must not win over the runtime's TensorRT
    # build, and the worker learns where TensorRT is only from its environment.
    monkeypatch.setattr(runtime, "_LINUX", True)
    target = _ready_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "path", ["/usr/lib/python3/site-packages"])
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/other")
    monkeypatch.setenv("PATH", "original")
    monkeypatch.delenv("UPSCALE_RELAY_ACTIVE_RUNTIME", raising=False)

    assert runtime.activate_runtime()
    assert runtime.activate_runtime()  # idempotent

    assert sys.path == [str(target), "/usr/lib/python3/site-packages"]
    assert os.environ["LD_LIBRARY_PATH"] == os.pathsep.join(
        [str(target / "tensorrt_libs"), "/opt/other"]
    )
    assert os.environ["UPSCALE_RELAY_ACTIVE_RUNTIME"] == str(target)
    assert os.environ["PATH"] == "original"


# Bugs caught: 0, False positives: 0
def test_linux_installs_only_ort_and_tensorrt_without_dependencies(monkeypatch, tmp_path):
    import types

    calls = []
    fake = types.ModuleType("pip._internal.cli.main")
    fake.main = lambda args: calls.append(args) or 0
    monkeypatch.setitem(sys.modules, "pip._internal.cli.main", fake)
    monkeypatch.setattr(runtime, "_prepare_pip_resources", lambda: None)

    monkeypatch.setattr(runtime, "_LINUX", True)
    assert runtime._run_pip(tmp_path) == 0
    assert "--no-deps" in calls[0]
    assert calls[0][-2:] == list(runtime.LINUX_RUNTIME_PACKAGES)

    monkeypatch.setattr(runtime, "_LINUX", False)
    assert runtime._run_pip(tmp_path) == 0
    assert "--no-deps" not in calls[1]
    assert calls[1][-len(runtime.NVIDIA_RUNTIME_PACKAGES):] == list(
        runtime.NVIDIA_RUNTIME_PACKAGES
    )


# Bugs caught: 0, False positives: 0
def test_install_stops_before_downloading_without_system_cuda(monkeypatch, tmp_path, capsys):
    target = _use_temp_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime, "missing_system_libraries", lambda: ["libcudnn.so.9 (package cudnn)"],
    )
    monkeypatch.setattr(runtime, "_run_pip", lambda *a, **k: pytest.fail("downloaded"))

    assert runtime.install_runtime() == 1
    assert "libcudnn.so.9 (package cudnn)" in capsys.readouterr().err
    assert not target.exists()


# Bugs caught: 0, False positives: 0
def test_linux_prune_drops_only_windows_builder_resources(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_LINUX", True)
    libs = tmp_path / "tensorrt_libs"
    libs.mkdir()
    names = (
        "libnvinfer.so.10",
        "libnvinfer_builder_resource_sm120.so.10.16.1",
        "libnvinfer_builder_resource_win_sm120.so.10.16.1",
        "libnvinfer_builder_resource_win_ptx.so.10.16.1",
    )
    for name in names:
        (libs / name).write_bytes(b"")
    runtime._prune_runtime(tmp_path)
    assert sorted(path.name for path in libs.iterdir()) == sorted(names[:2])


# Bugs caught: 0, False positives: 0
def test_linux_native_library_check(tmp_path):
    libs = tmp_path / "tensorrt_libs"
    libs.mkdir()
    for name in (*runtime._TENSORRT_LINUX_LIBRARIES,
                 "libnvinfer_builder_resource_sm120.so.10.16.1"):
        (libs / name).write_bytes(b"")
    capi = tmp_path / "onnxruntime" / "capi"
    capi.mkdir(parents=True)
    (capi / "libonnxruntime_providers_tensorrt.so").write_bytes(b"")
    libraries, missing = runtime._native_library_check(tmp_path, linux=True)
    assert missing == []
    assert libraries[0].name == "libnvinfer.so.10"

    (libs / "libnvinfer_builder_resource_sm120.so.10.16.1").unlink()
    (libs / "libnvonnxparser.so.10").unlink()
    _, missing = runtime._native_library_check(tmp_path, linux=True)
    assert len(missing) == 2


# Bugs caught: 0, False positives: 0
def test_missing_system_libraries_names_the_arch_package(monkeypatch):
    import ctypes

    monkeypatch.setattr(runtime, "_LINUX", True)

    def fake_cdll(name, *args, **kwargs):
        if name == "libcudnn.so.9":
            raise OSError(name)
        return object()

    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    assert runtime.missing_system_libraries() == ["libcudnn.so.9 (package cudnn)"]
    monkeypatch.setattr(runtime, "_LINUX", False)
    assert runtime.missing_system_libraries() == []
