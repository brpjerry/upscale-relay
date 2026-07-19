import sys

import pytest

from relay_server.pipeline import _should_use_tensorrt
from upscale_cli import infer_worker


def test_source_worker_command_uses_python_module(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", "python.exe")

    command = infer_worker.build_worker_command(
        "model.onnx", "tensorrt", "none", "input-shm", "output-shm",
    )

    assert command[:3] == ["python.exe", "-m", "upscale_cli.infer_worker"]
    assert command[-4:] == ["--shm-in", "input-shm", "--shm-out", "output-shm"]


def test_frozen_worker_command_reenters_server_executable(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "upscale-relay-server.exe")

    command = infer_worker.build_worker_command(
        "model.onnx", "tensorrt", "1024", "input-shm", "output-shm",
    )

    assert command[:2] == ["upscale-relay-server.exe", infer_worker.FROZEN_WORKER_ARG]
    assert "-m" not in command


def test_frozen_worker_dispatch_strips_internal_argument(monkeypatch):
    received = []
    monkeypatch.setattr(
        infer_worker, "worker_main",
        lambda argv=None: received.append(argv) or 0,
    )

    assert infer_worker.maybe_run_frozen_worker(["--ordinary-server-arg"]) is None
    assert infer_worker.maybe_run_frozen_worker([
        infer_worker.FROZEN_WORKER_ARG, "--check",
    ]) == 0
    assert received == [["--check"]]


def test_frozen_release_reports_unavailable_tensorrt_clearly():
    with pytest.raises(RuntimeError, match="Windows release binaries include DirectML/CPU"):
        _should_use_tensorrt("tensorrt", {"DmlExecutionProvider", "CPUExecutionProvider"})

    assert _should_use_tensorrt("auto", {"DmlExecutionProvider"}) is False
    assert _should_use_tensorrt("auto", {"TensorrtExecutionProvider"}) is True
