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

Deep-colour (HDR, 10-bit) sessions use ``wrap_rgb48_io`` instead: the same
contract for rgb48le samples. TensorRT has no 16-bit integer tensor type and
allows 8-bit ones only at network boundaries, so each 16-bit sample crosses
as its two little-endian bytes ([N,H,W,3,2] uint8) and the CPU side merely
reinterprets the buffer -- no conversion pass over a 4K frame.
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
    return _finish(model, pre_nodes, post_nodes, inits, new_in, new_out)


_P16 = "u16wrap_"
# 10-bit output written as rgb48le by bit replication: v * 64 + (v >> 4),
# so 1023 becomes 65535 exactly. 1023 is the deepest any encoder here keeps.
_OUT_MAX = 1023.0


def wrap_rgb48_io(model: onnx.ModelProto, value_range: tuple[float, float],
                  channel_order: str = "rgb") -> onnx.ModelProto:
    """Returns a new ModelProto with input 'u16wrap_in' and output
    'u16wrap_out', both uint8 [N,H,W,3,2]: rgb48le samples as their low and
    high bytes. The output carries 10 significant bits, enough for every
    deep-colour encoder and exact in fp16 engines (integers up to 2048 are
    representable)."""
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
    f32 = TensorProto.FLOAT

    def const(name: str, value: float):
        return _scalar(_P16 + name, value, f32)

    inits = [
        const("w_lo", 1.0 / 65535.0), const("w_hi", 256.0 / 65535.0),
        const("span", hi - lo), const("lo", lo), const("inv_span", 1.0 / (hi - lo)),
        const("out_max", _OUT_MAX), const("zero", 0.0),
        const("quarter", 0.25), const("four", 4.0), const("sixty_four", 64.0),
        const("sixteenth", 1.0 / 16.0),
        helper.make_tensor(_P16 + "idx0", TensorProto.INT64, [], [0]),
        helper.make_tensor(_P16 + "idx1", TensorProto.INT64, [], [1]),
        helper.make_tensor(_P16 + "plane_shape", TensorProto.INT64, [5], [0, 0, 0, 0, 1]),
    ]
    if channel_order == "bgr":
        inits.append(helper.make_tensor(_P16 + "chidx", TensorProto.INT64, [3], [2, 1, 0]))

    p = _P16
    pre = [
        # uint8 byte pairs -> float32 [0, 1] NHWC; Cast must lead (uint8 only
        # at network boundaries) and Gather with a scalar index drops the axis.
        helper.make_node("Cast", [p + "in"], [p + "bytes"], name=p + "cast_in", to=f32),
        helper.make_node("Gather", [p + "bytes", p + "idx0"], [p + "lo_b"], name=p + "lo_b", axis=4),
        helper.make_node("Gather", [p + "bytes", p + "idx1"], [p + "hi_b"], name=p + "hi_b", axis=4),
        helper.make_node("Mul", [p + "lo_b", p + "w_lo"], [p + "lo_v"], name=p + "lo_v"),
        helper.make_node("Mul", [p + "hi_b", p + "w_hi"], [p + "hi_v"], name=p + "hi_v"),
        helper.make_node("Add", [p + "lo_v", p + "hi_v"], [p + "unit"], name=p + "unit"),
        helper.make_node("Transpose", [p + "unit"], [p + "nchw"], name=p + "nhwc2nchw",
                         perm=[0, 3, 1, 2]),
    ]
    cur = p + "nchw"
    if channel_order == "bgr":
        pre.append(helper.make_node("Gather", [cur, p + "chidx"], [p + "swapped"],
                                    name=p + "swap_in", axis=1))
        cur = p + "swapped"
    pre += [
        helper.make_node("Mul", [cur, p + "span"], [p + "spanned"], name=p + "spanned"),
        helper.make_node("Add", [p + "spanned", p + "lo"], [p + "ranged"], name=p + "ranged"),
        helper.make_node("Cast", [p + "ranged"], [orig_in.name], name=p + "cast_model_in", to=elem),
    ]

    post = [
        helper.make_node("Cast", [orig_out.name], [p + "outf"], name=p + "cast_model_out", to=f32),
        helper.make_node("Sub", [p + "outf", p + "lo"], [p + "shifted"], name=p + "shifted"),
        helper.make_node("Mul", [p + "shifted", p + "inv_span"], [p + "unit_out"], name=p + "unit_out"),
        helper.make_node("Mul", [p + "unit_out", p + "out_max"], [p + "scaled"], name=p + "scaled"),
        helper.make_node("Round", [p + "scaled"], [p + "rounded"], name=p + "rounded"),
        helper.make_node("Clip", [p + "rounded", p + "zero", p + "out_max"], [p + "v"],
                         name=p + "clip"),
    ]
    cur = p + "v"
    if channel_order == "bgr":
        post.append(helper.make_node("Gather", [cur, p + "chidx"], [p + "swapback"],
                                     name=p + "swap_out", axis=1))
        cur = p + "swapback"
    post += [
        # rgb48le bytes of v * 64 + (v >> 4): high v >> 2, low
        # (v & 3) << 6 | v >> 4. Every intermediate is a small integer.
        helper.make_node("Mul", [cur, p + "quarter"], [p + "v_q"], name=p + "v_q"),
        helper.make_node("Floor", [p + "v_q"], [p + "high"], name=p + "high"),
        helper.make_node("Mul", [p + "high", p + "four"], [p + "high4"], name=p + "high4"),
        helper.make_node("Sub", [cur, p + "high4"], [p + "low2"], name=p + "low2"),
        helper.make_node("Mul", [p + "low2", p + "sixty_four"], [p + "low_top"], name=p + "low_top"),
        helper.make_node("Mul", [cur, p + "sixteenth"], [p + "v_s"], name=p + "v_s"),
        helper.make_node("Floor", [p + "v_s"], [p + "low_bottom"], name=p + "low_bottom"),
        helper.make_node("Add", [p + "low_top", p + "low_bottom"], [p + "low"], name=p + "low"),
        helper.make_node("Reshape", [p + "low", p + "plane_shape"], [p + "low5"], name=p + "low5"),
        helper.make_node("Reshape", [p + "high", p + "plane_shape"], [p + "high5"], name=p + "high5"),
        helper.make_node("Concat", [p + "low5", p + "high5"], [p + "pairs"], name=p + "pairs", axis=4),
        # Same tail as the 8-bit wrapper: float Transpose, then the uint8 Cast.
        helper.make_node("Transpose", [p + "pairs"], [p + "nhwc"], name=p + "nchw2nhwc",
                         perm=[0, 2, 3, 1, 4]),
        helper.make_node("Cast", [p + "nhwc"], [p + "out"], name=p + "cast_out", to=TensorProto.UINT8),
    ]

    new_in = helper.make_tensor_value_info(p + "in", TensorProto.UINT8, ["batch", "h", "w", 3, 2])
    new_out = helper.make_tensor_value_info(p + "out", TensorProto.UINT8, ["batch", "h2", "w2", 3, 2])
    return _finish(model, pre, post, inits, new_in, new_out)


def _finish(model, pre_nodes, post_nodes, inits, new_in, new_out) -> onnx.ModelProto:
    g = model.graph
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
