# Seek Latency — First Frame After an Epoch Seek

Far seeks take about twenty seconds to show a frame. Near seeks take under
three. This document records what was measured, explains which part of the
server produces the gap, and lists the candidate changes with their
trade-offs.

The measurements below were taken **from the Android client**. Server-side
timing has not been instrumented yet, so the attribution in
[Where the time goes](#where-the-time-goes) is inference from client-visible
behaviour plus a code read, not a profile. Step 1 of the roadmap fixes that
before anything else changes.

## Measured behaviour

Captured 2026-07-22 on a Samsung Tab S9 Ultra against a live server:
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

For comparison, a near seek (to 20.5 s, while the pipeline was already
producing nearby content) completed in **2.8 s** end to end.

Two conclusions follow directly:

- **`seek_ready` is not a readiness signal.** It is dispatched 45 ms after the
  request, before any media for the new epoch exists. `handle_seek`
  (`relay_server/session.py:296`) calls `start_server_source`, which only
  creates an asyncio task, and then sends the reply. The client cannot use it
  to predict anything.
- **The client is starved, not slow.** For roughly the first eight seconds
  after a far seek the downlink is nearly idle. The delay is server-side
  production latency, and it scales with seek distance.

## Where the time goes

On a seek, `Pipeline.flush` (`relay_server/pipeline.py:351`) sets
`_discard_until = target_pts`, and the source demuxer seeks **backward to the
nearest keyframe at or before the target**:

```python
# relay_media/demux.py:74
self._container.seek(from_pts, stream=self._stream, backward=True, any_frame=False)
```

Every frame decoded between that keyframe and `target_pts` is then thrown
away in `_put_decoded` (`relay_server/pipeline.py:470`):

```python
if frame.pts is not None and self._discard_until is not None:
    if frame.pts < self._discard_until:
        return                      # dropped before reaching _q_dec
    self._discard_until = None
```

The drop happens **before** the inference queue, so the discard window costs
decode time only — GPU inference is not wasted on it. That is the right
design. But it is also serial dead time: nothing can be emitted until the
decoder has walked the entire keyframe-to-target span, and on a long-GOP
animation encode that span can be many seconds of frames.

This is the leading explanation for the distance-dependent gap, and it is
consistent with the downlink being idle for the first eight seconds. It is
**not yet confirmed by a server-side measurement** — see step 1.

Two secondary contributors worth checking in the same pass:

- **Batching.** `_server_source_loop` accumulates 16 packets before handing
  work downstream (`relay_server/session.py:342`). Harmless in steady state;
  it adds avoidable latency to the very first post-seek batch.
- **Backpressure priming.** `handle_seek` calls `note_buffer_report(0)`, so
  the `HIGH_WATERMARK_MS = 10_000` gate should not engage immediately after a
  seek. Worth confirming it does not re-engage early from a stale report while
  the client still shows nothing.

## Roadmap

### 1. Instrument the seek path (do this first)

Nothing below should be attempted before the gap is attributed with real
numbers. `PipelineStats` already carries `stage_ms` and is surfaced through
`/status`; extend it with per-seek counters:

- wall time from `flush` to the first frame that survives `_discard_until`;
- number of frames discarded, and their decode time in aggregate;
- wall time from that first surviving frame to the first encoded packet
  handed to the downlink;
- the keyframe-to-target PTS distance actually seen.

Log one line per seek at info level. That single line will say whether the
discard window is 90% of the gap or 30% of it, and every choice below depends
on the answer.

### 2. Emit from the keyframe instead of discarding to the target

The largest available win, if step 1 confirms the discard window dominates.
Rather than dropping decoded frames until `target_pts`, upscale and send from
the keyframe onward and let the client land where the stream starts.

- **Cost:** the seek becomes keyframe-accurate. The player lands up to one GOP
  *before* the requested position instead of exactly on it.
- **Benefit:** the discard window disappears entirely. First frame arrives
  after one decode + infer + encode, independent of seek distance.
- **Note:** absolute Matroska PTS remain authoritative either way, so the
  client's position readout stays correct — it simply starts a little earlier
  than requested. This is how most streaming players behave, and it is
  probably the right default.
- A middle option: keep frame accuracy only when the keyframe is within a
  short threshold of the target (say under two seconds), and fall back to
  keyframe-accurate beyond that.

### 3. Speed up the discard window if it must stay

If frame-accurate seeking is a hard requirement, the discard span can be
decoded faster than it is played:

- The discarded frames are never displayed, but they **are** reference frames
  for the target, so decoding cannot be skipped and loop-filter shortcuts will
  introduce reconstruction drift. Measure any quality impact before adopting.
- Decoding the discard window on a second, throwaway decoder in parallel with
  the still-draining previous epoch is possible but adds real complexity.

Prefer step 2 unless frame accuracy is genuinely required.

### 4. Make `seek_ready` mean something, or add a progress message

Independent of the above, the client currently has no way to distinguish "the
server is working" from "the pipeline is wedged" — it shows a spinner over a
buffer readout that reads as full. Either:

- delay `seek_ready` until the first encoded packet of the new epoch is
  queued, so it is a genuine readiness signal; or
- keep the fast ack and add a `seek_progress` message carrying discarded-frame
  count and current PTS versus target.

The second is preferable: it keeps the existing epoch handshake intact and
gives the client something to render a real progress indicator from. Adding a
message type needs a `docs/PROTOCOL.md` update and a protocol-version bump.

## Client-side status

A related client bug was fixed separately in the Android repo: mpv stops
emitting `demuxer-cache-duration` while a new file loads, and the engine kept
the previous epoch's value, so during the entire 20 s gap the client reported
roughly 9.4 s of cache when the new stream had buffered nothing. That stale
number also went to the server in `buffer_report`, which means **the server's
backpressure logic may have been reading a phantom client buffer immediately
after every seek**. Worth re-checking the watermark behaviour against a
correctly-reporting client once that fix ships.
