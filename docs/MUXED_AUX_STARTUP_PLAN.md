# Muxed auxiliary-track startup and attachment caching

Status: immediate fixes and negotiated attachment cache implemented;
Linux/Windows live timing and cache tuning pending

Implemented on 2026-08-18: a 100 ms `max_interleave_delta`, immediate delivery
of each epoch's discontinuity packet, a conservative 0.5 s mpv
`cache-pause-wait`, and an additive cached-attachment protocol. The server
publishes sanitized SHA-256 font manifests through a session bearer token;
the Qt client uses a bounded verified content-addressed cache and per-session
libass font directory, while old clients keep embedded attachments. The live
measurement matrix below remains the authority for choosing a different
interleave/cache reserve.

## Scope

This plan addresses the Linux client's 3–5 second delay between opening a
server-library file and advancing playback when the server confirms
`aux_tracks: "muxed"`.

It is separate from NVENC session teardown. Measurements in this document used
the `passthrough` model so inference performance could not affect the result.

## Measured startup path

The rendered Linux client was instrumented at the relay framing reads, the
localhost mpv feed, and mpv position changes. Each first relay payload was also
checked for the Matroska `Cluster` element.

| File | Header/attachments | Header transfer | Header → first cluster | First position | Advancing |
|---|---:|---:|---:|---:|---:|
| Blu-ray remux | 35.3 MiB | 0.359 s | 2.596 s | 4.536 s | 5.649 s |
| Tenkosaki 05 | 6.5 MiB | 0.081 s | 2.625 s | 3.512 s | 4.688 s |
| Chainsmoker Cat E05 | 6 KiB | effectively zero | 0.957 s | 1.656 s | 2.955 s |

The 35.3 MiB transfer is healthy and is not the source of the multi-second
wait. Its first payload contains the Matroska header and attachments but no
`Cluster`. The slow interval is the absence of a playable cluster after that
header.

For the same remux in `external` auxiliary mode, the video-only header was
followed immediately by the first cluster. That comparison isolates the
dominant delay to server-side mux interleaving of the original audio/subtitle
streams. It is file-dependent because the initial packet timestamps and
sparsity differ between tracks.

The remaining delay has two client-side components:

- `_DOWNLINK_BATCH = 8` withholds the first completed epoch payload until eight
  relay packets have arrived, adding about 240 ms in the measured starts.
- mpv's cache-pause policy waits roughly another 1.1–1.7 seconds before the
  position begins advancing.

Session construction adds approximately 0.4–0.95 seconds depending on the
file. Attachment validation and header construction contribute to that phase,
but they are not the dominant 3–5 second stall.

## Current attachment behavior

Attachments are **not cached by the muxed protocol**.

`Pipeline._open_mux()` creates a fresh Matroska container for the initial epoch
and every seek epoch. Every invocation walks the source attachment streams and
calls `add_attachment()`. Therefore:

1. All attachment bytes are embedded in the first relay payload of epoch 0.
2. A seek abandons that container and creates a new one.
3. The same attachment bytes are embedded and transferred again.
4. mpv receives a new `loadfile` stream and parses those attachments again.

mpv may retain internal font-library state opportunistically, but the relay
does not rely on or observe such a cache. The bytes are unquestionably sent
again after every seek. On the 35.3 MiB remux, every seek currently retransmits
another 35.3 MiB header.

The working tree currently contains a 4 MiB attachment threshold that confirms
`external` mode for larger files. That avoids repeated muxed headers by falling
back to `/media`; it is not attachment caching and it does not fix the measured
interleaver stall for smaller files. Because the 35.3 MiB transfer took only
359 ms, the threshold must not be presented as the fix for the 3–5 second
startup delay.

## Immediate implementation

### 1. Bound FFmpeg's interleave delay

`relay_server/pipeline.py` already gives the Matroska muxer
`cluster_time_limit=100`. That controls cluster duration after packets are
eligible for output; it does not prevent libavformat's multi-stream interleaver
from retaining packets while waiting for sparse streams.

Add and live-test a small, nonzero `max_interleave_delta` on the output format
context. FFmpeg expresses this value in microseconds. Start with 100 ms and test
nearby values rather than setting it to zero: zero permits indefinite waiting
for every stream to have a packet and can make sparse-subtitle behavior worse.

Also test `flush_packets=1` independently. Do not combine knobs in the first
measurement, because the result must show which option releases the initial
cluster. Keep `cluster_time_limit=100` unless evidence shows it conflicts.

For each candidate configuration, record:

- time the header payload is emitted;
- time the first `Cluster` payload is emitted;
- first cluster's earliest audio, subtitle, and video timestamps;
- whether all advertised tracks remain selectable;
- A/V synchronization at startup and after a distant seek;
- subtitle events that begin at or near timestamp zero.

A small interleave bound must not reorder DTS, drop sparse subtitle tracks, or
emit audio so far ahead of video that mpv rejects the stream.

### 2. Forward the first packet of every epoch immediately

Keep `_DOWNLINK_BATCH = 8` for steady-state throughput under qasync, but bypass
the batch for the first packet of each epoch. The initial packet and every
post-seek packet already carry `FLAG_DISCONTINUITY`, so the blocking downlink
receiver can publish that packet immediately and then resume batches of eight.

This lets mpv parse stream metadata and attachments while the first cluster is
being produced. It also removes the measured 240 ms delay after the first
cluster becomes available.

The bypass must preserve these invariants:

- stale packets from older epochs are still discarded before publication;
- a seek's discontinuity packet cannot remain behind an old batch;
- only one event-loop wakeup is added per epoch, not one per media packet;
- the downlink thread never calls asyncio queue methods unsafely.

### 3. Reduce mpv's initial cache wait carefully

Keep mpv's cache enabled and keep cache-pause recovery; both are required for a
variable-rate lossless stream. Measure `paused-for-cache`, cache duration, and
the time from first decodable cluster to advancing position.

Tune `cache-pause-wait` downward in steps after the server emits clusters
promptly. Do not disable cache pause merely to improve the startup number. The
chosen value must survive ordinary Wi-Fi jitter without introducing immediate
start/stop playback. Apply the same setting to rendered and headless modes.

The target is the smallest startup reserve that remains stable on the intended
client network. Compare against the Android client's reserve and startup order
on the same file, tier, and server.

## Attachment caching design

The final muxed protocol should carry audio and subtitle packets in each epoch
but should not embed immutable attachments in every epoch container.

### Server update is required

This cannot be implemented as a client-only cache. In the current protocol the
server gives the client no attachment manifest or stable attachment identity;
it writes opaque attachment bytes directly into each new Matroska header. The
client only sees those bytes as part of the live container consumed by mpv and
cannot safely suppress their retransmission on the next epoch.

The server must be updated to:

1. Enumerate attachments for a server-library file and calculate a stable
   content hash for each object.
2. Advertise an attachment-cache capability during `hello`.
3. Return an attachment manifest and short-lived authenticated download token
   in `session_opened` when cached attachments are negotiated.
4. Serve only the manifest's immutable attachment objects through a dedicated
   endpoint or framed media direction.
5. Construct cached-mode Matroska epochs with audio/subtitle streams but without
   repeating attachment bodies.
6. Continue embedding attachments for older clients that did not negotiate
   cached mode.

The protocol should use explicit negotiation rather than changing the meaning
of `aux_tracks: "muxed"` underneath existing clients. One compatible extension
is:

- `capabilities.attachment_cache = 1` advertises manifest/cache support.
- `open_session.aux_attachments = "cached"` opts in; absent means
  `"embedded"`.
- `session_opened.aux_attachments` reports the effective mode.
- `session_opened.attachment_manifest` contains only sanitized metadata and
  content hashes.
- `session_opened.attachment_token` authorizes reads for that manifest and
  expires with the session or after a short fixed window.

The server must confirm `"cached"` only when it can provide every attachment
needed for equivalent subtitle rendering. Until it does, it must confirm
`"embedded"` or explicitly fall back; it must never omit attachments merely
because a new client requested caching.

Likely server implementation points are:

- `relay_media/demux.py`: expose immutable attachment metadata and hashes from
  `AuxiliaryTrack` without sharing its PyAV container across threads.
- `relay_server/session.py`: negotiate the effective attachment mode and place
  the manifest/token in `session_opened`.
- `relay_server/server.py`: advertise the capability and provide the
  authenticated attachment resource.
- `relay_server/pipeline.py`: parameterize `_open_mux()` so negotiated cached
  epochs copy auxiliary tracks and metadata but skip `add_attachment()`.
- `relay_protocol/` and `docs/PROTOCOL.md`: define optional request/response
  fields, authentication, size bounds, and backward-compatible defaults.

Hashing and extraction should not add a new multi-second session-open cost.
Cache the server-side manifest by resolved file identity such as path, size, and
nanosecond modification time, then verify attachment content by hash when it is
materialized. Do not expose the resolved library path to the client.

### Manifest and content-addressed cache

Add a server capability for an attachment manifest. For a server-library
session, report each attachment's sanitized filename, MIME type, byte size, and
cryptographic content hash. The client maintains a bounded persistent cache by
hash and fetches only missing objects through a dedicated authenticated
attachment resource.

This must not reuse the old full-file `/media` fallback. The attachment
resource exposes only declared immutable attachment objects associated with the
session or library item. It may use authenticated HTTP or a distinct framed
media direction, but it needs:

- existing session-token authentication or a short-lived attachment token;
- library-root containment;
- filename sanitization and no client-selected server paths;
- total-size and per-object bounds;
- hash verification before publishing a cache entry;
- atomic cache writes and cleanup of incomplete downloads;
- an eviction limit configurable independently of mpv's media cache.

For HTTP, prefer an `Authorization` header over a query-string token so secrets
do not appear in normal request logs. The server must reject hashes not present
in the authorized session manifest even if that hash exists in its internal
cache.

### mpv font registration

Materialize the session's cached font objects into a controlled font directory
and configure mpv's subtitle font directory before loading the epoch stream.
Verify the exact mpv/libass behavior with attached ASS fonts, including fonts
whose Matroska filenames differ from their internal family names.

Only after the font directory is ready should the server's first playback be
released when exact subtitle rendering is required. Missing fonts can download
in parallel with session construction; on the measured LAN the first 35.3 MiB
transfer should still complete in well under one second. Subsequent sessions
with identical hashes require no transfer.

Once this path is proven, omit font attachments from every epoch's Matroska
header. Audio and subtitle streams remain muxed, and seeks create small
self-describing containers without duplicating font data.

Non-font attachments need an explicit policy. Do not silently claim full
attachment parity until supported attachment MIME types and their consumers
have been identified. Unknown attachment metadata should remain visible in the
manifest even if the desktop client cannot use it.

### Cache invalidation

Content hashes make invalidation independent of filenames and modification
times. A replaced font produces a new key; unchanged fonts shared by multiple
files are downloaded once. Cache entries are immutable and may be removed only
by bounded eviction when they are not in use by an active mpv session.

The cache is an optimization, not authority: hash or size mismatches fail the
attachment fetch and must never expose a partial file to libass.

## Regression coverage

Add deterministic coverage for:

- the first packet of epoch 0 bypassing the eight-packet batch;
- the first discontinuity packet after every seek bypassing the batch;
- steady-state packets retaining batch behavior;
- stale pre-seek batches being discarded;
- small interleave bounds producing prompt video clusters with sparse subtitle
  streams while preserving all audio/subtitle streams;
- A/V PTS equivalence before and after a seek;
- attachment manifests rejecting unsafe filenames and incorrect hashes;
- a cache hit causing zero attachment-body transfer;
- a seek causing zero attachment-body transfer;
- a cache miss downloading once and becoming a verified cache hit;
- two files sharing a font hash using one cached object;
- interrupted downloads never becoming valid cache entries.

Do not run a local relay server for validation. Static checks and deterministic
logic may be inspected locally if permitted, but playback, FFmpeg mux behavior,
and mpv behavior must be tested against the Windows server.

## Live-server verification

Use the rendered Linux UI and the `passthrough` model first.

1. Measure the three files in the table with the baseline server.
2. Deploy only the interleave-bound change and repeat the framing trace.
3. Require the first cluster within 250 ms of the header for all three files.
4. Deploy the first-epoch packet bypass and require publication within 50 ms of
   socket receipt.
5. Tune mpv cache wait and compare time-to-advancing with Android on the same
   network and tier.
6. Verify active playback, pause, audio selection, subtitle selection, and A/V
   sync.
7. Seek near and far while playing and while paused; require immediate old
   epoch discard and automatic resume matching the caller's pause intent.
8. With attachment caching enabled, clear the cache and verify one font fetch,
   then perform several seeks and reopen the file. Require zero additional font
   bytes after the initial verified download.
9. Reopen a second file sharing fonts and verify content-hash cache hits.

After passthrough behavior is stable, repeat with one real ONNX model while
checking `/status` pipeline FPS so GPU contention is not mistaken for buffering.

## Acceptance criteria

- The first playable cluster follows the header within 250 ms on representative
  muxed files, including sparse subtitles.
- The client forwards the first packet of each epoch immediately.
- Linux startup is within 20% of Android for the same file, tier, display, and
  network, or every remaining difference is measured and documented.
- Cache tuning does not introduce startup oscillation or ordinary Wi-Fi
  rebuffers.
- All original audio/subtitle tracks and ASS font rendering remain available.
- A seek transfers no attachment bodies once the session fonts are cached.
- Reopening unchanged media does not redownload cached fonts.

## Non-goals

This plan does not change model inference, encoder teardown, source decode
speed, or the absolute-PTS seek protocol. Those must remain independently
measurable.
