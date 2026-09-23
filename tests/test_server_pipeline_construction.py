from fractions import Fraction
import sys
from types import SimpleNamespace

import pytest

from relay_server.pipeline import Pipeline, VideoConfig
import relay_server.pipeline as pipeline_mod
import upscale_cli.infer_worker as worker_mod


class _Owner:
    scale_factor = 2
    active_provider = "TensorrtExecutionProvider"

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


@pytest.mark.parametrize("failure", ["provider", "encoder", "aux", "mux", "decoder"])
def test_constructor_releases_every_allocated_owner(monkeypatch, failure):
    allocated = []

    def fail():
        raise RuntimeError(failure)

    def upscaler(*_args, **_kwargs):
        owner = _Owner()
        allocated.append(owner)
        return owner

    def select_encoder(*_args, **_kwargs):
        if failure == "encoder":
            fail()
        return "ffv1", "yuv420p", {}

    def open_aux(*_args, **_kwargs):
        if failure == "aux":
            fail()
        owner = _Owner()
        allocated.append(owner)
        return owner

    def open_mux(pipeline):
        owner = _Owner()
        allocated.append(owner)
        pipeline._mux = owner
        if failure == "mux":
            fail()

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        get_available_providers=lambda: ["TensorrtExecutionProvider"],
    ))
    monkeypatch.setattr(worker_mod, "SubprocessUpscaler", upscaler)
    monkeypatch.setattr(pipeline_mod, "select_encoder", select_encoder)
    monkeypatch.setattr(pipeline_mod.av, "open", open_aux)
    monkeypatch.setattr(Pipeline, "_open_mux", open_mux)
    monkeypatch.setattr(Pipeline, "_open_decoder", lambda _self: fail())
    if failure == "provider":
        monkeypatch.setattr(pipeline_mod, "_require_gpu_session", lambda *_args: fail())

    with pytest.raises(RuntimeError, match=failure):
        Pipeline(
            VideoConfig("h264", None, 32, 32, Fraction(1, 1000)),
            "model.onnx", "lossless-ffv1", (64, 64), lambda _: None, lambda _: None,
            aux_source_path="source.mkv",
        )
    assert allocated
    assert all(owner.closed == 1 for owner in allocated)


def test_invalid_geometry_is_rejected_before_loading_model(monkeypatch):
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    with pytest.raises(ValueError, match="positive"):
        Pipeline(
            VideoConfig("h264", None, 32, 32, Fraction(1, 1000)),
            "model.onnx", "lossless-ffv1", (0, 64), lambda _: None, lambda _: None,
        )


def test_thread_start_failure_releases_constructor_owned_mux(monkeypatch):
    import threading

    mux, auxiliary = _Owner(), _Owner()
    monkeypatch.setattr(pipeline_mod, "select_encoder", lambda *_a, **_k: ("ffv1", "yuv420p", {}))
    monkeypatch.setattr(pipeline_mod.av, "open", lambda *_a, **_k: auxiliary)
    monkeypatch.setattr(Pipeline, "_open_mux", lambda self: setattr(self, "_mux", mux))
    monkeypatch.setattr(Pipeline, "_open_decoder", lambda _self: object())
    original_start = threading.Thread.start

    def start(thread):
        if thread.name == "pl-finish":
            raise RuntimeError("cannot start finish")
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError, match="cannot start finish"):
        Pipeline(
            VideoConfig("h264", None, 32, 32, Fraction(1, 1000)), None,
            "lossless-ffv1", (64, 64), lambda _: None, lambda _: None,
            aux_source_path="source.mkv",
        )
    assert mux.closed == 1
    assert auxiliary.closed == 1
    assert not any(t.name.startswith("pl-") for t in threading.enumerate())
