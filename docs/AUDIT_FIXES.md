# September 2026 codebase audit fixes

Baseline: `89cf3a8`. Work branch: `fix/codebase-audit-2026-09-22`.
Each defect has its own commit; follow-up defects found during integration are
separate commits. The audit covered the shared protocol/media layer, client
core and desktop, server pipeline and GUI, offline CLI, tests, and packaging.

## Original findings

| Finding | Result | Initial fix |
| --- | --- | --- |
| F1: offline output aliases its source | Reject identical paths, symlinks and hard links before allocating stages. | `50b6cef` |
| F2: unbounded peer payload allocation | Reject payloads above 64 MiB before reading/allocating; bound queue and batch bytes. | `2915ec8`, `ac9096d` |
| F3: dropped container bytes at thread handoff | Reserve bounded queue capacity with blocking backpressure and retire it on teardown. | `4ac4aa7` |
| F4: overlapping seeks lose acknowledgements | Correlate replies by epoch and explicitly supersede prior waits/uplink work. | `4c3595e` |
| F5: headless CLI retains the entire stream | Discard ordinary payloads; spool optional decode verification to a temporary file. | `545d214` |
| F6: failed pipeline construction leaks owners | Roll back partial allocation; preserve failed native cleanup as a restart barrier. | `caabbce`, `0fccf05` |
| F7: failed worker startup leaks shared memory | Unwind each successfully allocated segment after allocation/start failure. | `9ce2ef2` |
| F8: stale worker readers poison replacements | Capture each generation's reply queue and reap processes/readers before reuse. | `1b77481` |
| F9: media sockets outlive sessions | Own/close media handlers, reject duplicate directions and clear retired payloads. | `a084b02` |
| F10: blocked loopback sender survives abort | Shut down the accepted socket before closing and join the sender. | `0d01548` |
| F11: GUI partial startup leaves an allocated session | Retire failed opens through the confirmed native teardown barrier. | `6802c6b` |
| F12: keyboard pause bypasses caller intent | Route Space and toolbar transport through the same controller. | `ae46880` |
| F13: local fallback has inert controls | Give local playback transport, track and position reporting. | `259b19e` |
| F14: subtitles beginning before a seek disappear | Preserve exact text-subtitle packets in a bounded temporary session index; replay overlapping events. | `009f7ac` |
| F15: benchmark encodes the wrong dimensions | Set actual output geometry before encoding and verify the decoded result. | `7f49b96` |
| F16: fullscreen light palette is unreadable | Apply coherent foreground/background colors to the overlay and controls. | `650f545` |
| F17: source GUI redundantly installs a managed runtime | Validate the source environment in an isolated inference subprocess before deciding setup is needed. | `d3ae86b` |
| F18: filesystem/native waits block asyncio | Offload source opens and path validation; invalidate iterators without waiting for native demux locks. | `bd430a7`, `896f250` |
| F19: overlapping tray restarts orphan a server | Serialize lifecycle changes, unwind unpublished startup, and coalesce GUI requests. | `46f3576` |
| F20: malformed/IPv6 addresses break connection handling | Validate input inside the handled operation and format IPv6 HTTP authorities correctly. | `58e27f6`, `5ffa345` |
| F21: benchmark temporary directories accumulate | Manage temporary media with guaranteed cleanup, including encode failures. | `0b1daf8`, `dffb4cd` |

## Resource and interface improvements

- Synthetic benchmarks generate frames lazily; offline frame conversion reuses
  reformatters. Missing NVENC is recorded without discarding the benchmark report.
- Diagnostic logs rotate without replacing native fault/worker file descriptors.
- Font cache views hold filesystem leases; abandoned views are reclaimed while
  active playback views remain usable.
- Desktop model, quality, framing, resize and debanding controls live in the
  requested Playback Settings panel. Connection status remains visible.
- Visible volume/mute controls, delay explanations and opening guidance improve
  the desktop transport. The tray shows persistent status, both ports and restart
  progress, explains that Apply disconnects playback, and bounds the control port.
- Shutdown closes active and unhandshaken media clients before awaiting listener
  completion. Failed listener startup rolls back previously allocated listeners.
- The final player handoff rejects superseded packets, including stale EOF and
  packets received while a native stream reload was settling.
- Local fallback waits for native file readiness before returning control;
  immediate pause/seek and cancellation during loading have regression coverage.
- Video-only originals require no external media attachment. Subtitle-only
  originals use one delayed `sub-add`; audio-bearing sources retain one delayed
  `audio-add`. Both paths preserve the caller's track and pause choices.
- Receiving an epoch's end marker keeps the desktop consumer alive for later
  seeks. Fast servers can finish transmitting a short clip well before playback
  ends; this case is covered by a new regression and a real Windows relay test.

The selected subtitle cache is temporary, per session, and deleted on Stop. It
stores original packet bodies, timestamps and codec side data in SQLite, with
256 MiB disk and 2 MiB page-cache limits. Ordinary playback fills it progressively;
a seek beyond indexed media scans the unseen source prefix without decoding it.
The UI reports this work. Stateful/bitmap subtitle formats negotiate the external
media path because packet duration alone cannot reconstruct their display state.

## Verification and deliverable

Targeted regressions cover resource boundaries, rollback, reordered seek replies,
blocked sockets, local playback controls, real libmpv state and subtitle overlap.
The reusable `tools/smoke_desktop.py` drives the actual MainWindow/qasync/libmpv
client against a running relay, with a unique settings scope. It checks pause,
paused/overlapping seeks, native positions, resume, local fallback and Stop; an
optional expected subtitle verifies text active at the paused seek target.

CI includes Linux offscreen GUI and CPU ONNX tests, Windows core/tray tests, and
frozen executable dispatch/ONNX/pip checks. It builds a Windows x64 GUI ZIP with
its icon, all `_internal` dependencies, setup instructions, source provenance and
SHA-256 checksum, then smoke-tests a relocated extraction of that ZIP.

Actual NVIDIA inference, Windows driver interaction and the previously documented
intermittent native GPU crash require the desktop relay. Local CPU passthrough
checks isolate client/transport correctness; they do not establish GPU throughput,
real network capacity or native OpenGL rendering. No cause or fix is claimed for
the existing intermittent NVIDIA crash without its native fault evidence.

### Local validation results

The complete local suite finished with **282 passed, 4 skipped** using offscreen Qt and
`RELAY_LOSSLESS_HEVC_PROFILE=x265-ultrafast`; the shipped default encoder-profile
test also passed separately without that override. Local skips cover unavailable
ONNX Runtime, Windows registry behavior and the intentionally overridden default.
The CI lanes supply CPU ONNX Runtime and Windows registry coverage.

One pre-CI run also exposed an intermittent native Qt timer crash. Its core
dump was retained for review. An existing test manually called `processEvents()`
inside an asyncio coroutine and suppressed Linux faulthandler output. That test
now uses an owned qasync loop and awaits player task shutdown; the ordered native
playback regression group passed after correction. This removes the concrete
reentrancy violation without attributing every possible native fault to it.

Native desktop smoke checks passed for video-only, subtitle-only and audio/ASS
uplinks, including local fallback; server-file muxed audio/ASS passed with FFV1
and software lossless HEVC. A server-file external subtitle-only run also passed.
These include native paused positions, overlapping seeks and subtitle text active
across the seek. The old subtitle implementation failed that text assertion;
the temporary index passed with the same fixture.

The real Windows relay's fast NVENC passthrough initially exposed a client seek
failure after all epoch bytes had arrived. After the EOS consumer fix, the same
Windows lossless-HEVC run passed playback, keyboard pause, active subtitle text,
paused/overlapping seeks, resume, local fallback and Stop. This validates the new
client against the existing Windows server. Re-run against the newly installed
ZIP to exercise the updated server together with the client.
