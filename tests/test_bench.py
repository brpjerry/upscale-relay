from types import SimpleNamespace

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
