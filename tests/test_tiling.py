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
