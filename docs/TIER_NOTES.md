# Quality tier notes

**Hardware decode status:** the client uses `hwdec=auto-safe` by default
(NVDEC/D3D11 on Windows, VAAPI on Linux) — benefits the HEVC tiers; FFV1 has
no hardware decoder anywhere (codec-inherent). Server-side NVDEC source
decode is opt-in (`RELAY_NVDEC=1`): running it concurrently with NVENC encode
in one process crashed natively on the dev machine; software source decode
(~6 ms/frame) was never the pipeline bottleneck.

The client selector uses coarse P95 bandwidth classes rather than exposing
encoder QP. These are ballpark values for 4K-ish animated output, rounded to
the nearest 50 Mbps; constant-QP bitrate is content-dependent and grain can
move a stream well outside its label.

| UI label | protocol ID | encoder | approximate P95 |
|---|---|---|---:|
| True Lossless HEVC | `lossless-hevc` | NVENC P4 low-delay, lossless | content-dependent |
| HEVC ~350 Mbps | `hevc-qp2` | NVENC P7 const-QP 2 | 350 Mbps |
| HEVC ~250 Mbps | `hevc-qp4` | NVENC P7 const-QP 4 | 250 Mbps |
| HEVC ~200 Mbps | `hevc-qp6` | NVENC P7 const-QP 6 | 200 Mbps |
| HEVC ~100 Mbps | `hevc-qp10` | NVENC P7 const-QP 10 | 100 Mbps |
| HEVC ~50 Mbps (higher quality) | `hevc-qp14` | NVENC P7 const-QP 14 | 50 Mbps |
| HEVC ~50 Mbps (lower bandwidth) | `hevc-qp18` | NVENC P7 const-QP 18 | 50 Mbps |
| Lossless FFV1 | `lossless-ffv1` | FFV1 level 3 | content-dependent |

QP 2 was the first tested lossy setting to produce zero decoder drops on the
Galaxy Tab S9 Ultra problem content. QP 0 reduced drops relative to transform-
bypass lossless but is not a public choice. Android advertises every HEVC row
and deliberately omits FFV1.

## Lossless HEVC server profiles

True Lossless HEVC defaults to `nvenc-p4-low-delay`.
`relay-server --lossless-hevc-profile <name>` can override it for a server-wide experiment.
It is intentionally not client-negotiated yet. Restart the server between
profiles and use the same file/timestamps; `/status` reports both the selected
profile and the actual encoder used by each session.

| profile | encoder structure | expected tradeoff |
|---|---|---|
| `auto` | P7 NVENC, 48-frame GOP; x265-medium fallback | Legacy control/baseline |
| `nvenc-p7` | Explicit form of the current NVENC control | Same as `auto` when NVENC is available |
| `nvenc-p7-slices4` | Current P7 control with four slices per frame | Strict-lossless decoder-parallelism experiment |
| `nvenc-p7-slices8` | Current P7 control with eight slices per frame | More slice parallelism and overhead |
| `nvenc-p7-qp0` | P7 NVENC constant QP 0, 240-frame GOP | Near-lossless control without transform-bypass tuning |
| `nvenc-p7-qp2` | P7 NVENC constant QP 2, 240-frame GOP | Near-lossless; first reduced-bitrate experiment |
| `nvenc-p7-qp4` | P7 NVENC constant QP 4, 240-frame GOP | Near-lossless with a larger peak reduction |
| `nvenc-p7-qp6` | P7 NVENC constant QP 6, 240-frame GOP | Moderate reduction, still substantially above QP 16 |
| `nvenc-p7-qp8` | P7 NVENC constant QP 8, 240-frame GOP | Strong reduction while remaining conservative visually |
| `nvenc-p7-long-gop` | P7 NVENC, 240-frame GOP | Fewer large periodic IDR frames; usually slightly less bandwidth |
| `nvenc-p4-low-delay` | P4, no B-frames, one reference, no lookahead/weighted prediction, 240-frame GOP | Simpler decoder dependency structure, usually more bandwidth |
| `nvenc-p1-low-delay` | P1 with the same low-delay structure | Simplest/faster NVENC search and commonly the heaviest NVENC stream |
| `x265-ultrafast` | CPU, no B-frames, one reference, zero latency | Software real-time experiment; bandwidth-heavy |
| `x265-medium` | CPU lossless defaults, 240-frame GOP | Better compression but unlikely to be real-time at tablet resolution |

Lossless bitrate ordering is not guaranteed: scene entropy and encoder
decisions dominate. In particular, removing B-frames can reduce MediaCodec
reorder/reference work while increasing the number of bits it must ingest.
The slice variants preserve the P7 control's 48-frame GOP and other settings,
isolating whether Qualcomm's decoder can parallelize independent HEVC slices.
The older QP variants under the experimental `--lossless-hevc-profile` flag
remain as lab controls. Normal clients use the dedicated `hevc-qp*` quality
IDs instead, so a lossy stream is never presented as true lossless. All retain
NVENC, 8-bit 4:2:0 HEVC, P7, and a long GOP.
The long-GOP profiles do not compromise relay seeking: every seek epoch creates
a fresh encoder/container and therefore begins with a new keyframe.

Initial encoder-only measurements on the 9800X3D/RTX 5090 used the first 24
real frames of the identified 1080p anime episode, center-cropped and scaled to
2960×1848. They exclude ONNX and most relay overhead:

| profile | encoder fps | sample bitrate |
|---|---:|---:|
| `nvenc-p1-low-delay` | 884 | 363 Mbps |
| `nvenc-p4-low-delay` | 934 | 360 Mbps |
| `nvenc-p7-long-gop` | 488 | 346 Mbps |
| `nvenc-p7` | 517 | 346 Mbps |
| `x265-ultrafast` | 31.0 | 391 Mbps |
| `x265-medium` | 6.5 | 332 Mbps |

On a separate 120-frame P7 comparison, the 48-frame control GOP generated
three roughly 2.2–2.3 MiB keyframes; the 240-frame profile generated only the
initial keyframe. Average bitrate changed only from 301.4 to 300.4 Mbps, so the
long GOP is primarily a peak-smoothing experiment. x265-ultrafast is only
marginally plausible for 23.976 fps once decode, fit, and mux compete for CPU;
x265-medium is not a real-time candidate on this CPU at the tablet resolution.

Operational findings:

- The desktop downlink is read on a dedicated blocking socket thread and
  handed to qasync in bounded batches. Raising `StreamReader.limit` alone was
  insufficient: the Linux selector transport still requests only 256 KiB per
  socket callback, which tied lossless throughput to the Qt loop cadence and
  left the server's downlink queue pinned at 256 packets.
- The final client-to-mpv handoff uses a per-load localhost `tcp://` stream.
  python-mpv's custom-stream callback copies data into libmpv byte by byte in
  Python; it saturated near 200 Mbps while ten seconds remained queued before
  mpv. Native TCP removes that callback ceiling while retaining bounded relay
  backpressure and clean per-seek stream replacement.
- All advertised choices play in the desktop client through the container downlink
  (mpv demuxes MKV; FFV1 needed this — it has no raw elementary-stream form).
- A/V drift stayed +0.000 s across tiers in the headless smoke test.
- FFV1 decode is CPU-side on the client. Measured on the dev machine at
  3840x2160: **FFV1 ~61 fps, lossless-HEVC ~116 fps software decode** — both
  comfortable for 24/30 fps content on desktop. Android intentionally does not
  support FFV1 because no hardware decoder exists and sustained software
  decode would increase battery drain and thermals. Android supports the two
  HEVC choices instead (see [https://github.com/brpjerry/upscale-relay-android/blob/main/docs/ANDROID_CLIENT.md](https://github.com/brpjerry/upscale-relay-android/blob/main/docs/ANDROID_CLIENT.md)).
- mpv uses `hwdec=auto-safe` by default. `relay-desktop --no-hwdec` forces
  software decode for comparison; the client telemetry reports the decoder
  mpv actually selected.
- Real lossless bitrate scales with content complexity; anime tends lower,
  film grain much higher. The 100–400 Mbps planning range in
  [PLAN.md](PLAN.md) still stands.
- Galaxy Tab S9 Ultra validation sustained a real 2960×1664 lossless-HEVC
  stream over Wi-Fi at roughly 200–270 Mbps through mpv `mediacodec`, with no
  decoder drops or cache pause. See
  [https://github.com/brpjerry/upscale-relay-android/blob/main/docs/ANDROID_DEVICE_NOTES.md](https://github.com/brpjerry/upscale-relay-android/blob/main/docs/ANDROID_DEVICE_NOTES.md). Broader devices, access
  points, and grain-heavy sources remain release-matrix work.

## Downscale and deband controls

The server's cached PyAV/libswscale stage exposes fast bilinear, bilinear,
bicubic, area, Bicublin (bicubic luma/bilinear chroma), Gaussian, Sinc,
Lanczos, and natural spline. Lanczos remains the default. Catmull–Rom is a
useful sharp cubic filter in mpv, but this PyAV reformatter API cannot pass its
required B=0, C=0.5 parameters; ordinary bicubic is therefore not mislabeled
as Catmull–Rom. Adding it later requires a measured zscale/libplacebo path.

GPU debanding runs in each client's mpv output pipeline after hardware decode.
That placement preserves the original input to the ONNX model and can treat
banding introduced by the source, upscale, final resize, or lossy encode. It is
off by default and persisted independently on desktop and Android.
