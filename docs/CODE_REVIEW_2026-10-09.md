# Repository code review — 2026-10-09

Baseline: `ae8dec4` (clean working tree when reviewed).
Work branch: `review/repository-audit-2026-10-09`.

The review covered the server, shared protocol/media layer, client core,
desktop, inference and offline tooling, runtime setup, packaging, CI, and
related tests. Three subagents reviewed the server/protocol, desktop, and
client core; the primary reviewer covered the remaining areas and
cross-checked findings.

There are **14 actionable findings**, ranked below. **P1 = high priority;
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

## Validation and limitations

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
