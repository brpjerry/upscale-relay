from types import SimpleNamespace
import tracemalloc
import sys

import av
import numpy as np
import pytest

from upscale_cli import bench


@pytest.mark.parametrize("fit, expected", [(None, (256, 192)), ((160, 90), (120, 90))])
def test_end_to_end_encodes_actual_upscaled_and_fitted_geometry(tmp_path, monkeypatch, fit, expected):
    monkeypatch.setattr(bench, "select_encoder", lambda tier: (
        "libx264", "yuv420p", {"preset": "ultrafast"},
    ))
    up = SimpleNamespace(infer_array=lambda frame: frame.repeat(2, 0).repeat(2, 1))
    frames = [np.zeros((96, 128, 3), dtype=np.uint8) for _ in range(3)]
    assert bench._bench_end_to_end(up, frames, tmp_path, fit) > 0
    with av.open(str(tmp_path / "e2e.mkv")) as output:
        decoded = list(output.decode(video=0))
    assert len(decoded) == 3
    assert {(frame.width, frame.height) for frame in decoded} == {expected}


@pytest.mark.parametrize("fails", [False, True])
def test_benchmark_removes_temporary_media_on_success_and_failure(tmp_path, monkeypatch, fails):
    (tmp_path / "model.onnx").touch()
    directories = []

    def run(models, output, frames, ep, fit, workdir):
        directories.append(workdir)
        (workdir / "large-lossless.mkv").write_bytes(b"temporary media")
        if fails:
            raise RuntimeError("model failed")

    monkeypatch.setattr(bench, "_run_bench", run)
    if fails:
        with pytest.raises(RuntimeError, match="model failed"):
            bench.run_bench(str(tmp_path), str(tmp_path / "report.md"))
    else:
        bench.run_bench(str(tmp_path), str(tmp_path / "report.md"))
    assert len(directories) == 1
    assert not directories[0].exists()


def test_synthetic_frames_are_repeatable_with_bounded_retained_memory():
    tracemalloc.start()
    try:
        frames = bench._synthetic_frames(160, 90, 1000)
        baseline, _ = tracemalloc.get_traced_memory()
        for frame in frames:
            assert frame.shape == (90, 160, 3)
        retained, peak = tracemalloc.get_traced_memory()
        assert retained - baseline < 500_000
        assert peak < 1_000_000  # 1000 retained images would exceed 40 MB.
    finally:
        tracemalloc.stop()
    assert len(frames) == 1000
    assert np.array_equal(frames[3], np.roll(frames[0], shift=12, axis=1))
    assert np.array_equal(frames[2:5][1], frames[3])


def test_benchmark_still_writes_report_without_nvenc(tmp_path, monkeypatch):
    (tmp_path / "model.onnx").touch()
    up = SimpleNamespace(scale_factor=1, infer_array=lambda frame: frame,
                         infer_array_tiled=lambda frame, tile: frame)
    monkeypatch.setitem(sys.modules, "upscale_cli.infer", SimpleNamespace(OnnxUpscaler=lambda *a, **kw: up))
    monkeypatch.setattr(bench, "SIZES", {"small": (64, 64)})
    monkeypatch.setattr(bench, "_bench_decode", lambda *args: 100.0)

    def select(tier):
        if tier == "lossless-ffv1":
            return "libx264", "yuv420p", {"preset": "ultrafast"}
        raise RuntimeError("NVENC unavailable")

    monkeypatch.setattr(bench, "select_encoder", select)
    report = tmp_path / "report.md"
    bench.run_bench(str(tmp_path), str(report), frames=3, ep="cpu")
    text = report.read_text()
    assert "e2e fps (hevc-qp18)" in text
    assert "| small |" in text
    assert text.count("unavailable") == len(bench.TIERS)


@pytest.mark.parametrize("stage", ["decode", "encode"])
def test_encode_failure_closes_container_before_temporary_cleanup(tmp_path, monkeypatch, stage):
    (tmp_path / "model.onnx").touch()
    opened = []
    directories = []

    class Container:
        def __init__(self, path, **kwargs):
            self.file = open(path, "wb")
            opened.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            self.file.close()

        def add_stream(self, *args, **kwargs):
            def fail_encode(frame):
                raise RuntimeError("encoder failure")
            return SimpleNamespace(encode=fail_encode)

    def run(models, output, frames, ep, fit, workdir):
        directories.append(workdir)
        if stage == "decode":
            bench._bench_decode(64, 64, 1, workdir)
        else:
            bench._bench_encode(64, 64, bench._synthetic_frames(64, 64, 1), "lossless-ffv1", workdir)

    monkeypatch.setattr(bench.av, "open", Container)
    monkeypatch.setattr(bench, "select_encoder", lambda tier: ("ffv1", "yuv420p", {}))
    monkeypatch.setattr(bench, "_run_bench", run)
    try:
        with pytest.raises(RuntimeError, match="encoder failure"):
            bench.run_bench(str(tmp_path), str(tmp_path / "report.md"))
        assert opened and all(container.file.closed for container in opened)
        assert not directories[0].exists()
    finally:
        for container in opened:
            container.close()
