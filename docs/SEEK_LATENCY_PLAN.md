# Seek Latency — First Frame After an Epoch Seek

Far seeks took about twenty seconds to show a frame on the Android client.
This document records the original field measurement, the server-side profile
that followed it, and what changed as a result.

**Status:** step 1 (instrumentation) is done and the server-side gap is now
attributed with real numbers. Step 2 (emit from the keyframe) is implemented
behind `--seek-discard-max-s`, off by default. Step 4 (`seek_progress`) is
done. Step 3 was measured and rejected. What remains unexplained is
client-side, not server-side — see [What is still open](#what-is-still-open).

## Measured behaviour (client, 2026-07-22)

Captured on a Samsung Tab S9 Ultra against a live server:
`2x_AnimeJaNai_HD_V3Sharp1_Compact`, tier `hevc-qp4`, output 2960x1848,
source `[SubsPlease] Kimi ga Shinu made Koi wo Shitai - 02 (1080p)`.

A far seek from ~285 s to 1197.8 s, taken from mpv's own event stream:

| Elapsed | Event |
|---|---|
| 0 ms | client sends `seek` |
| **45 ms** | **server replies `seek_ready`** |
| 203 ms | mpv `start-file` on the new epoch's loopback |
| 2.10 s | mpv `file-loaded` |
| 2.25 s | hardware decoder up, `video-reconfig` |
| **22.31 s** | **mpv `playback-restart` — first frame displayed** |

Between `video-reconfig` and `playback-restart` there is a **20.06 s gap with
no mpv activity at all**. Client-side buffer telemetry across that gap:

```
+8 s    queue=5 MB      rx=64 Mbps    (almost nothing has arrived)
+18 s   queue=217 MB    rx=66 Mbps    (flood)
+22 s   playback-restart
```

One conclusion holds up unchanged: **`seek_ready` is not a readiness signal.**
It is dispatched before any media for the new epoch exists — `handle_seek`
calls `start_server_source`, which only creates an asyncio task, and then
sends the reply. Measured in-process it acks in **4–6 ms**.

## Server-side profile (2026-07-25)

`PipelineStats.last_seek` now records one `SeekTrace` per seek
(`relay_server/pipeline.py`), logged at info level and surfaced as
`/status → sessions[].pipeline.last_seek`. Numbers below are from the real
server run in-process against a synthetic long-GOP clip: 1080p, 24 fps, x264
defaults (keyint 250 ⇒ ~10.3 s GOP), passthrough model, `lossless-hevc`
(hevc_nvenc), on the RTX 5090 box.

| Seek target | Keyframe gap | Frames discarded | Discard decode | First frame | Client's first bytes |
|---|---|---|---|---|---|
| 26700 ms | 10.33 s | 248 | 3634 ms | +3648 ms | 3885 ms |
| 37100 ms | 10.35 s | 249 | 3609 ms | +3627 ms | 3851 ms |
| **26800 ms** | **0.05 s** | **2** | **103 ms** | **+116 ms** | **337 ms** |
| 26740 ms | 10.37 s | 249 | 3729 ms | +3742 ms | 3966 ms |

The discard window is **94% of the server-side wait** to the client's first
bytes, and essentially 100% of the wait to the first surviving frame. Step 2
is therefore the right lever, and step 1's question is answered.

Two things this profile corrects in the original write-up:

- **The gap does not scale with seek distance.** It scales with the
  keyframe-to-target span, which is bounded by one GOP. Rows 3 and 4 are the
  proof: targets 60 ms apart, straddling a keyframe, same seek distance —
  **337 ms vs 3966 ms**. The "near seek completed in 2.8 s" comparison in the
  field capture was confounded; that seek landed on content the pipeline was
  already producing.
- **The source seek itself is free.** `VideoTrack.packets(from_pts)` →
  `container.seek(..., backward=True)` measures **0.3–0.5 ms**. So is the
  16-packet batch in `_server_source_loop`, which the original write-up
  flagged as a secondary contributor: `flush_seen_ms` (flush call to the
  decode stage dequeuing it) is 8–30 ms, inside the noise. Neither was
  changed.

The mechanism is unchanged from the original code read: `Pipeline.flush` sets
`_discard_until = target_pts`, the demuxer seeks backward to the nearest
keyframe, and `_put_decoded` drops every frame before the target. The drop
happens *before* the inference queue, so no GPU work is wasted — that part of
the design was and is right. It is just serial dead time: 250 frames × ~14.5
ms of single-threaded 1080p decode.

## Roadmap

### 1. Instrument the seek path — **done**

`SeekTrace` carries, per seek: the keyframe-to-target span the source seek
actually produced, frames discarded and their aggregate decode time, and the
wall time from `flush` to the decode stage, to the first surviving frame, and
to the first encoded packet handed to the downlink. One info line per seek:

```
relay.pipeline INFO seek epoch=1 target_pts=26700 mode=accurate: flush_seen=30ms
  keyframe_gap=10.32s discarded=248 frames (3603ms decode) first_frame=+3636ms
  first_packet=+3773ms
```

### 2. Emit from the keyframe instead of discarding to the target — **implemented, opt-in**

`relay-server --seek-discard-max-s SECONDS` takes the middle option: keep
frame accuracy when the keyframe is within `SECONDS` of the target, emit from
the keyframe beyond it. Measured on the same clip with `--seek-discard-max-s 2`:

| Seek target | Mode | First bytes (default) | First bytes (threshold 2 s) |
|---|---|---|---|
| 26700 ms | keyframe | 3885 ms | **349 ms** |
| 37100 ms | keyframe | 3851 ms | **314 ms** |
| 26800 ms | accurate | 337 ms | 371 ms |
| 26740 ms | keyframe | 3966 ms | **326 ms** |

A ~12x improvement on long-GOP seeks, with near-keyframe seeks staying
frame-accurate. The cost is that the player lands up to one GOP *before* the
requested position. Absolute Matroska PTS remain authoritative, so the
position readout stays correct — playback simply starts earlier.

**It is off by default on purpose.** It changes user-visible seek semantics to
buy ~3.5 s, and the field gap it was proposed to fix is 20 s. Turn it on
knowingly, not as a fix for something that was never measured.

### 3. Speed up the discard window if it must stay — **measured, rejected**

The obvious safe trick is `CodecContext.skip_frame`. Non-reference B-frames
are by definition not used as references, so skipping them during the discard
window introduces no reconstruction drift. Measured over a full 250-frame GOP:

| `skip_frame` | Frames decoded | Discard time | Note |
|---|---|---|---|
| `DEFAULT` | 249 | 3595 ms | — |
| `NONREF` | 201 | 3184 ms | −11%; drops frames after the target too |
| `BIDIR` | 177 | 2911 ms | −19%; skips reference B-frames ⇒ drift |

`NONREF` buys 11% because non-reference B-frames are the cheapest frames in
the GOP — the expensive ones are exactly the ones that must be decoded. It
also suppresses output for frames past the target, so it would need a margin
and a mode switch mid-window. Not worth the complexity for 11%. `BIDIR` is
not safe. Parallel decode on a throwaway decoder remains theoretically
available and remains too complex to justify. Prefer step 2.

Note that the discard window is single-threaded decode by necessity:
`RELAY_DECODE_THREADS` is opt-in because frame-threaded decode crashes this
stack (see CLAUDE.md), so the usual "just thread it" answer is closed here.

### 4. Make `seek_ready` mean something, or add a progress message — **done**

Took the second option, as recommended: the fast ack is unchanged and the
server now sends `seek_progress` (`docs/PROTOCOL.md` §2) roughly every 500 ms
while a seek has produced no downlink bytes, carrying `frames_discarded`,
`keyframe_pts` and `elapsed_s`. Seeks that produce media promptly send none —
in the table above, the 337 ms seek sent zero ticks and the 3.9 s ones sent
six or seven. The ticker gives up after 60 s with a warning, so a genuinely
wedged pipeline shows up in the log instead of narrating forever.

No protocol-version bump. The server's `hello` check is strict equality, so a
bump locks out every existing client; `seek_progress` is additive and
server→client, and clients that do not know the type already skip it. This is
the same convention `session_progress` was added under.

## What was still open — resolved client-side (2026-07-26)

The remaining ~16 s was the Android client waiting for **external audio**, and
neither candidate below was the cause. Both are struck out; the measurement is
in the Android repo (`docs/ANDROID_CLIENT.md`, Phase 2). In short:

mpv positions an external demuxer at the current playback time *when the track
is selected*, and during `loadfile` that time is still zero. The client passed
the original file as `audio-file` / `sub-files-append` on the load, so those
demuxers opened at the start of the original file while the relay stream began
at the seek target. mpv's `--start` seek would normally reposition them, but
the loopback stream is a live one-shot socket ("Cannot seek in this stream"),
so the seek was rejected and mpv reached the epoch by *decoding forward*
through everything before it. Hence a stall proportional to the seek target —
and hence a stall that server-side profiling could never see.

Measured on a Tab S9 Ultra against this server, seeking within a 23:40 file:

| Seek target | Video frame shown | `audio ready` (playback-restart) |
|---|---|---|
| 305 s (before) | +2.9 s | +12.8 s |
| 678 s (before) | +3.3 s | +19.6 s |
| any (after) | +1.0 s | **+1.5 s** |

The fix attaches the original media with `audio-add` / `sub-add` after
playback has started, when the position is known; each demuxer then issues one
HTTP range seek. A/V error stayed at ~10–25 µs with zero decoder drops across
an eight-action seek storm.

~~**The client's queue → mpv sender is the bottleneck.**~~ Not it. The
loopback sender was never behind; the 217 MB queue was the symptom of mpv not
draining while it waited on audio, not the cause.

~~**Backpressure primed from a phantom buffer.**~~ Not it either. The stale
`demuxer-cache-duration` was a real bug and was fixed separately, but the
watermark never engaged here.

Reproduce any of this with `/status → sessions[].pipeline.last_seek`, or the
`relay.pipeline` seek log line, on a real client seek. For the client half,
`msg-level=all=v` in the Android player engine prints `refresh seek to <pts>`
per external demuxer, which is what made this visible.
