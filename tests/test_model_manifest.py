import json

import pytest

from relay_server.server import discover_models
from upscale_cli.manifest import ModelManifest, infer_scale_factor


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("AnimeJaNai.onnx", 2),
        ("2x_AnimeJaNai.onnx", 2),
        ("AnimeJaNai-3x.onnx", 3),
        ("realesrgan-x4plus.onnx", 4),
        ("MODEL_X6.onnx", 6),
        ("source-1080x1920.onnx", 2),
        ("conv3x3.onnx", 2),
    ],
)
def test_infer_scale_factor(filename, expected):
    assert infer_scale_factor(filename) == expected


def test_missing_manifest_is_generated(tmp_path):
    model_path = tmp_path / "upscaler-3x.onnx"
    model_path.touch()

    manifest = ModelManifest.load(model_path)

    assert manifest.scale_factor == 3
    assert manifest.channel_order == "rgb"
    assert manifest.value_range == (0.0, 1.0)
    assert json.loads(model_path.with_suffix(".json").read_text(encoding="utf-8")) == {
        "scale_factor": 3,
        "channel_order": "rgb",
        "value_range": [0.0, 1.0],
    }


def test_existing_manifest_is_preserved(tmp_path):
    model_path = tmp_path / "model-4x.onnx"
    manifest_path = model_path.with_suffix(".json")
    model_path.touch()
    manifest_path.write_text(
        '{"scale_factor": 3, "channel_order": "bgr", "value_range": [-1, 1]}',
        encoding="utf-8",
    )

    manifest = ModelManifest.load(model_path)

    assert manifest.scale_factor == 3
    assert manifest.channel_order == "bgr"
    assert manifest.value_range == (-1, 1)
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["scale_factor"] == 3


def test_discovery_generates_manifest_and_advertises_scale(tmp_path):
    model_path = tmp_path / "plain-model.onnx"
    model_path.touch()

    models = discover_models(str(tmp_path))

    assert models == {
        "plain-model": {"path": str(model_path), "scale_factor": 2},
    }
    assert model_path.with_suffix(".json").exists()
