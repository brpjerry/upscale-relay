# Repository code review — 2026-10-09

Baseline: `ae8dec4` (clean working tree when reviewed).
Work branch: `review/repository-audit-2026-10-09`.

The review covered the server, shared protocol/media layer, client core,
desktop, inference and offline tooling, runtime setup, packaging, CI, and
related tests. Three subagents reviewed the server/protocol, desktop, and
client core; the primary reviewer covered the remaining areas and
cross-checked findings.

The Windows/NVIDIA continuation reviewed the same application baseline and
added findings 15–19, native corroboration of finding 12, and the reuse and
consistency opportunities below. The original Linux evidence is retained.

There are **19 actionable application findings**. **P1 = high priority;
P2 = medium priority.** All findings remain open; this report does not
implement fixes. Source line references refer to the reviewed baseline.

## Findings

### 1. [P1] One session can buffer 16 GiB of compressed input

Location: [relay_server/pipeline.py:292](../relay_server/pipeline.py#L292).

The input queue limits packet count to 256, but each accepted wire packet may
contain 64 MiB. When decoding is paused or falls behind, this permits
server-wide memory exhaustion and can terminate every active session.

**Evidence:** A real paused pipeline accepted 256 maximum-sized packet
references, representing 17,179,869,184 payload bytes. The reproduction
deliberately reused one allocation to avoid consuming 16 GiB; actual network
reads allocate separate payloads.

**Fix direction:** Add byte backpressure to pipeline input buffering,
including video and auxiliary input, rather than relying only on packet count.

### 2. [P2] Cancelled seeks can silently remove the new epoch's audio and subtitles

Location: [relay_server/session.py:620](../relay_server/session.py#L620).

Auxiliary iterator creation happens inside a worker after reading the first
video packet. Cancelling its asyncio task leaves that worker running. If a
replacement seek has already created its auxiliary iterator, the obsolete
worker's late `aux_track.packets(...)` call increments the shared generation
and invalidates the replacement iterator. Video continues while auxiliary
tracks disappear for the remainder of the epoch.

**Evidence:** A controlled reproduction using real `VideoTrack` and
`AuxiliaryTrack` demuxers, real H.264/AAC media, the real session source loop,
and a recording pipeline delivered all 72 video packets after a seek, but
only 11 of 141 auxiliary packets. Audio ended at 1.216 seconds instead of
3.989 seconds.

**Fix direction:** Establish iterator ownership before cancellable worker
execution and prevent retired batches from acquiring a new generation.

### 3. [P2] Variable-frame-rate timestamps change during encoding

Locations: [relay_server/pipeline.py:513](../relay_server/pipeline.py#L513)
and [upscale_cli/stages.py:142](../upscale_cli/stages.py#L142).

Both encoder constructors specify an average frame rate without setting the
encoder codec context's time base. Supplying each frame's original time base
later does not prevent FFmpeg from rounding timestamps to the encoder's
frame-rate grid. This changes playback timing and violates the PTS invariant
in streaming and offline output.

**Evidence:** A valid H.264 source with millisecond timestamps
`[0, 33, 83, 117, 167, 200, ...]` became
`[0, 42, 83, 125, 167, 208, ...]` through the actual passthrough FFV1
pipeline. Offline `FrameSink` produced the same retiming, while its
`pts_written` list retained the original timestamps.

**Fix direction:** Configure an appropriate encoder codec context time base
before encoding. Leave the output stream time base under muxer ownership,
as required by the project's native-runtime rules.

### 4. [P2] TensorRT playback rejects 4K sources before tiling can run

Location: [upscale_cli/infer_worker.py:304](../upscale_cli/infer_worker.py#L304).
Related caller: [relay_server/pipeline.py:301](../relay_server/pipeline.py#L301).

The subprocess facade rejects any source larger than 2560×1440, despite the
pipeline explicitly selecting tiling for larger sources. The full source
frame reaches this guard before worker-side tiling can run.

**Evidence:** Calling the facade with a 3840×2160 frame raised
`ValueError: frame 3840x2160 exceeds worker input cap 2560x1440`, even with
`tile_size=1024`. This guard was reproduced without NVIDIA execution and
independently checked against the pipeline call path.

**Fix direction:** Tile before crossing the fixed shared-memory boundary,
or size the shared buffers appropriately. Preserve subprocess isolation for
TensorRT inference.

### 5. [P2] Local/SMB source-read failures leave playback buffering indefinitely

Location: [relay_client_core/client.py:690](../relay_client_core/client.py#L690).
Related exception handling: [client.py:709](../relay_client_core/client.py#L709).

A demux exception terminates the background uplink task without notifying
playback, closing the uplink, or terminating the session. Neither desktop
playback nor the CLI monitors that task. The server keeps awaiting packets
while control traffic and buffer reports remain healthy.

**Evidence:** Injecting `OSError("SMB source read failed")` from the source
iterator left the writer open, `client.errors` empty, and the downlink
consumer waiting without a failure notification.

**Fix direction:** Propagate pump failures into session cleanup and a visible
playback error or fallback path.

### 6. [P2] Valid FFV1 source files cannot play through the relay

Location: [relay_server/pipeline.py:463](../relay_server/pipeline.py#L463).

The reconstructed decoder receives codec name and extradata but not source
width and height. FFV1 requires those container-supplied dimensions, so an
ordinary FFV1 MKV accepted by the library produces a fatal pipeline error.

**Evidence:** A generated 32×32 FFV1 MKV decoded all 12 frames normally, but
feeding its actual metadata and packets through the pipeline failed at
`avcodec_open2("ffv1", {})`. The standalone decoder decoded all 12 frames
after assigning width and height.

**Fix direction:** Initialize required source dimensions on decoder contexts.

### 7. [P2] Anamorphic sources display with distorted proportions

Locations: [relay_media/demux.py:145](../relay_media/demux.py#L145)
and [relay_server/pipeline.py:327](../relay_server/pipeline.py#L327).

Source metadata omits sample aspect ratio, and fitting uses only stored
pixel dimensions. The output also lacks the original sample aspect ratio,
so anamorphic sources have distorted geometry even using passthrough.

**Evidence:** A real 720×576 H.264 source with SAR 16:15 and display aspect
4:3 produced 1350×1080 FFV1 with no sample aspect ratio for a 1920×1080
display. It therefore displays at 5:4; correct square-pixel fitting would
produce 1440×1080.

**Fix direction:** Carry sample aspect ratio through source metadata and
configuration, and account for it during fitting and cropping.

### 8. [P2] Pausing through input.conf is lost after a seek or restart

Location: [desktop_client/mpv_view.py:604](../desktop_client/mpv_view.py#L604).
Related epoch release: [mpv_view.py:1089](../desktop_client/mpv_view.py#L1089).

The native pause observer updates state only for idle inhibition, leaving
`_caller_paused` and `MainWindow._paused` stale. A native pause binding can
pause playback while the toolbar still indicates playing; a relay seek,
settings restart, or fallback then uses the wrong pause intent.

**Evidence:** With temporary `input.conf` containing `P cycle pause`, real
headless libmpv and a private passthrough server reported `mpv.pause=True`
while both application pause flags remained false. Seeking to eight seconds
resumed playback.

**Fix direction:** Synchronize user-originated native pause changes with
application intent while excluding internal epoch-loading holds.

### 9. [P2] Linux inference imports fail when TensorRT libraries are installed

Location: [upscale_cli/infer.py:46](../upscale_cli/infer.py#L46).

The import-time DLL setup detects `tensorrt_libs` and unconditionally calls
Windows-only `os.add_dll_directory`. It also prepends paths using semicolons
instead of the platform path separator. Linux inference environments with
that directory fail before execution-provider selection, including when
requesting CPU execution. Ordinary Linux client-only installations lack
this directory and are unaffected.

**Evidence:** Exercising the actual directory-setup helper on Linux against
a temporary site-packages layout containing `tensorrt_libs` produced
`AttributeError: module 'os' has no attribute 'add_dll_directory'` and a
semicolon-separated PATH prefix.

**Fix direction:** Guard Windows DLL setup by platform and use platform-aware
path handling.

### 10. [P2] Concurrent playback startup can evict another session's fonts

Location: [relay_client_core/attachments.py:241](../relay_client_core/attachments.py#L241).

Eviction protects only its caller's manifest. Another session's downloaded
or verified objects remain vulnerable until its font view is linked. A
second player opening against a cache near capacity can delete an object
the first player is preparing, failing the first playback startup.

**Evidence:** A deterministic concurrent mock-HTTP reproduction paused
session A after downloading its first font. Session B completed and evicted
that font; resuming A failed with `FileNotFoundError` in `_populate_view`.
The cache budget was scaled down to reproduce the race without large files.

**Fix direction:** Protect in-progress objects across processes, or acquire
verified references incrementally and coordinate eviction with publication.

### 11. [P2] Interrupted font downloads bypass the persistent-cache limit

Location: [relay_client_core/attachments.py:285](../relay_client_core/attachments.py#L285).
Success-only eviction: [attachments.py:300](../relay_client_core/attachments.py#L300).

Objects are published individually, but eviction runs only after the entire
operation succeeds. Failure or cancellation on a later attachment retains
earlier downloads without enforcing the documented 512 MiB persistent-cache
limit. Repeated interrupted opens can keep growing the object store.

**Evidence:** With the budget scaled to ten bytes, four opens each published
a distinct valid six-byte font and failed on the next download. The cache
contained 24 bytes afterward, with no session views created.

**Fix direction:** Enforce bounds on unsuccessful paths as well as successful
ones, while protecting objects needed by concurrent operations.

### 12. [P2] Windows local autoplay breaks after advancing to the next file

Location: [desktop_client/features.py:408](../desktop_client/features.py#L408).

The next-sibling helper uses `PurePosixPath` to extract local Windows
basenames. Initial Qt paths use forward slashes, but autoplay returns a
native backslash-separated path. The next comparison treats that entire
drive-qualified path as the filename and can stop or select the wrong file.

**Evidence:** Running the actual helper with a Windows-path filesystem
adapter containing `01.mkv`, `02.mkv`, and `03.mkv` advanced from
`C:/shows/01.mkv` to `C:\shows\02.mkv`, then returned no next file.

**Fix direction:** Use native `Path` for local basenames and reserve
`PurePosixPath` for server-relative paths.

### 13. [P2] Starting playback during initial library loading strands the browser

Location: [desktop_client/main_window.py:1951](../desktop_client/main_window.py#L1951).

Initial connection adoption awaits the library listing inside a cancellable
playback transition. Starting a local video cancels that request, but
`CancelledError` bypasses `except Exception`, and the flag-resetting
`finally` only covers later tree restoration. The Server tab remains in its
loading state and browser-state persistence stays suppressed until refresh.

**Evidence:** The real window transition and refresh methods, with a delayed
fake client, successfully started local playback while leaving zero library
rows, the loading placeholder, and `_restoring_browser=True`, with no error
reported.

**Fix direction:** Make the complete refresh cancellation-safe and complete
or restart interrupted initial loading.

### 14. [P2] mDNS failures leak Zeroconf resources

Locations: [relay_server/mdns.py:70](../relay_server/mdns.py#L70)
and [mdns.py:83](../relay_server/mdns.py#L83).

The advertiser retains ownership only after registration succeeds, so
registration failure or cancellation abandons the allocated instance.
Shutdown also skips closing when unregistration fails. Repeated tray
restarts can accumulate abandoned sockets and background activity.

**Evidence:** A tracking `AsyncZeroconf` fake raised
`OSError("interface disappeared")` during registration. Running the real
advertiser's start and stop constructed one instance and closed none.

**Fix direction:** Retain ownership immediately, close unsuccessful startup
allocations, and guarantee closing through `finally` during shutdown.

### 15. [P2] Cancelling an uplink iterator worker can prematurely end the replacement seek

Location: [relay_client_core/client.py:676](../relay_client_core/client.py#L676).
False completion: [client.py:702](../relay_client_core/client.py#L702).

The client acquires its `VideoTrack` iterator inside `asyncio.to_thread`.
Cancelling the task cannot stop a worker that has already started. If that
obsolete worker acquires its iterator after the replacement seek's iterator,
it advances the shared generation and invalidates the replacement. The new
pump mistakes that iterator exhaustion for natural completion and sends EOS
for the current epoch. This is the video-uplink counterpart of finding 2's
server-side auxiliary race; it can truncate the entire playback epoch.

**Evidence:** A controlled Windows reproduction used a real generated H.264
source, real `VideoTrack`, and the actual `RelayClient.start_uplink` and
`_uplink_loop`. A scheduling barrier delayed the obsolete worker immediately
before iterator acquisition until the replacement sent its first batch.
The replacement emitted only **16 of 120 video packets**, then EOS for epoch
1, without an exception. The source itself was complete and readable.

**Fix direction:** Acquire the cheap generation-owning iterator before
cancellable worker execution, and offload only its native advancement. The
server video path already does this at
[session.py:596](../relay_server/session.py#L596). Apply the same ownership
rule to that path, client video, and server auxiliary iterators.

### 16. [P2] Streaming and offline encoding lose color signaling and change colors

Locations: [relay_server/pipeline.py:513](../relay_server/pipeline.py#L513)
and [upscale_cli/stages.py:139](../upscale_cli/stages.py#L139).
Source description: [relay_media/demux.py:141](../relay_media/demux.py#L141).

The source description and reconstructed output streams omit color matrix,
range, primaries, and transfer information. Pixel-format conversion alone
does not establish the correct interpretation of the encoded output. Even
passthrough with a lossless codec can therefore produce different displayed
colors, independently of model behavior or compression quality.

**Evidence:** A real full-range BT.709 H.264 fixture passed through the CPU
passthrough FFV1 pipeline lost its matrix/range/primaries/transfer values:
`(1, 2, 1, 1)` became unspecified `(2, 0, 2, 2)`. Decoding the same constant
region to RGB changed `[178, 95, 128]` to `[178, 79, 129]`. Offline `FrameSink`
reproduced the metadata loss and the pixel change. This proves a concrete SDR
case; HDR conversion and tone mapping were not validated by this fixture.

**Fix direction:** Define the conversion and output color semantics explicitly;
carry source signaling when appropriate and configure both conversion and
encoder metadata consistently. Share that setup between streaming and offline
encoding. Add decoded-output checks instead of checking metadata alone.

### 17. [P2] Muxed audio tails accumulate outside backpressure until EOS

Location: [relay_server/pipeline.py:950](../relay_server/pipeline.py#L950).
Storage: [pipeline.py:187](../relay_server/pipeline.py#L187).

The finish worker muxes auxiliary packets and immediately continues without
draining available container bytes. `_SinkBuffer` appends without a byte
limit; normal drains occur after video frames or final EOS. Consequently,
audio after the final video frame, or across long video gaps, accumulates
outside the bounded queues. A slow source can withhold playable audio until
the entire tail has been read, and longer tails increase memory consumption.

**Evidence:** A real H.264/PCM MKV contained 0.5 seconds of video and 25.6
seconds of audio. Feeding it in the server's timestamp merge order produced
600 audio packets, including 589 after the video. Once those packets traversed
the otherwise empty stage queues, emitted output remained **5 packets /
71,907 bytes**, while `_SinkBuffer` retained **4,811,479 bytes**. Only EOS
released the remainder, for 4,932,631 total container bytes. No error occurred.

**Fix direction:** Drain available mux bytes after auxiliary packets as well,
preserving bounded downstream backpressure and the first discontinuity. Use
`NO_TS` when a chunk has no associated video timestamp; do not invent video
PTS for audio-only output.

### 18. [P2] Invalid tile sizes can return an unwritten output image

Locations: [upscale_cli/infer.py:155](../upscale_cli/infer.py#L155),
[infer.py:254](../upscale_cli/infer.py#L254), and
[infer.py:277](../upscale_cli/infer.py#L277).

The constructor validates overlap parity but not the relationship between
tile size and overlap. With the public `--tile-size 8` and default overlap
16, the negative stepping produces an incomplete tile grid, and the crop
assignments write no pixels into the `np.empty` output. The operation returns
success with uninitialized image data. Tile size 16 instead fails with a
zero-step `range` error.

**Evidence:** Real CPU ONNX execution of the synthetic bilinear2x model on
an 80×80 image, with tile size 8 and overlap 16, generated starts `[72]`.
All **76,800 elements** of the returned 160×160×3 output retained an allocation
sentinel inserted to measure unwritten memory. The untiled reference was
uniformly 127. The sentinel instrumentation does not change tiling or copying.

**Fix direction:** Reject nonpositive tile sizes, negative overlap, and
`tile <= overlap` before model execution or output allocation. Validate the
same contract for constructor settings and direct tiled-inference calls.

### 19. [P2] Worker recovery can silently abandon the server's GPU requirement

Locations: [upscale_cli/infer_worker.py:249](../upscale_cli/infer_worker.py#L249)
and [infer_worker.py:312](../upscale_cli/infer_worker.py#L312).
Initial check: [relay_server/pipeline.py:317](../relay_server/pipeline.py#L317).

Pipeline construction rejects an inference session that falls back to CPU
when GPU inference was requested. Worker recovery replaces `active_provider`
from the new READY message, then retries the frame without repeating that
check. A GPU worker crash followed by provider initialization failure can
therefore leave playback on CPU, contrary to the original acceptance policy,
with severe starvation instead of the actionable error used at startup.

**Evidence:** Controlled real child processes exercised the actual
`SubprocessUpscaler` lifecycle. The first announced TensorRT and exited on its
first frame request. The replacement announced CPU and returned an 8×8 frame;
the facade accepted it without error. Running the existing server provider
check against that replacement correctly rejected it. This reproduces the
policy gap, not a claim that this machine's installed GPU naturally fell back.

**Fix direction:** Apply a shared provider-acceptance policy after every READY,
including recovery, and clean up rejected replacements. Preserve deliberate
CPU execution where explicitly requested.

## Reuse, simplification, and consistency opportunities

These are implementation directions, separate from the reproduced defects.
They should preserve the native thread/process ownership rules rather than
merge components merely because their code looks similar.

1. **Share encoder configuration and a complete video description.**
   `Pipeline._open_mux` and `FrameSink._init_stream` repeat stream setup and
   share the timestamp/color omissions in findings 3 and 16. A dependency-light
   description of geometry, time base, sample aspect ratio, and color semantics
   plus common encoder configuration would let both paths use one correction.
   Keep mux ownership and cleanup local to each existing thread. Never set the
   output stream time base; configure the codec context where appropriate.
2. **Use one iterator-ownership rule across local and server sources.**
   Acquire generation ownership before entering cancellable work, then perform
   serialized native demux on the worker. The server video path already offers
   the pattern missing from client video and server auxiliary paths (2 and 15).
3. **Share byte-budget and retirement invariants across queue boundaries.**
   Server `_DownlinkQueue` and client `_ThreadBridgeQueue` account for bytes,
   while pipeline input and mux buffers do not (1 and 17). Reuse accounting
   policy and contract tests across synchronous and asynchronous adapters;
   retaining separate synchronization mechanisms is appropriate.
4. **Extract the abandoned-worker result handoff.**
   `_SourceOpening` in `relay_client_core/client.py:59` and `_ViewPublication`
   in `attachments.py:29` duplicate lock-protected publication, abandonment,
   and cleanup-future observation. A small shared helper with resource-specific
   cleanup callbacks could reduce cancellation drift. Preserve the tests for
   late completion and repeated cancellation.
5. **Centralize source-aware paths and playable suffixes.**
   Use native `Path` for local media and POSIX paths for server-relative media.
   Align basename selection and name ordering in `features.py`,
   `browser_state.py`, and `relay_server/library.py`. Move the duplicated
   playable-extension set out of `main_window.py` and `library.py` into a
   dependency-light shared module; autoplay currently imports the window
   module just to obtain it. This directly supports finding 12's correction.
6. **Unify NVIDIA activation, validation, and inference selection policy.**
   `infer.py` and `runtime_bootstrap.py` duplicate DLL discovery with different
   platform and handle-lifetime behavior. Source runtime validation runs a
   tiny model; managed-runtime validation checks provider registration and
   native library loading. Share the platform-aware activation and execution
   checks, including CUDA fallback. Reuse inference selection/ownership policy
   across streaming, offline CLI, and benchmarks, while retaining TensorRT
   process isolation and checking recovered workers (9 and 19).
7. **Make observed native playback state the common UI synchronization path.**
   Finding 8 shows pause intent diverging between input bindings and toolbar
   state. The same review should cover track choices: periodic observations
   update remembered descriptors, while dropdown publication mostly occurs
   at initial enumeration. Consolidate user-originated state updates while
   excluding internal loading holds. The track case remains a static review
   opportunity, not an additional reproduced defect.

## Original Linux validation and limitations

The full CPU/offscreen suite completed with **443 passed, 4 skipped** in
100.22 seconds, using:

```bash
QT_QPA_PLATFORM=offscreen RELAY_LOSSLESS_HEVC_PROFILE=x265-ultrafast \
  python -u -m pytest tests -q -o faulthandler_timeout=60
```

The successful run had localhost networking enabled. An initial sandboxed
run stalled in asynchronous executor shutdown and was stopped. Focused
reproductions supplemented the suite for the findings above.

Optional ONNX inference tests were unavailable because the host lacked
`onnxruntime` and `onnx`. Actual Windows binaries and NVIDIA execution were
not exercised. Windows autoplay was checked through Windows path semantics,
not native Windows playback. Native desktop reproductions used headless
libmpv, isolated settings, and temporary media/configuration.

No application code was changed during the review. Existing documented
native GPU crashes remain unverified; this report does not claim a cause or
fix for them.

## Windows/NVIDIA continuation — 2026-10-09

Application baseline remains `ae8dec4`; this continuation started from report
commit `3222b42` on the same audit branch. Only this document is changed.
Reproduction scripts, generated media, logs, XML results, and binary-build
evidence are retained locally under the ignored `build/audit-2026-10-09/`
directory; they are not committed artifacts or portable links in this report.

### Native Windows suite

The source environment was Python **3.14.6**, PyAV **18.0.0**, NumPy **2.5.1**,
PySide6 **6.11.1**, ONNX **1.22.0**, and ONNX Runtime GPU **1.28.0**, with the
repository's Windows libmpv DLL present. The GPU was an **RTX 5090**, driver
**616.56**, observed at 0–1% utilization before the bounded GPU checks.

The complete suite finished with **438 passed, 6 failed, 2 skipped** in
**200.18 seconds**, without overriding the default NVIDIA encoder profile:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONFAULTHANDLER = '1'
.\.venv-cuda\Scripts\python.exe -u -m pytest tests -vv -ra `
  -o faulthandler_timeout=60 `
  --junitxml=build/audit-2026-10-09/windows-suite.xml
```

The successful completion required ordinary local networking outside the
restricted tool sandbox. The initial sandboxed attempt stalled before its
first result and was interrupted; it is not counted as application evidence.
The completed run also reported a 60-second traceback during the tray theme
test; that test subsequently passed. Caught LuaJIT SEH `0xe24c4a02` diagnostics
appeared during native mpv tests and did not terminate the process.

| Failure | Classification and evidence |
| --- | --- |
| Desktop parity, `default-uplink` and `lavf-uplink` | Application finding **12**. Both timed out at `tests/test_desktop_parity_playback.py:105` awaiting next-file autoplay. Resume, pause, restart, delay/track preservation, seek, and reopen assertions had already passed. Both server-file variants passed. |
| Local proxy/navigation | Test portability: `tests/test_desktop_features_gui.py:328` compares Qt's forward-slash path with the equivalent native backslash path after successful parent navigation. Compare `Path` values. |
| Desktop argument parsing | Test portability: `tests/test_desktop_options.py:23` compares native `\tmp\keys.conf` with `/tmp/keys.conf`. Argument parsing produced the correct `Path`. |
| Two instance-lock PID assertions | Test portability: the virtualenv launcher PID differs from its Python child PID. The lock and CLI correctly name the child that owns the lock. Make the fixture publish its actual `os.getpid()` instead of asserting against `Popen.pid`. |

A separate native Windows filesystem reproduction of finding 12 advanced
`C:/.../01.mkv` to `C:\...\02.mkv`, then returned no next sibling. The lock
reproduction observed launcher PID 63972 and direct child PID 61132; the lock
recorded 61132, the CLI rejected a second instance with exit 1 and that same
PID, and reacquisition succeeded after the holder shut down.

The skips were the Linux D-Bus dependency and a symlink fixture requiring a
Windows privilege unavailable to the process. Optional CPU ONNX tests and
Windows registry/autostart tests executed here, closing those Linux coverage
gaps. The full suite is **not green**; four failed assertions need portable
test expectations, and the two autoplay failures require the application fix.

### Actual NVIDIA execution and relay checks

Isolated source-runtime probes successfully executed a tiny ONNX graph under
each explicitly requested **CPU, CUDA, and TensorRT** provider, all exit 0.
An initialized TensorRT worker with `tile_size=1024` also confirmed finding 4:
a 3840×2160 input was rejected with
`frame 3840x2160 exceeds worker input cap 2560x1440`. Working 4K **output**
does not establish support for a 4K **source**.

The real client core and server ran on a private loopback port pair, using
passthrough first and then the installed
`2x_AnimeJaNai_HD_V3Sharp1_Compact` model. Every collected epoch was demuxed
and decoded, compared with source timestamps at the fixture's millisecond
time base, and checked for exactly one initial discontinuity and terminal
EOS. Every session returned the teardown acknowledgement, emptied the server
session registry, and left no native teardown error.

| Case | Verified result |
| --- | --- |
| 320×180/30 fps uplink → 640×360, passthrough, all eight public quality tiers | 120 frames per tier with exact source PTS and decoded frame counts. Seven HEVC choices used `hevc_nvenc`; FFV1 used `ffv1`. |
| Same uplink, default lossless HEVC, post-EOS seeks to 2 s then 1 s | Complete new epochs of 60 and 90 frames, exact source PTS, fresh discontinuities and EOS. |
| 1920×1080/24 fps server-file → 3840×2160, passthrough lossless HEVC | All 120 frames decoded with exact source PTS. |
| Same server-file geometry, AnimeJaNai model and lossless HEVC | Confirmed `TensorrtExecutionProvider`, uint8-wrapped model, `hevc_nvenc`, and `nvenc-p4-low-delay`. Initial 120 frames and post-EOS seek epochs of 72 and 96 frames all decoded with exact source PTS. |

The model session took 56.63 seconds including initialization, collection,
three decoded epochs, and verification; its accumulated inference stage
averaged 28.7 ms/frame. This is a functional check, not a sustained playback
or throughput benchmark: the consumer drained as quickly as possible and
reported zero buffered media. It does not validate Wi-Fi capacity, audible
synchronization, visible OpenGL presentation, VFR timing (finding 3 remains
open), or long-duration NVIDIA stability. These bounded runs did not reproduce
the historically reported intermittent native GPU crash and establish no fix
for it.

The exact local commands for the additional hardware probes were:

```powershell
.\.venv-cuda\Scripts\python.exe -u build/audit-2026-10-09/gpu_acceptance.py
.\.venv-cuda\Scripts\python.exe -u build/audit-2026-10-09/provider_checks.py
```

### Frozen Windows executable checks

Both console and tray-GUI executables were freshly built from this checkout
using the release workflow's PyInstaller options and an isolated lightweight
environment. The build resolved Python **3.14.6**, PyInstaller **6.22.3**,
PySide6 Essentials **6.12.0**, PyAV **19.0.1**, NumPy **2.5.3**, and ONNX
**1.22.0**. It contained no ONNX Runtime or multi-GB NVIDIA extra.

All **10 smoke checks passed**: console `--help`, GUI `--check`, and each
executable's worker dispatch, packaged-ONNX import, installer-dispatch check,
and real installation of the small `humanfriendly==10.0` smoke dependency
into an isolated destination. These ran from a separate working directory.
They validate frozen imports and pip dispatch; they do not establish a fresh
managed NVIDIA installation or GPU playback inside a frozen executable.

The first GUI bundle failed to import Qt with "The specified procedure could
not be found." This was reproduced outside the sandbox and traced to build
environment contamination: PyInstaller collected Codex's unrelated Poppler
`icuuc.dll`, whose version-suffixed exports did not satisfy Qt's 20 unversioned
ICU imports. Rebuilding with PATH limited to the audit virtualenv, Python,
and Windows directories resolved the failure without changing source. The
clean GUI `--check` then exited 0 in 1.56 seconds. This is an environment
qualification, not an additional repository defect or an unverified pass.

Build commands, dependency versions, executable SHA-256 values, and smoke
outputs are retained in `frozen-build-provenance-clean.json`,
`frozen-smoke-offline-clean.json`, and `frozen-smoke-network-clean.json` under
the local audit directory. The clean bundles are in `frozen-dist-clean/`.
An archive extraction/relocation round trip and visible tray interaction
were not part of these checks.

### Reproduction evidence for the new findings

All five additions have focused reproductions beyond the existing suite.
The generation races and provider-recovery failure use controlled scheduling
or controlled child processes; the color, audio-tail, and tiling fixtures run
real demux/encode or CPU ONNX paths. They do not depend on GPU contention.

| Finding | Local reproduction under `build/audit-2026-10-09/` |
| --- | --- |
| 15, premature uplink EOS | `desktop/uplink_iterator_race.py` |
| 16, color metadata/pixel change | `server/probe_color.py` |
| 17, auxiliary tail buffering | `server/probe_aux_tail.py` |
| 18, unwritten tiled output | `runtime/repro_tiling.py`, `runtime/repro_tiling.json` |
| 19, recovered worker provider | `runtime/repro_restart_provider.py`, `runtime/repro_restart_provider.json` |

No fixes are implemented by this continuation. Each new finding remains open.
