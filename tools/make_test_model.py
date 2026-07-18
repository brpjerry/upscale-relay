"""Generate a synthetic 2x bilinear-upscale ONNX model for pipeline testing.

Not a real SR model — it exists so the inference plumbing (manifest, EP
selection, pre/post-processing, tiling) can be exercised without downloading
weights. Usage: python tools/make_test_model.py [models/bilinear2x.onnx]
"""

import json
import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper


def build(path: Path, scale: int = 2) -> None:
    scales = helper.make_tensor("scales", TensorProto.FLOAT, [4], np.array([1, 1, scale, scale], np.float32))
    resize = helper.make_node(
        "Resize",
        inputs=["input", "", "scales"],
        outputs=["output"],
        mode="linear",
        coordinate_transformation_mode="pytorch_half_pixel",
    )
    graph = helper.make_graph(
        [resize],
        "bilinear2x",
        inputs=[helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 3, "h", "w"])],
        outputs=[helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, "h2", "w2"])],
        initializer=[scales],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)], ir_version=10)
    onnx.checker.check_model(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))
    path.with_suffix(".json").write_text(
        json.dumps({"scale_factor": scale, "channel_order": "rgb", "value_range": [0.0, 1.0]}, indent=2)
    )
    print(f"wrote {path} and {path.with_suffix('.json').name}")


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("models/bilinear2x.onnx")
    build(target)
