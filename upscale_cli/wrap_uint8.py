"""Wrap an SR model graph with GPU-side pre/post-processing.

CPU-side normalization was the pipeline's biggest cost (float passes over
multi-megapixel arrays are memory-bandwidth-bound). This rewrites the graph
so the session's contract becomes:

    uint8 [N,H,W,3] (RGB, straight from the decoded frame)
        -> Cast -> Transpose NCHW -> [reverse channels] -> normalize
        -> original model
        -> denormalize -> Round -> Clip -> Cast uint8 -> Transpose NHWC

All conversion work runs on the execution provider. Falls back cleanly: the
caller keeps the numpy path if wrapping fails.
"""

from __future__ import annotations

import numpy as np
import onnx
from onnx import TensorProto, helper

_PREFIX = "u8wrap_"


def _scalar(name: str, value: float, elem_type: int) -> onnx.TensorProto:
    if elem_type == TensorProto.FLOAT16:
        return helper.make_tensor(name, elem_type, [], np.array([value], np.float16).tobytes(), raw=True)
    return helper.make_tensor(name, elem_type, [], [float(value)])


def wrap_uint8_io(model: onnx.ModelProto, value_range: tuple[float, float],
                  channel_order: str = "rgb") -> onnx.ModelProto:
    """Returns a new ModelProto with uint8 NHWC input 'u8wrap_in' and uint8
    NHWC output 'u8wrap_out'. Raises on models this doesn't fit (multiple
    inputs/outputs, non-float IO)."""
    model = onnx.ModelProto.FromString(model.SerializeToString())  # deep copy
    g = model.graph
    if len(g.input) != 1 or len(g.output) != 1:
        raise ValueError("expected exactly one input and one output")
    orig_in = g.input[0]
    orig_out = g.output[0]
    elem = orig_in.type.tensor_type.elem_type
    if elem not in (TensorProto.FLOAT, TensorProto.FLOAT16):
        raise ValueError("model input is not float32/float16")
    lo, hi = value_range

    pre_nodes = []
    post_nodes = []
    inits = [
        _scalar(_PREFIX + "norm", (hi - lo) / 255.0, elem),
        _scalar(_PREFIX + "denorm", 255.0 / (hi - lo), elem),
        _scalar(_PREFIX + "clip_min", 0.0, elem),
        _scalar(_PREFIX + "clip_max", 255.0, elem),
    ]
    if lo:
        inits += [_scalar(_PREFIX + "lo", lo, elem)]
    if channel_order == "bgr":
        inits += [helper.make_tensor(_PREFIX + "chidx", TensorProto.INT64, [3], [2, 1, 0])]

    # -- pre: uint8 NHWC -> float NCHW normalized ---------------------------------
    pre_nodes.append(helper.make_node("Cast", [_PREFIX + "in"], [_PREFIX + "f"],
                                      name=_PREFIX + "cast_in", to=elem))
    pre_nodes.append(helper.make_node("Transpose", [_PREFIX + "f"], [_PREFIX + "nchw"],
                                      name=_PREFIX + "nhwc2nchw", perm=[0, 3, 1, 2]))
    cur = _PREFIX + "nchw"
    if channel_order == "bgr":
        pre_nodes.append(helper.make_node("Gather", [cur, _PREFIX + "chidx"], [_PREFIX + "swapped"],
                                          name=_PREFIX + "swap_in", axis=1))
        cur = _PREFIX + "swapped"
    if lo:
        pre_nodes.append(helper.make_node("Mul", [cur, _PREFIX + "norm"], [_PREFIX + "scaled"],
                                          name=_PREFIX + "mul_in"))
        pre_nodes.append(helper.make_node("Add", [_PREFIX + "scaled", _PREFIX + "lo"], [orig_in.name],
                                          name=_PREFIX + "add_in"))
    else:
        pre_nodes.append(helper.make_node("Mul", [cur, _PREFIX + "norm"], [orig_in.name],
                                          name=_PREFIX + "mul_in"))

    # -- post: float NCHW -> uint8 NHWC ---------------------------------------------
    # Explicit Cast first: converted models sometimes carry a different actual
    # output dtype than their declared value_info (seen with fp16 conversions).
    post_nodes.append(helper.make_node("Cast", [orig_out.name], [_PREFIX + "outf"],
                                       name=_PREFIX + "cast_norm", to=elem))
    cur = _PREFIX + "outf"
    if lo:
        post_nodes.append(helper.make_node("Sub", [cur, _PREFIX + "lo"], [_PREFIX + "shifted"],
                                           name=_PREFIX + "sub_out"))
        cur = _PREFIX + "shifted"
    post_nodes.append(helper.make_node("Mul", [cur, _PREFIX + "denorm"], [_PREFIX + "descaled"],
                                       name=_PREFIX + "mul_out"))
    post_nodes.append(helper.make_node("Round", [_PREFIX + "descaled"], [_PREFIX + "rounded"],
                                       name=_PREFIX + "round_out"))
    post_nodes.append(helper.make_node(
        "Clip", [_PREFIX + "rounded", _PREFIX + "clip_min", _PREFIX + "clip_max"],
        [_PREFIX + "clipped"], name=_PREFIX + "clip_out"))
    cur = _PREFIX + "clipped"
    if channel_order == "bgr":
        post_nodes.append(helper.make_node("Gather", [cur, _PREFIX + "chidx"], [_PREFIX + "swapback"],
                                           name=_PREFIX + "swap_out", axis=1))
        cur = _PREFIX + "swapback"
    # Tail order matters twice over: TensorRT allows uint8 only at network
    # boundaries (so Cast must come after everything else), and measured
    # engines with a float Transpose -> Cast NHWC tail are 3-5x FASTER than a
    # direct NCHW uint8 output (fresh caches, idle GPU, both shapes).
    post_nodes.append(helper.make_node("Transpose", [cur], [_PREFIX + "nhwc"],
                                       name=_PREFIX + "nchw2nhwc", perm=[0, 2, 3, 1]))
    post_nodes.append(helper.make_node("Cast", [_PREFIX + "nhwc"], [_PREFIX + "out"],
                                       name=_PREFIX + "cast_out", to=TensorProto.UINT8))

    new_in = helper.make_tensor_value_info(_PREFIX + "in", TensorProto.UINT8, ["batch", "h", "w", 3])
    new_out = helper.make_tensor_value_info(_PREFIX + "out", TensorProto.UINT8, ["batch", "h2", "w2", 3])

    nodes = list(pre_nodes) + list(g.node) + list(post_nodes)
    del g.node[:]
    g.node.extend(nodes)
    del g.input[:]
    g.input.extend([new_in])
    del g.output[:]
    g.output.extend([new_out])
    g.initializer.extend(inits)

    # Round needs opset >= 11; bump the default domain if the model is older.
    for imp in model.opset_import:
        if imp.domain == "" and imp.version < 11:
            imp.version = 11
    onnx.checker.check_model(model)
    # TensorRT requires value_info on the tensors feeding its subgraphs.
    try:
        model = onnx.shape_inference.infer_shapes(model)
    except Exception:
        pass  # other EPs don't need it
    return model
