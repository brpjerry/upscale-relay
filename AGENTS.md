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

## Desktop state and configuration ownership

- `history.py`, `browser_state.py`, `discovery.py`, `mpv_config.py`,
  `diagnostics.py`, and `playback_state.py` own the corresponding state;
  `features.py` integrates their controls with the retained tree browsers.
- History/browser records are versioned QSettings values. Local identities use
  normalized absolute paths; server identities include configured host/port.
  Stable progress saves every five seconds; resume excludes the first ten and
  final 90 seconds. The final 90 seconds count as watched. Autoplay defaults on
  and walks unwatched siblings in name order only after confirmed epoch EOS.
- Open/stop/restart/fallback/autoplay transitions serialize native ownership.
  Capture position, pause, track descriptors, and delays before teardown using
  cached observations. Preserve the confirmed server barrier when cancelling.
- `sid`, `slang`, `video-sync`, `interpolation`, and `tscale` belong to the shared
  user `mpv.conf`, resolved through libmpv. The GUI discloses that edits also
  affect standalone mpv. Never duplicate these defaults in QSettings, rewrite
  named profiles, or reload the whole config into active playback. GUI edits
  apply only exposed properties when stable; external edits wait for a new
  session. Current-file track choices stay session-only and use descriptors.
- Diagnostic display and client file logging default off independently. Metrics
  and timed buffer reports continue with diagnostics hidden. Client logs use
  timestamped Documents files, bounded queues/rollover, ten-file retention, and
  URL/token redaction before queuing. Do not dump configuration contents.
- Tests MUST isolate `settings_scope` and use temporary `mpv_config_path` and
  `log_root` overrides (the options supply temporary defaults for isolated or
  headless runs). Disable real discovery unless testing it. Never touch the
  user's mpv configuration or production history. On CPU-only test machines,
  use `RELAY_LOSSLESS_HEVC_PROFILE=x265-ultrafast` for the full suite.

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
- For server-file muxed auxiliary tracks, seek the auxiliary container against
  the video stream's keyframe cues. Matroska audio streams are often not
  indexed; using audio as the seek anchor caused a measured 7-second scan.

**asyncio / Qt (qasync)**
- **No modal dialogs / exec() / processEvents from coroutine context** — the
  nested loop re-enters asyncio tasks and ends in memory corruption.
  `MainWindow._error()` is non-modal on purpose.
- A garbage-collected `asyncio.StreamWriter` closes its socket — keep refs.
- Client must send `buffer_report` on a timer with *live* values; reporting
  only on packet arrival deadlocks the server's watermark pause/resume.
- Keep mpv's render call blocking (`block_for_target_time`, the default)
  and keep `video-timing-offset=0` (set in `mpv_view.py`, re-asserted after
  `mpv.conf`). With mpv's default offset (0.05 s) a frame is announced ~one
  period early and `render()` waits the difference out on the GUI thread
  (~40 of every 42 ms at 24 fps): sidebar slides ran at 12-14 fps, a live
  splitter drag at 9 fps (230 ms behind the pointer), and only 83-85% of
  frames landed on the 5-vsync cadence of 24 fps at 120 Hz. With offset 0
  the frame is announced at its display time and `render()` returns at once:
  paints ~2.5 ms, frames submitted 3 ms median after target (p95 5-6 ms),
  99.6-100% on cadence, 0 drops (2026-10-07, Lunar Lake 120 Hz; MV Player
  has run offset 0 since its first commit). The rejected alternatives never
  included offset 0. Measured on 2026-10-03 and rejected: rendering without
  `block_for_target_time` shows every frame a period early (39.5 ms at 24 fps,
  video ahead of audio, invisible to mpv's `avsync`); waiting on a Qt timer
  for `next_frame_info.target_time` (nanoseconds, despite the header) is on
  time in steady state, but any other repaint of the video widget then
  consumes the pending frame early, and `screenshot-raw` intermittently
  stalls frame delivery ~0.3 s (with offset 0 and the blocking call, 15 s of
  sampling once a second dropped nothing).
- A fluid UI during playback no longer needs display sync. With
  `video-timing-offset=0` the GUI thread is free between frames: chrome
  slides step once per presented frame (`_FrameSlide`, on the video widget's
  `frameSwapped`, with a timer fallback while the window is unexposed;
  100-120 fps at 1080p, 0 drops) and the splitter resizes live (~110 fps,
  edge 2-3 px behind the pointer, ~15 ms lag). "Fluid interface during
  playback" (off by default) still opts into mpv's display sync: it hands
  timing back to mpv ~0.9 s after entering fullscreen, where display pacing
  costs +3 W, and mpv switches either way in ~0.2 s with no drop
  (`_apply_display_pacing`); `MpvPlayerView` reports the screen's refresh
  rate (`display-fps-override`; the render API cannot see the display), so
  `video-sync=display-*` renders once per refresh. Display sync drops video
  whenever the video widget resizes, because mpv counts every extra
  `render()` as a vsync: 13-18 drops per sidebar show slide (2-3 for the
  pinned hide slide) and 70-77 per live splitter drag. `interpolation` only
  blends frames. Under display sync `screenshot-raw` holds mpv's rendering
  up ~0.3 s, so the auto accent samples only at open/seek/pause there
  (`_request_accent_sample`).
- No synchronous mpv property reads in periodic GUI-thread code. A read
  waits for mpv's core, which can be waiting for the GUI thread to render:
  two of `_stats_loop`'s reads stalled ~210 ms under display sync. The stats
  tick reads `_OBSERVED_PROPERTIES`, cached by mpv's event thread (they stay
  live while paused, so `buffer_report` still carries live values). mpv never
  announces `display-sync-active` changes; `display_sync_active()` reads the
  observed `vsync-ratio`, which exists exactly while display sync is active.
- Under qasync the loop turned ~once per rendered frame (~25/s) while
  `render()` waited out each frame (before `video-timing-offset=0`). Media
  pumps must move batches per loop turn regardless: per-packet
  `to_thread`+`drain` capped the uplink at ~12 pkt/s (starved the server
  below realtime), and the 64 KiB StreamReader default capped the downlink
  at ~1.6 MB/s. See `_UPLINK_BATCH` / `_DOWNLINK_READ_LIMIT` in
  `relay_client_core/client.py`.

**mpv (python-mpv, embedded)**
- The downlink is a live Matroska stream via a per-load localhost `tcp://`
  socket. Keep the dedicated sender thread: python-mpv's custom-stream adapter
  copies bytes one at a time in Python and capped lossless HEVC near 200 Mbps.
- Relay `loadfile` options must disable mpv's network timeout for that epoch.
  Pausing legitimately silences the TCP pipe; the default 60-second read timeout
  became a false EOF after resume drained the cache. Stop/seek and real transport
  failures still close the pipe explicitly; ordinary local playback keeps its
  configured timeout.
- When Qt's window is unexposed (e.g. another Wayland workspace), acknowledge
  render updates with `skip_rendering=True` and a current OpenGL context. Relying
  only on Qt paints makes libmpv count every hidden frame as a renderer drop.
- Post-seek: **never** pass `start=`; we run `rebase-start-time=no` so the new
  stream's absolute PTS place playback and external audio aligns itself.
  Reload = `stop`, close old buffer, `await asyncio.sleep(0.15)`, then
  `loadfile` — no synchronous mpv property reads during teardown.
- mpv OSC (LuaJIT) intermittently crashes mpv's event thread on stream
  reloads → OSC off by default. LuaJIT's caught SEH exception `0xe24c4a02`
  in faulthandler output is *benign noise*, not a crash.
- On Linux's embedded Qt/OpenGL render path, re-assert `hwdec=auto-copy-safe`
  after loading `mpv.conf`. A real core landed in
  `paintGL → mpv_render_context_render → vaSyncSurface → iHD` when the user's
  `hwdec=vaapi` exposed a retired zero-copy Intel surface. Copy-back retains
  hardware decode; never restore zero-copy VA-API as the default here.
- For external auxiliary media, load the video-only epoch paused, then issue
  exactly one raw-argument `audio-add` after mpv's `playback-restart`; use
  `sub-add` instead only when source metadata confirms subtitles without audio.
  Video-only originals need no external attachment. The single external
  demuxer contributes both audio and subtitle tracks. Attaching during
  `loadfile` positions the original demuxer at zero and makes far seeks decode
  through old media. On desktop, release the load-time hold when the attachment command
  returns (`audio-pts` is not reliable while paused), and preserve the caller's
  pause intent across that hold.
- For a server-file session confirmed as `aux_tracks:"muxed"`, never attach
  `/media` or issue `audio-add`. Prepare any confirmed cached font view before
  the first `loadfile`, then re-enumerate and remap track choices after every
  fresh epoch. Requested mode alone is not authoritative.

**GPU stacks (server box: RTX 5090 "Blackwell", Windows)**
- ORT TensorRT EP corrupts the process heap under streaming → it lives in a
  subprocess (`SubprocessUpscaler`), restart-once-on-crash. The uint8 graph
  wrapper's tail must stay `Transpose(float) → Cast(uint8)` (3-5x faster TRT
  engines than NCHW-uint8 output; uint8 only legal at network boundaries).
- NVDEC decode + NVENC encode concurrently in one process → native AV.
- TRT builds engines from *live timing measurements*: engines built while the
  GPU is busy (user games on this box) are permanently slow — delete
  `models/.trt_cache` and rebuild with an idle GPU.
- **Reproduce any playback bug on the `passthrough` model first.** This GPU is
  shared with whatever the owner is doing on the box, so a model that normally
  keeps up can quietly fall under realtime — and a starved pipeline produces
  exactly the client-side symptoms you are usually trying to measure: dropped
  frames, rebuffers, stalls. Passthrough takes inference out of the picture;
  put the model back once the behaviour is understood. The tells that you are
  measuring contention rather than the bug are `/status →
  sessions[].pipeline.fps` below the source frame rate and the client's mpv
  `cache` draining toward zero. Desktop shows buffering and optional metrics;
  the actionable "server is not keeping up" warning is an Android feature
  and remains unimplemented on desktop. This cost a round of confounded
  frame-drop measurements
  on 2026-07-26. Match the *network* the same way: a tier the client's Wi-Fi
  cannot carry starves it just as effectively, and looks identical.

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
  (see `tests/ports.py::free_port_pair` — never check-then-use ephemeral
  ports; Linux's default ephemeral range starts at 32768).
- Native Qt playback tests use `tests/qt_helpers.py::playback_loop` so closed
  qasync loops retire their remaining native timers on the GUI thread. Leaving
  those timers for cyclic GC caused a reproducible crash in the next Qt loop.
- GUI verification: offscreen smoke pattern — `QT_QPA_PLATFORM=offscreen` +
  `relay-desktop --headless --settings-scope <test>` (or pass matching
  `DesktopOptions`) and drive `MainWindow` slots directly. Seek verification needs a file with a
  sparse keyframe interval (`upscale-cli sample` output has keyint 250).
- PTS equivalence is the core invariant: demux the downlink MKV and compare
  against source packet timestamps (see `demux_downlink_pts`).
