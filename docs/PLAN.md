# Video Upscale Relay — Architecture and Roadmap

Video Upscale Relay plays local, mounted-share, or server-hosted media while a
GPU server upscales the video through ONNX models. The returned video keeps the
source timestamps, and the desktop client uses the original file for audio and
subtitles.

This document describes the current implementation first and keeps only
unfinished work in the roadmap. See [PROTOCOL.md](PROTOCOL.md) for the wire
contract and [SERVER_LIBRARY.md](SERVER_LIBRARY.md) for server-hosted media.

## Current status

| Area | Status | Current implementation |
|---|---|---|
| Offline upscale pipeline | **Implemented** | `upscale-cli` decode, ONNX inference, tiling, fit/cover sizing, tiered encoding, verification, sample generation, and benchmarks |
| Streaming server and protocol | **Implemented** | aiohttp WebSocket control, framed TCP media, sessions, seeks/epochs, Matroska downlink, pacing, and `/status` |
| Desktop client | **Implemented** | PySide6/qasync UI, local browser, embedded libmpv, uplink, playback, seek, subtitles, quality/model controls, and local fallback |
| Lossless playback path | **Implemented** | blocking downlink receiver plus native localhost `tcp://` handoff to mpv; avoids qasync and python-mpv callback throughput ceilings |
| Server-side media library | **Implemented** | `--library`, sandboxed listing and Range HTTP delivery, server demux/seek, capability-driven Server tab |
| Server-side framing and resize filters | **Implemented** | Fit preserves the full frame; Cover center-crops before encode; the final post-ONNX downscale is selectable per server or session |
| Shared-mount path mapping | **Planned** | server library currently delivers original audio/subtitles over HTTP; mapping one relative path to different client/server mount roots is not implemented |
| Polish phase | **Partial** | model discovery/picker, metrics, manual host configuration, mounted shares, and fallback exist; discovery, pairing, hot model reload, and reconnect/resume remain |
| Android client | **Phase 4 host-verified; device gate pending** | The tablet client now adds SAF local files, encoded MediaExtractor uplink, persistent local recents, and direct fallback to the Phase 3 shell and Phase 2 A/V/seek core; PGS/VobSub remains unrun because the configured library has no bitmap-subtitle sample |

## Architecture

There are two supported source paths:

```text
Client-local source
  client VideoTrack -> framed uplink -> server decode

Server-library source
  server VideoTrack -----------------> server decode

Server decode -> ONNX inference -> fit/cover -> encode/mux
              -> framed Matroska downlink -> client loopback TCP -> libmpv

Original audio/subtitles
  local source: client path -> libmpv external tracks
  server source: Range HTTP /media URL -> libmpv external tracks
```

The control channel is WebSocket on port 8590. Media uses framed TCP on port
8591. A session has one control connection, an optional uplink attachment, and
one downlink attachment. Server-library sessions omit the uplink attachment.

### Core design decisions

1. **Send compressed video, not decoded frames.** For client-local media, the
   client stream-copies encoded video packets to the server. Server-library
   sessions skip that hop and demux the same packet stream on the server.
2. **PTS is the contract.** Source presentation timestamps are preserved
   through demux, decode, inference, encode, mux, and playback. Epochs isolate
   seeks. These rules are specified in [PROTOCOL.md](PROTOCOL.md).
3. **Buffer instead of forcing minimum latency.** The server runs ahead until
   the client reports roughly ten seconds buffered, then pauses and resumes
   around a narrow watermark.
4. **Fit on the server.** Model scale and display size are independent. The
   server applies `fit` or a centered `cover` crop after inference and reports
   the encoded dimensions to the client. Cover no longer sends off-screen
   overflow for client-side panscan.
5. **Quality is negotiated.** The implemented choices are true-lossless HEVC,
   six NVENC HEVC bandwidth classes (internally QP 2/4/6/10/14/18), and
   lossless FFV1. Clients present approximate P95 bandwidth labels supplied by
   the server instead of encoder QP numbers. HEVC is the Android-compatible
   path; FFV1 remains desktop-only.
6. **Audio and subtitles stay with the original.** libmpv attaches them as
   external tracks and aligns them to returned video using the original PTS.
7. **The client remains protocol-thin.** The server and protocol do not depend
   on PySide6 or other desktop-specific behavior, leaving room for Android.

## Implemented components

### Offline pipeline (`upscale_cli/`)

- PyAV source decode with preserved PTS and optional hardware acceleration.
- ONNX Runtime providers for TensorRT, CUDA, DirectML, and CPU.
- Model manifests, uint8 graph wrapping, tiled inference with overlap, and
  automatic fallback when a full-frame allocation does not fit.
- Aspect-preserving `fit` sizing and centered server-side `cover` cropping with
  encoder alignment.
- FFV1 lossless, P4 low-delay NVENC HEVC lossless, and six constant-QP NVENC
  HEVC encoders exposed as approximate bandwidth classes.
- Output verification, synthetic sample generation, and a benchmark harness.
- TensorRT inference isolation in `upscale_cli/infer_worker.py`; the worker
  process is required because in-process streaming corrupted the native heap.

### Server (`relay_server/`)

- aiohttp WebSocket control and HTTP status/library endpoints on the control
  port, with a separate TCP media listener.
- One `Session` and three-stage threaded pipeline per client: decode, infer,
  then fit/encode/mux.
- Streaming Matroska downlink with the selected HEVC or FFV1 codec.
- Epoch-safe seek/flush, decode-and-discard to the target, stale packet
  rejection, and seek-storm coalescing.
- Live buffer-report pacing with stale-report decay and bounded queues.
- Model discovery from `--models-dir` and execution-provider selection.
- Configurable post-ONNX resize filters (`fast-bilinear`, `bilinear`, `bicubic`,
  `area`, `bicublin`, `gaussian`, `sinc`, `lanczos`, and `spline`), advertised
  as protocol capabilities with a server default and per-session override.
- Optional client-side mpv GPU debanding after decode. Keeping it client-side
  avoids modifying the model input and also treats banding introduced by
  upscale, scaling, or encoding.
- Server-wide lossless-HEVC experiment profiles spanning low-delay NVENC,
  long-GOP NVENC, and x265 software encoding. These remain server flags until
  device testing identifies profiles worth exposing through the protocol.
- Optional sandboxed server library from local, UNC, or mounted paths. See
  [SERVER_LIBRARY.md](SERVER_LIBRARY.md).

Server-side NVDEC remains opt-in through `RELAY_NVDEC=1`: concurrent NVDEC and
NVENC caused native crashes on the development machine, while software source
decode was fast enough for the measured pipeline.

### Shared protocol and media (`relay_protocol/`, `relay_media/`)

- Versioned handshake and packet framing shared by client and server.
- PTS/DTS, keyframe, discontinuity, EOS, token, and epoch handling.
- A shared, lock-serialized `VideoTrack` used for client uplink sources and
  server-library sources.

### Client core (`relay_client_core/`)

- Control lifecycle, session negotiation, uplink batching, seek handling, and
  timed live buffer reports.
- Dedicated blocking downlink socket receiver with a bounded bridge into the
  qasync loop. This replaced per-callback asyncio reads that could not sustain
  lossless traffic under Qt.
- Headless `relay-client` CLI used by integration tests and diagnostics.

### Desktop client (`desktop_client/`)

- PySide6 UI with native libmpv render API support on Wayland, X11, and
  Windows.
- Local filesystem browser plus a capability-driven Server library tab.
- Model, quality, fit/cover, resize filter, play/pause, seek, subtitle track, subtitle delay,
  fullscreen, telemetry, and local fallback controls.
- External audio/subtitle attachment from a local file or server `/media`
  URL.
- Per-load localhost `tcp://` stream between the Python buffer and mpv. The
  previous python-mpv custom callback copied each byte in Python and saturated
  near 200 Mbps; the native socket removes that ceiling.
- Command-line options for debugging, tracing, mpv OSC/scripts, hardware
  decode, headless output, and isolated test settings.

## Implementation milestones

The original phase prompts have been completed or superseded as follows:

### Phase 1 — Offline proof of pipeline: implemented

Prompts 1.1–1.6 are represented by `upscale_cli/` and the fit/tiling tests.
The implementation uses Matroska and the current tier encoders; automatic
tiling is allocation/fallback driven rather than based on a portable free-VRAM
query.

### Phase 2 — Streaming server: implemented

Prompts 2.1–2.6 are represented by `relay_protocol/`, `relay_server/`,
`relay_client_core/`, and `tests/test_streaming.py`. The downlink format chosen
after prototyping is a complete streaming Matroska byte stream per epoch,
carried inside the protocol packets.

### Phase 3 — Desktop MVP: implemented

Prompts 3.1–3.5 are represented by `desktop_client/`. libmpv uses its render
API rather than window-ID embedding, and downlink bytes reach mpv through a
native localhost TCP stream rather than `stream_cb`.

### Phase 4 — Full desktop playback: implemented, validation ongoing

Seek/epoch reloads, buffering telemetry, local fallback, subtitles, and all
advertised quality choices are implemented. Wired and synthetic measurements are
recorded in [TIER_NOTES.md](TIER_NOTES.md). Broader Wi-Fi, long-duration, and
content-diversity measurements remain operational validation, not missing
features.

### Server-side library extension: implemented

This post-MVP extension supports server-hosted media through `--library`,
including Range delivery of original tracks and the desktop Server tab. Only
shared-mount path mapping remains planned; see
[SERVER_LIBRARY.md](SERVER_LIBRARY.md).

## Remaining roadmap

### Phase 5 — Desktop/server polish: partial

- **SMB:** OS-mounted shares and Windows UNC library roots work now. A direct
  `smb://` browser with credential storage is not implemented.
- **Discovery and pairing:** add mDNS advertisement/browsing, a first-connect
  code, persistent client credentials, and optional TLS.
- **Model management:** discovery and selection work now. Add directory
  watching, manifest validation UX, benchmark metadata in capabilities, and
  mid-play model switching.
- **Recovery:** cleanup and manual local fallback work now. Add automatic
  reconnect with session resume, keepalives/timeouts, and chaos coverage for
  network outages.
- **Performance/endurance:** continue per-stage profiling, two-hour drift and
  memory tests, quality-tier network measurements, and native crash diagnosis.
- **Shared mounts:** map a library-relative identity to distinct server and
  client roots so audio/subtitles can bypass HTTP when both machines mount the
  same share.

### Phase 6 — Android client: Phase 5 device-verified

A separate Kotlin/Jetpack Compose project now lives in [upscale-relay-android](https://github.com/brpjerry/upscale-relay-android), with
an internal libmpv engine adapted from mpv-android rather than a fork of its
application. Phase 1 established protocol v1 control, bounded blocking media
transport, localhost libmpv handoff, Surface lifecycle ownership, and hardware
HEVC decode. Initial playback on the target Galaxy Tab S9 Ultra runs through
Qualcomm hardware HEVC decode. The Phase 1 target-device gate
also covers natural EOS/endurance, 20 session cycles, 10 live Surface
replacements, background/foreground recovery, bounded server and Wi-Fi loss,
and device-side truncated framing. Phase 2 now implements server-library
audio/subtitle attachment, audio-clock/absolute-PTS synchronization, pause and
seek controls, persistent-downlink epoch changes with fresh localhost streams,
track/delay controls, the HEVC quality choices, fit/cover, and expanded
telemetry. The
physical-device A/V and seek-storm gates passed, including SSA rendering and
live delay changes. Phase 3 adds the Material 3 tablet product shell,
Server/Local/Recent/Settings navigation, a two-pane server browser, DataStore
preferences and recents, system light/dark behavior, immersive auto-hiding
player controls, track/settings sheets, and seek/brightness/volume gestures.
Those paths are host-tested and exercised on the target tablet. PGS/VobSub
remains unrun because the test library has no bitmap-subtitle sample. Local
SAF demux/uplink and direct fallback are implemented in Phase 4 and passed the
physical-device gate (exact PTS equivalence, seek-storm alignment, natural
EOS, and picker-free fallback at the current position). Android Phase 5 is
also implemented and device-verified: the server now advertises
`_upscalerelay._tcp` over mDNS (zeroconf, `--no-mdns` to disable), Android
discovers it with NSD next to manual entry, recoverable failures reconnect
and resume automatically at the captured position (the ten-second
interruption gate passed hands-free), mid-play model/quality/framing changes
restart the session in place, and sustain warnings flag a server or device
that cannot keep up. Pairing/authorization remains deferred until the server
implements it; direct-SMB and shared-mount work is deferred to the Android
Phase 6 alongside release hardening. Android supports the
hardware-decoded HEVC bandwidth classes and true-lossless HEVC; FFV1 remains
available
on desktop but is intentionally excluded from Android because it requires
continuous software decode and the associated battery/thermal cost.
See [https://github.com/brpjerry/upscale-relay-android/blob/main/docs/ANDROID_CLIENT.md](https://github.com/brpjerry/upscale-relay-android/blob/main/docs/ANDROID_CLIENT.md) for the phases and
[https://github.com/brpjerry/upscale-relay-android/blob/main/docs/ANDROID_DEVICE_NOTES.md](https://github.com/brpjerry/upscale-relay-android/blob/main/docs/ANDROID_DEVICE_NOTES.md) for the validation record.

## Future/stretch work

- A server GUI or web dashboard over the existing status/control surfaces.
- User-defined VapourSynth pre/post stages, with live mode restricted to
  frame-count- and timestamp-preserving filters.
- Persistent offline render queue with resumable jobs, output retention, and
  client-side queue/library views.
- QUIC transport if measurements show a material benefit over the current
## Current risks and open questions

- An intermittent native server crash has occurred during full 4K
  lossless-HEVC runs. If evidence places it in NVENC, encoding should move to
  a worker process as TensorRT inference already has.
- Lossless bitrate depends heavily on grain and motion. Desktop Ethernet and
  the target Tab S9 Ultra Wi-Fi topology are proven; broader Android hardware
  and RF conditions remain release-matrix work.
- Heavy models may not sustain the source frame rate even on strong GPUs;
  benchmarked model selection and buffered operation mitigate but do not
  remove this limit.
- Shared-mount identity and unattended SMB credentials need explicit designs
  before the server is run as a service.
