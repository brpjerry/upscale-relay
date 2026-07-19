# AGENTS.md — video-upscale-relay

Client plays local/SMB video; a GPU server upscales each frame through .onnx
models; the upscaled stream comes back losslessly encoded and plays in sync
with the original file's audio/subs. Read `docs/PLAN.md` (architecture +
roadmap), `docs/PROTOCOL.md` (wire format — PTS and epoch semantics are
load-bearing), `docs/CLIENT_LINUX.md` (Linux setup), and
`docs/TIER_NOTES.md` / `docs/BENCH.md` (measurements).

## Layout

- `relay_protocol/` — shared framing/handshake. Client and server both import it.
- `relay_server/` — asyncio control WS (:8590) + TCP media (:8591) + a
  3-thread pipeline per session (decode → infer → fit/encode/mux) in
  `pipeline.py`. TensorRT inference runs in a **separate worker process**
  (`upscale_cli/infer_worker.py`) — do not move it back in-process (heap
  corruption, see Hard rules).
- `relay_client_core/` — demux/uplink/control/downlink library + `relay-client`
  mock CLI (used by the integration tests).
- `desktop_client/` — PySide6 + qasync + python-mpv player (`relay-desktop`).
- `upscale_cli/` — offline pipeline, ONNX/EP handling, uint8 graph wrapper,
  `upscale-cli` (run/info/sample/bench subcommands).

## Commands

```bash
pip install -e ".[gui]"                       # client machine: this is everything
python -m pytest tests -q                     # full suite, all green expected
relay-desktop                                 # the player (Wayland-native; mpv draws via render API)
relay-client FILE --model NAME --tier TIER --display WxH [--decode]   # headless client
upscale-cli sample out.mkv --frames 240 --size 1920x1080 --fps 24     # make test media
```

On Windows, a source-run desktop client or full GUI test environment also
needs `mpv-dev/libmpv-2.dll` at the repository root. Get it from the
`mpv-dev-x86_64-...7z` archive, not the similarly named player archive; the
client adds that directory to its DLL search path automatically. See
`docs/CLIENT_WINDOWS.md`. Server-only release binaries do not need libmpv.

Server (runs on the Windows box, not the laptop):
`relay-server --models-dir models --ep tensorrt` from `.venv-cuda`.
`http://<server>:8590/status` returns per-session pipeline fps + per-stage ms —
first stop for any performance question.
Release binaries stay small and use `relay_server/runtime_bootstrap.py` to
install the pinned NVIDIA wheels into a versioned `%LOCALAPPDATA%` runtime on
first launch. CI must not install or package the multi-GB `.[nvidia]` extra; it
smoke-installs one small wheel through each frozen executable to exercise pip
without downloading the NVIDIA stack, and separately asserts packaged ONNX can
import so the uint8 graph wrapper cannot silently disappear. Runtime setup
itself verifies TensorRT, CUDA, and CPU before publishing its ready marker.
The tray GUI's optional file log is `Documents/upscale-relay-server.log` and is
enabled by default through its persisted configuration checkbox. While enabled,
the GUI server logs 2-second `relay.stats` samples and a final session sample.

Client flags: `--debug` (faulthandler), `--trace` (consume-loop trace),
`--mpv-osc` (mpv OSC overlay — known to destabilize seeks), `--no-hwdec`
(force sw decode), `--mpv-scripts` (load user mpv scripts — off by default,
LuaJIT scripts destabilize stream reloads), `--headless` (null vo/ao), and
`--settings-scope <name>` (isolate QSettings — tests MUST set this option or
pass the equivalent `DesktopOptions`). Server env flag: `RELAY_NVDEC=1`
(server hw source decode — crashed with NVENC concurrently, off by default).
The desktop client loads the user's `mpv.conf`/`input.conf`
(prefs pass through; relay plumbing like `vo`/`rebase-start-time` is
re-asserted post-init in `mpv_view.py` because the config file overrides
constructor options).

## Hard rules (each one is a native crash or deadlock we actually hit)

**PyAV / libav**
- Never set `stream.time_base` on an output container; set `frame.time_base`
  and let the muxer rescale. `CodecContext` has no `.close()`.
- Never touch an `av` container/codec from two threads. A cancelled
  `asyncio.to_thread` **keeps running on its worker thread** — `VideoTrack`
  serializes all demux/seek under a lock for exactly this reason, and its
  iterator generation counter makes superseded iterators end instead of
  stealing post-seek packets (a headless batch that outlives its task shares
  the container's read position; stolen runs of packets garble B-frame
  reordering server-side → non-monotonic dts → mux EINVAL, seen as
  seek-storm flakiness).
- `frame.reformat()` / `to_ndarray()` rebuild a swscale context per call —
  ruinous at 4K (90 ms/frame). Use a cached `VideoReformatter`, one per thread.
- Report bitstream codec names to peers ("hevc"), not encoder names ("hevc_nvenc").

**asyncio / Qt (qasync)**
- **No modal dialogs / exec() / processEvents from coroutine context** — the
  nested loop re-enters asyncio tasks and ends in memory corruption.
  `MainWindow._error()` is non-modal on purpose.
- A garbage-collected `asyncio.StreamWriter` closes its socket — keep refs.
- Client must send `buffer_report` on a timer with *live* values; reporting
  only on packet arrival deadlocks the server's watermark pause/resume.
- Under qasync the loop turns ~once per rendered frame (~25/s) while mpv
  plays. Media pumps must move batches per loop turn: per-packet
  `to_thread`+`drain` capped the uplink at ~12 pkt/s (starved the server
  below realtime), and the 64 KiB StreamReader default capped the downlink
  at ~1.6 MB/s. See `_UPLINK_BATCH` / `_DOWNLINK_READ_LIMIT` in
  `relay_client_core/client.py`.

**mpv (python-mpv, embedded)**
- The downlink is a live Matroska stream via a per-load localhost `tcp://`
  socket. Keep the dedicated sender thread: python-mpv's custom-stream adapter
  copies bytes one at a time in Python and capped lossless HEVC near 200 Mbps.
- Post-seek: **never** pass `start=`; we run `rebase-start-time=no` so the new
  stream's absolute PTS place playback and external audio aligns itself.
  Reload = `stop`, close old buffer, `await asyncio.sleep(0.15)`, then
  `loadfile` — no synchronous mpv property reads during teardown.
- mpv OSC (LuaJIT) intermittently crashes mpv's event thread on stream
  reloads → OSC off by default. LuaJIT's caught SEH exception `0xe24c4a02`
  in faulthandler output is *benign noise*, not a crash.
- The audio/subs come from the original file via `audio-file`/`sub-files`
  loadfile options (plain `external-files` tracks are not auto-selected).

**GPU stacks (server box: RTX 5090 "Blackwell", Windows)**
- ORT TensorRT EP corrupts the process heap under streaming → it lives in a
  subprocess (`SubprocessUpscaler`), restart-once-on-crash. The uint8 graph
  wrapper's tail must stay `Transpose(float) → Cast(uint8)` (3-5x faster TRT
  engines than NCHW-uint8 output; uint8 only legal at network boundaries).
- NVDEC decode + NVENC encode concurrently in one process → native AV.
- TRT builds engines from *live timing measurements*: engines built while the
  GPU is busy (user games on this box) are permanently slow — delete
  `models/.trt_cache` and rebuild with an idle GPU.

## Known issues / current debugging front

- **Intermittent server crash** (~1 in 5 full 4K lossless-hevc runs), native
  AV, cause not yet pinned — need a faulthandler stack from a crash (server
  prints it to its terminal). If it lands in NVENC, the plan is to move encode
  into the worker subprocess like TRT.
- Single-box operation contends (server pipeline + client decode); the Linux
  laptop client is the intended topology.
- FFV1 has no hardware decoder anywhere (codec-inherent); lossless-hevc is
  the recommended lossless tier for live playback.
- `gitignore`d and machine-local: `models/` (onnx + trt cache), `mpv-dev/`
  (Windows libmpv DLL), venvs, `*.mkv` test media. The laptop needs none of
  them (distro libmpv + no models client-side).

## Testing conventions

- Integration tests spin the real server in-process on a private port pair
  (see `tests/test_streaming.py::free_port_pair` — never check-then-use
  ephemeral ports).
- GUI verification: offscreen smoke pattern — `QT_QPA_PLATFORM=offscreen` +
  `relay-desktop --headless --settings-scope <test>` (or pass matching
  `DesktopOptions`) and drive `MainWindow` slots directly. Seek verification needs a file with a
  sparse keyframe interval (`upscale-cli sample` output has keyint 250).
- PTS equivalence is the core invariant: demux the downlink MKV and compare
  against source packet timestamps (see `demux_downlink_pts`).
