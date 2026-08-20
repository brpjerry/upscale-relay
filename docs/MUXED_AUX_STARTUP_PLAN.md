# Muxed auxiliary-track startup and attachment caching

Status: implemented and live-verified on Linux against the Windows server;
Android adoption is specified separately in the Android repository

Implemented on 2026-08-18: a 100 ms `max_interleave_delta`, immediate delivery
of each epoch's discontinuity packet, a conservative 0.5 s mpv
`cache-pause-wait`, and an additive cached-attachment protocol. The server
publishes sanitized SHA-256 font manifests through a session bearer token;
the Qt client uses a bounded verified content-addressed cache and per-session
libass font directory. Muxed-capable clients that omit cache negotiation keep
embedded attachments, while older clients retain the external `/media` path.
The live measurement matrix below remains the authority for choosing a
different interleave/cache reserve.

Live verification on 2026-08-19 confirmed a 3.2 KiB cached-mode header,
first position in 0.58–0.88 s, and advancing playback in 1.94–2.12 s on the
representative Tenkosaki file. A muxed auxiliary seek regression was then
fixed by anchoring the second demuxer on video cues: the same rendered seek
improved from 7.88 s to 0.86 s with sub-millisecond A/V error.

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

## Historical attachment behavior before the implemented cache

Before the cached-attachment extension, attachments were **not cached by the
muxed protocol**.

`Pipeline._open_mux()` creates a fresh Matroska container for the initial epoch
and every seek epoch. Every invocation walks the source attachment streams and
calls `add_attachment()`. Therefore:

1. All attachment bytes are embedded in the first relay payload of epoch 0.
2. A seek abandons that container and creates a new one.
3. The same attachment bytes are embedded and transferred again.
4. mpv receives a new `loadfile` stream and parses those attachments again.

mpv could retain internal font-library state opportunistically, but the relay
did not rely on or observe such a cache. The bytes were unquestionably sent
again after every seek. On the 35.3 MiB remux, every seek retransmitted another
35.3 MiB header.

Legacy embedded mode retains a 4 MiB attachment threshold that confirms
`external` mode for larger files. That avoids repeated muxed headers by falling
back to `/media`; it is not attachment caching and it does not fix the measured
interleaver stall for smaller files. Because the 35.3 MiB transfer took only
359 ms, the threshold must not be presented as the fix for the 3–5 second
startup delay.

## Implemented startup changes

### 1. Bound FFmpeg's interleave delay

`relay_server/pipeline.py` already gives the Matroska muxer
`cluster_time_limit=100`. That controls cluster duration after packets are
eligible for output; it does not prevent libavformat's multi-stream interleaver
from retaining packets while waiting for sparse streams.

The output format context now uses a 100 ms `max_interleave_delta`. FFmpeg
expresses this value in microseconds; zero was intentionally avoided because it
permits indefinite waiting for every stream and can make sparse subtitles
worse.

`cluster_time_limit=100` remains in place. The bounded interleaver, rather than
bulk attachment transfer, was the change that released the initial cluster.

The comparison recorded:

- time the header payload is emitted;
- time the first `Cluster` payload is emitted;
- first cluster's earliest audio, subtitle, and video timestamps;
- whether all advertised tracks remain selectable;
- A/V synchronization at startup and after a distant seek;
- subtitle events that begin at or near timestamp zero.

A small interleave bound must not reorder DTS, drop sparse subtitle tracks, or
emit audio so far ahead of video that mpv rejects the stream.

### 2. Forward the first packet of every epoch immediately

`_DOWNLINK_BATCH = 8` remains for steady-state throughput under qasync, while
the first packet of each epoch bypasses the batch. The initial packet and every
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

mpv's cache and cache-pause recovery remain enabled because both are required
for a variable-rate lossless stream. The implemented 0.5 s `cache-pause-wait`
applies to rendered and headless modes and passed the live startup/seek runs;
future tuning must retain ordinary Wi-Fi jitter coverage rather than optimize
only the startup number.

## Implemented attachment caching design

The muxed protocol carries audio and subtitle packets in each epoch without
embedding immutable attachments in every cached-mode epoch container.

### Server update — implemented

This required server support rather than a client-only cache. Before the
extension, the server gave the client no attachment manifest or stable
identity; it wrote opaque bodies into each new Matroska header, where a client
could not safely suppress their retransmission on the next epoch.

The implemented server:

1. Enumerates attachments for a server-library file and calculates a stable
   content hash for each object.
2. Advertises an attachment-cache capability during `hello`.
3. Returns an attachment manifest and short-lived authenticated download token
   in `session_opened` when cached attachments are negotiated.
4. Serves only the manifest's immutable attachment objects through an
   authenticated HTTP endpoint.
5. Constructs cached-mode Matroska epochs with audio/subtitle streams but without
   repeating attachment bodies.
6. Continues embedding attachments for older clients that did not negotiate
   cached mode.

The protocol uses explicit negotiation rather than changing the meaning
of `aux_tracks: "muxed"` underneath existing clients. One compatible extension
is implemented:

- `capabilities.attachment_cache = 1` advertises manifest/cache support.
- `open_session.aux_attachments = "cached"` opts in; absent means
  `"embedded"`.
- `session_opened.aux_attachments` reports the effective mode.
- `session_opened.attachment_manifest` contains only sanitized metadata and
  content hashes.
- `session_opened.attachment_token` authorizes reads for that manifest and
  expires when the session is torn down.

The server must confirm `"cached"` only when it can provide every attachment
needed for equivalent subtitle rendering. Until it does, it must confirm
`"embedded"` or explicitly fall back; it must never omit attachments merely
because a new client requested caching.

The server implementation points are:

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

Hashing and extraction do not add a new multi-second session-open cost. The
server caches attachment information by resolved path, size, and nanosecond
modification time, while clients verify materialized objects by hash. The
resolved library path is never exposed to the client.

### Manifest and content-addressed cache

The server advertises an attachment manifest capability. For a server-library
session, report each attachment's sanitized filename, MIME type, byte size, and
cryptographic content hash. The client maintains a bounded persistent cache by
hash and fetches only missing objects through a dedicated authenticated
attachment resource.

This does not reuse the old full-file `/media` fallback. The authenticated HTTP
resource exposes only declared immutable attachment objects associated with the
active session, with:

- existing session-token authentication or a short-lived attachment token;
- library-root containment;
- filename sanitization and no client-selected server paths;
- total-size and per-object bounds;
- hash verification before publishing a cache entry;
- atomic cache writes and cleanup of incomplete downloads;
- an eviction limit configurable independently of mpv's media cache.

HTTP uses an `Authorization` header rather than a query-string token so secrets
do not appear in normal request logs. The server rejects hashes not present
in the authorized session manifest even if that hash exists in its internal
cache.

### mpv font registration

The desktop client materializes the session's cached font objects into a
controlled font directory and configures mpv's subtitle font directory before
loading the epoch stream. Coverage includes fonts whose Matroska filenames
differ from their internal family names.

The font directory is ready before the first epoch load. On the measured LAN,
the initial 35.3 MiB transfer completes in well under one second; subsequent
sessions with identical hashes require no transfer.

In cached mode, font attachments are omitted from every epoch's Matroska
header. Audio and subtitle streams remain muxed, and seeks create small
self-describing containers without duplicating font data.

The current cache mode is deliberately font-only. The server confirms cached
mode only when every attachment is a recognized libass font type; otherwise it
keeps attachments embedded when the live-header bound permits, or confirms
external auxiliary media. This avoids claiming parity for attachment types the
desktop client cannot consume.

### Cache invalidation

Content hashes make invalidation independent of filenames and modification
times. A replaced font produces a new key; unchanged fonts shared by multiple
files are downloaded once. Cache entries are immutable and may be removed only
by bounded eviction when they are not in use by an active mpv session.

The cache is an optimization, not authority: hash or size mismatches fail the
attachment fetch and must never expose a partial file to libass.

## Regression coverage

Deterministic coverage includes:

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

## Live-server verification record

The rendered Linux UI was tested with the `passthrough` model first, including:

1. Small cached headers and prompt first clusters on representative muxed files.
2. Immediate publication of the discontinuity packet for initial and seek epochs.
3. Active playback, pause, audio/subtitle discovery, and explicit selections.
4. Far seeks while playing and paused, including caller pause preservation.
5. A/V sync after active seek (`avsync` about -0.0005 s) and paused seek
   (`avsync` about -0.0011 s).
6. Safe Linux hardware decode through `vaapi-copy` over repeated reloads.
7. Clean session teardown with no `restart_required` or native teardown error.

Cache miss/hit and zero-redownload behavior also have deterministic coverage;
the Android implementation must repeat the physical-device font and cache
gates in its own plan.

After passthrough behavior is stable, repeat with one real ONNX model while
checking `/status` pipeline FPS so GPU contention is not mistaken for buffering.

## Acceptance criteria

- The first playable cluster follows the header within 250 ms on representative
  muxed files, including sparse subtitles.
- The client forwards the first packet of each epoch immediately.
- Linux startup reaches first position in under one second on the representative
  cached-header file; cross-device comparisons must still match tier, display,
  source, and network.
- Cache tuning does not introduce startup oscillation or ordinary Wi-Fi
  rebuffers.
- All original audio/subtitle tracks and ASS font rendering remain available.
- A seek transfers no attachment bodies once the session fonts are cached.
- Reopening unchanged media does not redownload cached fonts.

## Non-goals

This plan does not change model inference, encoder teardown, source decode
speed, or the absolute-PTS seek protocol. Those must remain independently
measurable.
