"""upscale-cli entry point.

Subcommands:
  run     process a video through the pipeline (default if omitted)
  info    print stream metadata
  sample  generate a synthetic test clip

`upscale-cli in.mkv out.mkv` is shorthand for `upscale-cli run in.mkv out.mkv`.
"""

from __future__ import annotations

import argparse
import sys

from .info import print_info, verify_passthrough, verify_pts
from .sample import make_sample
from .stages import FrameSink, FrameSource, run_pipeline

_SUBCOMMANDS = {"run", "info", "sample", "bench"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="upscale-cli")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="process a video through the pipeline")
    run.add_argument("input")
    run.add_argument("output")
    run.add_argument("--hwaccel", default="auto", choices=["auto", "cuda", "d3d11va", "none"])
    run.add_argument("--model", help="path to .onnx super-resolution model")
    run.add_argument("--ep", default="auto", choices=["auto", "tensorrt", "cuda", "dml", "cpu"],
                     help="onnxruntime execution provider")
    run.add_argument("--tile-size", default="auto",
                     help="inference tile size: 'auto', 'none' (whole frame), or pixels (e.g. 512)")
    run.add_argument("--tile-overlap", type=int, default=16, help="tile overlap in pixels (even)")
    run.add_argument("--fit", help="fit output inside WxH (e.g. 3840x2160), aspect preserved")
    run.add_argument("--fit-align", type=int, default=2, help="round fitted dims to multiple (2 or 16)")
    run.add_argument("--quality", choices=[
        "lossless-ffv1", "lossless-hevc", "hevc-qp2", "hevc-qp4",
        "hevc-qp6", "hevc-qp10", "hevc-qp14", "hevc-qp18",
    ],
                     help="encoder tier; omit for default x264 (--crf/--preset)")
    run.add_argument("--crf", default="12", help="x264 CRF (default 12, near-transparent)")
    run.add_argument("--preset", default="medium", help="x264 preset")
    run.add_argument("--no-verify", action="store_true", help="skip output verification")

    info = sub.add_parser("info", help="print stream metadata")
    info.add_argument("input")

    sample = sub.add_parser("sample", help="generate a synthetic test clip")
    sample.add_argument("output")
    sample.add_argument("--frames", type=int, default=240)
    sample.add_argument("--size", default="640x360", help="WxH")
    sample.add_argument("--fps", type=int, default=30)

    bench = sub.add_parser("bench", help="benchmark models in a directory")
    bench.add_argument("--models-dir", default="models")
    bench.add_argument("--out", default="docs/BENCH.md")
    bench.add_argument("--frames", type=int, default=48)
    bench.add_argument("--ep", default="auto", choices=["auto", "tensorrt", "cuda", "dml", "cpu"])
    bench.add_argument("--fit", help="apply fit stage in e2e/encode measurements (WxH)")

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default subcommand: `upscale-cli in out` / `upscale-cli --info file`.
    if argv and argv[0] == "--info":
        argv = ["info"] + argv[1:]
    elif argv and argv[0] not in _SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["run"] + argv

    args = _build_parser().parse_args(argv)

    if args.command == "info":
        print_info(args.input)
        return 0

    if args.command == "sample":
        width, _, height = args.size.partition("x")
        make_sample(args.output, frames=args.frames, width=int(width), height=int(height), fps=args.fps)
        return 0

    if args.command == "bench":
        from .bench import run_bench

        fit = None
        if args.fit:
            fw, _, fh = args.fit.partition("x")
            fit = (int(fw), int(fh))
        run_bench(args.models_dir, args.out, frames=args.frames, ep=args.ep, fit=fit)
        return 0

    # run
    stages = []
    if args.model:
        from .infer import OnnxUpscaler

        tile: int | str | None
        if args.tile_size == "auto":
            tile = "auto"
        elif args.tile_size in ("none", "0"):
            tile = None
        else:
            tile = int(args.tile_size)
        stages.append(OnnxUpscaler(args.model, ep=args.ep, tile_size=tile, overlap=args.tile_overlap))
    if args.fit:
        from .fit import FitStage

        fit_w, _, fit_h = args.fit.partition("x")
        stages.append(FitStage(int(fit_w), int(fit_h), align=args.fit_align))

    if args.quality:
        from .encode import select_encoder

        codec, pix_fmt, enc_options = select_encoder(args.quality)
    else:
        codec, pix_fmt, enc_options = "libx264", "yuv420p", {"crf": args.crf, "preset": args.preset}

    with FrameSource(args.input, hwaccel=args.hwaccel) as source:
        if source.hwaccel_active:
            print(f"decode: {source.hwaccel_active} (hardware)", file=sys.stderr)
        else:
            print("decode: software", file=sys.stderr)
        with FrameSink(
            args.output,
            time_base=source.time_base,
            rate=source.average_rate,
            codec=codec,
            pix_fmt=pix_fmt,
            options=enc_options,
        ) as sink:
            decoded = run_pipeline(source, sink, stages=stages)
            pts_written = sink.pts_written

    if not args.no_verify:
        ok = verify_passthrough(args.input, args.output, decoded)
        ok = verify_pts(args.output, pts_written) and ok
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
