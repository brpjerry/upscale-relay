# NVENC session teardown and encoder admission

Status: proposed

## Problem

After several short desktop playback sessions, the server can reject a new
session during `open_session` with:

```text
pipeline_error: no encoder available for tier 'hevc-qp6'
(tried: ['hevc_nvenc'])
```

This was reproduced against the Windows server with the rendered Linux client,
the `passthrough` model, an ordinary server-library file, and `hevc-qp6`. The
failure arrived 270 ms after the open request, before media playback began.
`/status` showed no active sessions afterward.

The same server had successfully opened several `hevc-qp18` sessions shortly
before the failure. All `hevc-qp*` tiers use the same `hevc_nvenc` encoder and
differ only in encoder options, so this is not evidence that QP 6 itself is
unsupported. It means the server's per-session NVENC availability probe could
no longer open a hardware encoder context.

## Current teardown defects

There are three ordering problems in the current implementation.

### The close acknowledgement is premature

`RelayServer.handle_control()` sends the `closed` control message as soon as it
receives `teardown`. Actual cleanup occurs later in the handler's `finally`
block. A client therefore cannot interpret `closed` as “native resources are
released.”

`RelayClient.teardown()` does not wait for that acknowledgement anyway. It sends
`teardown`, sleeps for 100 ms, and closes the connection. The desktop client can
then reconnect and request another session while the previous encoder still
exists.

### `Pipeline.close()` does not wait for its owners

The decode, inference, and finish stages own native PyAV/FFmpeg state. In
particular, only the finish thread may touch the output container and NVENC
stream. `Pipeline.close()` currently sets `_closed`, wakes the input queue, and
returns without joining those threads. The finish thread closes `_mux` later.
Consequently, a session can disappear from `/status` while its old encoder is
still being drained or destroyed.

`Pipeline.close()` also closes the upscaler from the caller thread before the
inference thread is known to have stopped. That is the same class of ownership
race the project already avoids for PyAV containers.

### Encoder probing temporarily consumes another NVENC context

`select_encoder()` calls `probe_encoder()` for every new session. The probe
opens a real `hevc_nvenc` `CodecContext`; the pipeline then opens the actual
encoder. PyAV exposes no explicit `CodecContext.close()`, so the probe relies on
Python reference destruction. This temporarily raises the number of live NVENC
contexts and its exception is discarded, leaving only the generic “no encoder
available” message.

The probe may amplify the teardown race. It must have an explicit, observable
lifetime even though releasing its final Python reference is the only PyAV
close operation available.

## Required behavior

The teardown contract should be:

1. The client sends `teardown`.
2. The server stops source production and marks the session closed.
3. Every pipeline worker exits.
4. The finish worker closes the Matroska container and releases its stream.
5. The inference worker has stopped before its upscaler is closed.
6. The server removes the session and sends `closed`.
7. Only then may the desktop client reconnect and open a replacement session.

The `closed` message becomes a resource-release barrier, not merely receipt of
the teardown command.

## Implementation

### 1. Make `Pipeline.close()` synchronous and idempotent

Add a close lock and completion event to `relay_server/pipeline.py`. Exactly one
caller performs shutdown; concurrent callers wait for the same completion.

The owning caller should:

1. Set `_closed` and `_flush_pending` so normal work and watermark waits stop.
2. Drain stale input and place the input sentinel.
3. Join the decode, inference, and finish threads against one bounded deadline.
4. Let each worker's existing `_guard()` propagate shutdown downstream.
5. Let the finish thread close `_mux` and `_aux_template_container`; never close
   either object from the caller while that thread may still be using it.
6. Clear `_enc_stream` in the finish-thread cleanup after `_mux.close()`. A
   retained PyAV stream can retain its codec context even after the container
   reference is cleared.
7. Only after the inference thread exits, close `self.upscaler` and clear the
   reference.
8. Clear decoder and reformatter references after their owning threads exit.
9. Set the completion event in a `finally` block.

Use one overall timeout rather than a full timeout per thread. If a native call
does not return before the deadline, log the names of the surviving threads and
raise a teardown error. Do not race those threads by closing their native
objects from another thread. A timed-out shutdown means the server may require
a restart and must not claim that cleanup completed successfully.

Record close duration, tier, encoder name, and surviving thread names. This is
needed to distinguish ordinary delayed destruction from a native encoder hang.

### 2. Await pipeline shutdown outside the asyncio thread

Change `Session.close()` in `relay_server/session.py` to detach the pipeline
reference and run its blocking close operation with `asyncio.to_thread()`.
Keep source demux closure serialized as it is today.

Account for an `open_session` task that is still constructing a pipeline. A
teardown must not acknowledge completion while `_open_task` can publish a late
pipeline. The existing `_open_and_reap()` late-arrival cleanup should remain,
but the teardown path must wait until that cleanup has completed before sending
`closed`. Do not cancel a `to_thread()` pipeline build and assume it stopped;
the worker continues after asyncio cancellation.

`Session.close()` remains idempotent so the control handler, pipeline error
path, and connection-loss path can converge on it safely.

### 3. Send `closed` after cleanup

Restructure `RelayServer.handle_control()` in `relay_server/server.py` so the
`teardown` branch records that an acknowledgement was requested and exits the
receive loop. Its `finally` block should then:

1. Log the final session statistics while they are still available.
2. Await `session.close()`.
3. Remove all three session dictionary keys.
4. Send `closed` only if cleanup completed and the WebSocket is still writable.

If cleanup times out, log a restart-required error and close the control socket
without a successful `closed` acknowledgement. Sending an acknowledgement in
that case would recreate the original race.

### 4. Make the client wait for the barrier

Replace the fixed 100 ms sleep in `RelayClient.teardown()` with a pending
request for the `closed` response. Use a bounded timeout long enough for normal
encoder drain and destruction. Older servers already send `closed`, so this is
wire-compatible; the acknowledgement simply has stronger semantics on updated
servers.

On timeout or connection loss, continue local socket/task cleanup, but surface a
warning that the server did not confirm resource release. The desktop client
must not automatically reconnect and open a replacement session after such a
timeout without making the risk visible.

The settings-change path in `desktop_client/main_window.py` already calls
`_teardown_session()` before reopening. Once `RelayClient.teardown()` waits for
the barrier, model/tier/fit/resize changes inherit the correct ordering without
another arbitrary delay.

### 5. Make encoder probe failures diagnostic

Preserve the actual exception raised by `ctx.open()` in
`upscale_cli/encode.py`. The server error and log should distinguish at least:

- NVENC session/resource exhaustion;
- unsupported options;
- missing encoder/driver;
- device or driver failure.

Drop the probe context reference in a `finally` block immediately after the
probe. Do not call a nonexistent `CodecContext.close()`.

As a follow-up, avoid probing NVENC for every session. Cache static codec/option
support after one successful server-startup probe, then let actual pipeline
creation report transient admission failures. If concurrent clients are
supported, guard pipeline creation and teardown with an encoder admission
manager so probing, destruction, and new allocation cannot race. This manager
must not impose a one-session limit unless that is an explicit server policy.

## Regression coverage

Add deterministic tests using fakes rather than requiring NVENC:

- `Pipeline.close()` does not return until the finish worker has closed its
  fake mux and cleared its fake encoder stream.
- The upscaler is not closed while the fake inference worker is active.
- Two concurrent `close()` calls perform native cleanup once and both return
  after completion.
- The server emits no `closed` message while a fake pipeline close is blocked;
  it emits `closed` immediately after the close barrier is released.
- The client waits for a deliberately delayed `closed` response instead of
  sleeping for 100 ms.
- A late pipeline produced by an in-progress open is reaped before teardown is
  acknowledged.
- A close timeout reports the surviving stage and does not send a successful
  acknowledgement.
- Encoder-probe errors retain the original PyAV/FFmpeg reason.

Do not run a local server for verification. Local checks are limited to static
compilation and deterministic unit-level logic if permitted; playback and
encoder validation use the existing Windows server.

## Live-server verification

After deploying the fix and restarting the Windows server:

1. Confirm a baseline server-library playback with `passthrough` and
   `hevc-qp6`.
2. Stop it and wait for the new `closed` barrier; verify `/status` has no
   session.
3. Run at least 20 immediate open/play/teardown cycles with no artificial sleep,
   alternating `hevc-qp6` and `hevc-qp18` and at least two media files.
4. Require each cycle to produce advancing video before teardown, so the actual
   encoder—not merely its probe—was opened.
5. Repeat rapid tier changes through the rendered desktop UI, since that path
   tears down and reopens automatically.
6. Confirm every close log reports all pipeline workers stopped and the encoder
   stream released before the next session begins.
7. Confirm no cycle produces `no encoder available`, no session remains in
   `/status`, and a final fresh `hevc-qp6` playback succeeds.
8. If multiple simultaneous clients are supported, separately verify the
   intended concurrency limit and a clean recovery after one client exits.

Start with `passthrough` throughout. Only repeat with an ONNX model after the
encoder lifecycle is proven, so inference load cannot confound NVENC admission.

## Acceptance criteria

- `closed` is sent only after all native pipeline owners have stopped.
- Immediate session replacement cannot overlap the old and new NVENC contexts.
- Repeated playback and settings changes do not degrade encoder availability.
- A stuck native shutdown is reported explicitly instead of hidden behind an
  empty `/status` session list.
- Encoder-open failures include the underlying FFmpeg/NVENC error.

## Non-goals

This change does not address the muxed-aux initial interleaving delay, attachment
transport, seek epoch size, or mpv cache startup policy. Those affect playback
latency but did not cause the encoder-availability failure reproduced here.
