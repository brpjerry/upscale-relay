import pytest

pytest.importorskip("onnxruntime")  # inference runtime is an optional extra

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.make_test_model import build
from upscale_cli.infer import OnnxUpscaler


@pytest.fixture(scope="module")
def model_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("models") / "bilinear2x.onnx"
    build(path)
    return str(path)


def test_tiled_matches_untiled(model_path):
    rng = np.random.default_rng(42)
    rgb = rng.integers(0, 256, size=(200, 320, 3), dtype=np.uint8)

    up = OnnxUpscaler(model_path, ep="cpu")
    full = up.infer_array(rgb)
    assert full.shape == (400, 640, 3)

    up.scale_factor = up.manifest.scale_factor
    tiled = up.infer_array_tiled(rgb, tile=96)
    assert tiled.shape == full.shape
    # Bilinear receptive field (1px) << overlap/2 (8px): must match exactly
    # apart from rounding.
    assert int(np.abs(tiled.astype(int) - full.astype(int)).max()) <= 1


def test_tile_grid_covers_frame(model_path):
    up = OnnxUpscaler(model_path, ep="cpu")
    starts = up._tile_starts(200, 96)
    assert starts[0] == 0
    assert starts[-1] == 200 - 96
    # consecutive tiles overlap by at least `overlap`
    for a, b in zip(starts, starts[1:]):
        assert b - a <= 96 - up.overlap


def test_odd_overlap_rejected(model_path):
    with pytest.raises(ValueError):
        OnnxUpscaler(model_path, ep="cpu", overlap=15)


@pytest.mark.parametrize(("tile_size", "overlap"), [(8, 16), (16, 16), (0, 16), (-64, 16), (64, -2)])
def test_tiles_that_cannot_cover_a_frame_are_rejected_up_front(model_path, tile_size, overlap):
    # tile 8 / overlap 16 used to step backwards and return an unwritten
    # np.empty output; tile == overlap failed later with a zero range step.
    with pytest.raises(ValueError, match="tile"):
        OnnxUpscaler(model_path, ep="cpu", tile_size=tile_size, overlap=overlap)


@pytest.mark.parametrize("tile", [8, 16])
def test_direct_tiled_inference_rejects_tiles_within_the_overlap(model_path, tile):
    up = OnnxUpscaler(model_path, ep="cpu")
    with pytest.raises(ValueError, match="tile"):
        up.infer_array_tiled(np.full((80, 80, 3), 127, np.uint8), tile)


def test_smallest_valid_tile_writes_every_output_pixel(model_path):
    up = OnnxUpscaler(model_path, ep="cpu", tile_size=18)  # one past the overlap
    rgb = np.full((80, 80, 3), 127, np.uint8)
    assert np.array_equal(up._infer_with_fallback(rgb), up.infer_array(rgb))


def test_worker_facade_accepts_sources_beyond_the_tensorrt_profile(model_path):
    # The pipeline tiles sources larger than 2560x1440 inside the worker; the
    # shared-memory handoff in front of it must not reject them first.
    from upscale_cli.infer_worker import SubprocessUpscaler

    rng = np.random.default_rng(4)
    rgb = rng.integers(0, 256, size=(160, 2600, 3), dtype=np.uint8)
    reference = OnnxUpscaler(model_path, ep="cpu", tile_size=1024)._infer_with_fallback(rgb)
    worker = SubprocessUpscaler(model_path, ep="cpu", tile_size=1024, max_input_hw=rgb.shape[:2])
    try:
        assert np.array_equal(worker.infer_array(rgb), reference)
        with pytest.raises(ValueError, match="exceeds"):
            worker.infer_array(np.zeros((160, 2601, 3), np.uint8))
    finally:
        worker.close()
