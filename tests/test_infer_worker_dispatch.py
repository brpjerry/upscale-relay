import sys
from types import ModuleType, SimpleNamespace

import pytest

from relay_server.pipeline import _require_gpu_session, _should_use_tensorrt
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


def test_packaged_onnx_check(monkeypatch):
    fake_onnx = ModuleType("onnx")
    fake_onnx.__version__ = "1.22.0"
    monkeypatch.setitem(sys.modules, "onnx", fake_onnx)
    assert infer_worker.worker_main(["--onnx-check"]) == 0


def test_frozen_release_reports_unavailable_tensorrt_clearly():
    with pytest.raises(RuntimeError, match="first-launch NVIDIA setup"):
        _should_use_tensorrt("tensorrt", {"DmlExecutionProvider", "CPUExecutionProvider"})

    assert _should_use_tensorrt("auto", {"DmlExecutionProvider"}) is False
    assert _should_use_tensorrt("auto", {"TensorrtExecutionProvider"}) is True


def test_streaming_refuses_a_session_that_fell_back_to_cpu():
    # A TensorRT provider whose native libraries fail to load is still listed
    # as available, so the fallback is only visible after session creation.
    fallen_back = SimpleNamespace(active_provider="CPUExecutionProvider")
    for ep in ("tensorrt", "auto", "cuda"):
        with pytest.raises(RuntimeError, match="needs GPU inference"):
            _require_gpu_session(fallen_back, ep)

    # Deliberate CPU use stays possible, and a GPU session is accepted.
    _require_gpu_session(fallen_back, "cpu")
    for provider in ("TensorrtExecutionProvider", "CUDAExecutionProvider",
                     "DmlExecutionProvider"):
        _require_gpu_session(SimpleNamespace(active_provider=provider), "tensorrt")


def test_missing_provider_information_is_not_treated_as_a_gpu_session():
    with pytest.raises(RuntimeError, match="unknown provider"):
        _require_gpu_session(SimpleNamespace(active_provider=None), "tensorrt")
    with pytest.raises(RuntimeError):
        _require_gpu_session(SimpleNamespace(), "tensorrt")


def test_worker_ready_line_carries_the_active_provider():
    """The parent learns the session's provider only from the handshake."""
    assert infer_worker.parse_ready_provider(
        b"READY 2 TensorrtExecutionProvider\n"
    ) == "TensorrtExecutionProvider"
    assert infer_worker.parse_ready_provider(
        b"READY 2 CPUExecutionProvider\n"
    ) == "CPUExecutionProvider"
    # A worker reporting no provider must not be mistaken for a GPU session.
    assert infer_worker.parse_ready_provider(b"READY 2\n") is None
    with pytest.raises(RuntimeError, match="unknown provider"):
        _require_gpu_session(
            SimpleNamespace(
                active_provider=infer_worker.parse_ready_provider(b"READY 2\n")
            ),
            "tensorrt",
        )


def test_packaged_provider_check_requires_nvidia_and_cpu_fallbacks(monkeypatch):
    fake_infer = ModuleType("upscale_cli.infer")
    fake_infer.ort = SimpleNamespace(
        get_available_providers=lambda: sorted(infer_worker._NVIDIA_PROVIDERS),
    )
    monkeypatch.setitem(sys.modules, "upscale_cli.infer", fake_infer)
    monkeypatch.delenv("UPSCALE_RELAY_ACTIVE_RUNTIME", raising=False)
    assert infer_worker.provider_check() == 0

    fake_infer.ort = SimpleNamespace(
        get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    assert infer_worker.provider_check() == 2
