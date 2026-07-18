# upscale-cli benchmark

> Historical snapshot: these tables predate the bandwidth-labeled HEVC ladder.
> The former `visually-lossless` column used NVENC QP 16; current benchmark
> runs enumerate the dedicated `hevc-qp*` choices instead.

- frames per measurement: 12
- execution provider request: auto

## Decode (NVDEC/software auto)

| input | fps |
|---|---|
| 720p | 191.5 |
| 1080p | 133.9 |
| 1440p | 93.3 |

## Model: 2x_AnimeJaNai_fp16.onnx

| input | untiled fps | tiled-512 fps | lossless-ffv1 enc fps | lossless-hevc enc fps | visually-lossless enc fps | e2e fps (vl) | GPU util peak | VRAM peak MB |
|---|---|---|---|---|---|---|---|---|
| 720p | 18.5 | 7.8 | 54.2 | 45.0 | 42.4 | 13.2 | 28% | 8433 |
| 1080p | 8.1 | 4.6 | 29.5 | 27.1 | 24.6 | 6.2 | 43% | 10028 |
| 1440p | 4.6 | 3.2 | 17.3 | 20.9 | 16.9 | 3.9 | 57% | 11825 |

## Model: 2x_AnimeJaNai_HD_V3Sharp1_Compact.onnx

| input | untiled fps | tiled-512 fps | lossless-ffv1 enc fps | lossless-hevc enc fps | visually-lossless enc fps | e2e fps (vl) | GPU util peak | VRAM peak MB |
|---|---|---|---|---|---|---|---|---|
| 720p | 37.6 | 19.7 | 54.2 | 49.3 | 44.1 | 20.8 | 72% | 8847 |
| 1080p | 19.0 | 9.8 | 30.4 | 33.5 | 27.0 | 12.3 | 83% | 10093 |
| 1440p | 11.1 | 6.5 | 17.8 | 21.4 | 16.9 | 7.8 | 88% | 11674 |

## Model: bilinear2x.onnx

| input | untiled fps | tiled-512 fps | lossless-ffv1 enc fps | lossless-hevc enc fps | visually-lossless enc fps | e2e fps (vl) | GPU util peak | VRAM peak MB |
|---|---|---|---|---|---|---|---|---|
| 720p | 406.5 | 135.1 | 52.5 | 49.7 | 44.9 | 42.5 | 79% | 8392 |
| 1080p | 212.6 | 65.6 | 30.5 | 33.2 | 26.1 | 31.3 | 16% | 8791 |
| 1440p | 129.8 | 39.8 | 18.0 | 22.9 | 17.0 | 22.2 | 25% | 9349 |

## Model: realesrgan-x2-ts.onnx

| input | untiled fps | tiled-512 fps | lossless-ffv1 enc fps | lossless-hevc enc fps | visually-lossless enc fps | e2e fps (vl) | GPU util peak | VRAM peak MB |
|---|---|---|---|---|---|---|---|---|
| 720p | 6.3 | 1.1 | 55.2 | 48.3 | 43.2 | 5.5 | 100% | 11107 |
| 1080p | 3.2 | 0.6 | 30.4 | 33.4 | 26.4 | 2.9 | 100% | 14330 |
| 1440p | 2.0 | 0.4 | 17.3 | 21.2 | 16.9 | 1.8 | 100% | 20522 |

Real-time viability: e2e fps must sustain the content frame rate (24/30/60). Prefer the fastest model that clears it with ~30% headroom.

## TensorRT EP (server run from `.venv-cuda` with `--ep tensorrt`)

2x_AnimeJaNai_HD_V3Sharp1_Compact, uint8-wrapped graph, fp16 engines:

| measurement | 720p | 1080p |
|---|---|---|
| inference only | 119.7 fps (8.4 ms) | 53.9 fps (18.5 ms) |
| full server pipeline (1080p -> 3840x2160, visually-lossless) | — | **34.0 fps** |

First engine build per input shape ~20 s (cached on disk in `models/.trt_cache`;
subsequent sessions load in seconds). The table above (DirectML `auto` EP) is
what the default `.venv` server achieves; TensorRT roughly triples inference
throughput on the same model.
