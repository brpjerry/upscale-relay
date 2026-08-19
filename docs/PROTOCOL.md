# Upscale Relay Protocol v1

Contract between any client (desktop, Android) and the upscale server. Three
channels per session:

| channel  | transport                  | content                                   |
|----------|----------------------------|-------------------------------------------|
| control  | WebSocket `/control`       | JSON messages (this spec, §2)             |
| uplink   | TCP, framed packets (§3)   | source video elementary stream, client→server |
| downlink | TCP, framed packets (§3)   | upscaled encoded video, server→client     |

Status/metrics: `GET /status` on the control port returns JSON (not part of
the session protocol; consumed by GUIs and tests). The server-library HTTP
surface is documented in [SERVER_LIBRARY.md](SERVER_LIBRARY.md). If a bounded
native session close fails, `restart_required` is true and
`native_teardown_error` identifies the failed session; the server rejects new
sessions until it is restarted.

Design invariants:
- **PTS is never rewritten.** All timestamps are integer ticks in the *source
  video stream's* `time_base`, end to end. The downlink carries the same PTS
  values the uplink delivered.
- **Epochs guard seeks.** Every media packet carries the epoch it was produced
  in; anything from an older epoch is discarded on arrival (§4).
- Transport is assumed ordered and reliable per connection (TCP now; QUIC
  streams later — nothing in the spec depends on TCP specifics).

## 1. Session lifecycle & state machine

One WebSocket connection = at most one session.

```
        hello/capabilities
IDLE ──────open_session────► OPEN ──play──► PLAYING ◄──play/pause──► PAUSED
                              │                │                        │
                              └────────────────┴──────teardown─────────┴──► CLOSED
                                          (or WS disconnect ⇒ CLOSED)
```

- `OPEN`: pipeline allocated, media connections may attach, pre-buffering may
  begin. `play` is valid once both media connections are attached.
- `PLAYING` vs `PAUSED` gate downlink pacing only; the server may keep filling
  the client buffer up to the watermark in both states.
- Any fatal error ⇒ server sends `error{fatal:true}` and moves to CLOSED.
- WS disconnect at any point ⇒ server tears down the pipeline and closes media
  sockets.

## 2. Control channel (JSON over WebSocket)

Every message: `{"type": "<name>", ...fields}`. Unknown fields must be
ignored; unknown types ⇒ `error{code:"bad_message"}`.

### client → server

| type | fields | notes |
|---|---|---|
| `hello` | `protocol_version:int`, `client_name:str`, `display:{w:int,h:int}` | first message; server replies `capabilities` |
| `open_session` | `source:"uplink" \| {type:"server_file",path:str}` plus `file:{name:str, duration_s:float?, chapters:[{start_s:float, end_s:float?, title:str?}]?}`, `video:{codec:str, extradata_b64:str?, width:int, height:int, time_base:[num,den], avg_rate:[num,den]?}`, `model:str`, `quality_tier:str`, `display:{w:int,h:int}`, `fit_mode:str?`, `resize_algorithm:str?`, `aux_tracks:"external"\|"muxed"?`, `aux_attachments:"embedded"\|"cached"?` | `source` defaults to `"uplink"`; `video` is omitted for `server_file`; omitting `resize_algorithm` selects the server default. `file.chapters` carries the source container's chapter marks for uplink sessions (only the client can read them). `aux_tracks` defaults to `"external"`; `"muxed"` is valid only for `server_file` when advertised. `aux_attachments` defaults to `"embedded"`; `"cached"` requires muxed tracks and capability `attachment_cache >= 1` |
| `play` | | |
| `pause` | | |
| `seek` | `target_pts:int`, `epoch:int` | epoch = client's current epoch + 1; see §4 |
| `buffer_report` | `buffered_ms:int`, `playing_pts:int?` | send ~every 500 ms while media attached |
| `teardown` | | graceful close; `closed` is sent only after pipeline workers and native encoder/mux/inference owners have released their resources |

### server → client

| type | fields | notes |
|---|---|---|
| `capabilities` | `protocol_version:int`, `server_name:str`, `models:[{name, scale_factor}]`, `quality_tiers:[str]`, `quality_options:[{id,label,codec,lossless,android_supported,p95_mbps}]`, `resize_algorithms:[str]`, `default_resize_algorithm:str`, `library:bool`, `library_sort:[str]`, `muxed_aux_tracks:bool`, `attachment_cache:int` | `quality_tiers` remains the authoritative ID list; `quality_options` supplies presentation metadata. `library` means the library HTTP API is available. `muxed_aux_tracks` permits the opt-in mode. `attachment_cache >= 1` permits authenticated content-addressed attachment manifests; absent/zero means embedded attachments only |
| `session_opened` | `session_id:str`, `media_port:int`, `uplink_token:str?`, `downlink_token:str`, `epoch:int(=0)`, `source:str`, `time_base:[num,den]`, `duration_s:float?`, `avg_rate:[num,den]?`, `chapters:[...]?`, `downlink_width:int`, `downlink_height:int`, `fit_mode:str`, `resize_algorithm:str`, `aux_tracks:"external"\|"muxed"`, `aux_attachments:"embedded"\|"cached"`, `attachment_manifest:[{name,mimetype,size,sha256}]?`, `attachment_token:str?` | Effective choices are authoritative. A client omits `/media` only after muxed confirmation and omits in-container attachment bodies only after cached confirmation. Cached manifests contain sanitized immutable font objects; the bearer token expires with the session. Absent auxiliary fields mean `external` and `embedded` |
| `state` | `state:"open"\|"playing"\|"paused"\|"closed"` | emitted on every transition |
| `session_progress` | `stage:str`, `message:str`, `elapsed_s:float` | ticked ~every 2 s while a slow `open_session` is being processed (today: `stage:"pipeline_init"`, e.g. a first-use TensorRT engine build, which can run for minutes). Clients must treat it as a keepalive for the pending open — refresh the open_session timeout on every tick — and should surface `message` as a loading indicator. Quick opens send none |
| `seek_ready` | `epoch:int` | server has flushed; client may start uplinking the new epoch. **Not a readiness signal for media** — it is dispatched within milliseconds, before any frame of the new epoch exists (§4) |
| `seek_progress` | `epoch:int`, `target_pts:int`, `keyframe_pts:int?`, `frames_discarded:int`, `elapsed_s:float` | ticked ~every 500 ms while a seek has produced no downlink bytes yet, starting ~750 ms after the `seek`. Purely informational: the client renders it as seek progress so a long discard window (§4 step 4) is distinguishable from a wedged pipeline. `keyframe_pts` is null until the server's demuxer reports where the seek landed. A seek that produces media promptly sends none, and the server stops ticking after 60 s. Additive and safe to ignore — clients that do not know the type must skip it, so it carries no protocol-version change |
| `stats` | `pipeline_fps:float`, `queue_depths:{...}`, `buffered_ahead_ms:int` | periodic, informational |
| `error` | `code:str`, `message:str`, `fatal:bool` | non-fatal errors leave the session usable |
| `closed` | | resource-release barrier acknowledging successful teardown; no acknowledgement is sent if a native owner times out |

`fit_mode` (default `"fit"`) chooses how the output frame relates to `display`.
`"fit"` preserves the full image and returns the largest same-aspect frame that
fits inside `display` (the player letterboxes it). `"cover"` center-crops the
post-ONNX image to the display aspect ratio on the server, then returns an
aligned display-sized frame. Off-screen pixels are therefore neither encoded
nor sent to the client. Unknown values produce a non-fatal `bad_message` error.

`resize_algorithm` controls the server's final post-ONNX scale. Servers
advertise their supported names and configured default. The current algorithms
are `fast-bilinear`, `bilinear`, `bicubic`, `area`, `bicublin`, `gaussian`,
`sinc`, `lanczos`, and `spline`;
`lanczos` is the default unless `relay-server --resize-algorithm` changes it.
The selected algorithm applies to both fit and cover, and is echoed in
`session_opened`. Unknown names produce `unknown_resize_algorithm`.

The current quality IDs are `lossless-hevc`, `hevc-qp2`, `hevc-qp4`,
`hevc-qp6`, `hevc-qp10`, `hevc-qp14`, `hevc-qp18`, and `lossless-ffv1`.
Clients display `quality_options[].label`, rather than deriving a user-facing
name from the ID. Android filters on `android_supported` and therefore omits
FFV1.

Error codes (initial set): `bad_message`, `unsupported_version`,
`unknown_model`, `unknown_tier`, `unknown_resize_algorithm`, `decode_error`, `pipeline_error`,
`media_timeout`, `internal`.

## 3. Media framing (uplink and downlink)

### 3.1 Connection handshake

After TCP connect, the peer sends exactly 41 bytes:

```
magic     6 bytes   "UPRLY1"
direction 1 byte    0x01 = uplink, 0x02 = downlink
token     34 bytes  ASCII hex token from session_opened (padded, see note)
```

Tokens are 17-byte random values hex-encoded (34 ASCII chars). The server
validates the token, associates the socket with the session, and replies with
1 byte: `0x00` OK, `0x01` reject (then closes). On the downlink connection the
*server* starts sending frames after the OK byte; the client only reads.

### 3.2 Packet format

All integers little-endian. One packet = one encoded video packet (access
unit).

```
offset size  field
0      4     payload_len (uint32)
4      1     flags: bit0 keyframe, bit1 discontinuity, bit2 end_of_stream
5      4     epoch (uint32)
9      8     pts (int64, ticks in source stream time_base; INT64_MIN = none)
17     8     dts (int64, INT64_MIN = none)
25     n     payload (encoded bitstream data)
```

- `discontinuity` is set on the first packet after a seek (new epoch) — the
  receiver resets its decoder before consuming it.
- `end_of_stream`: payload_len = 0; uplink: file fully sent; downlink: pipeline
  fully drained (only sent after uplink EOS, never during a seek flush).
- Uplink payloads are in the source codec's storage format (e.g. length-
  prefixed AVCC for H.264, as extracted; codec-specific detail is carried by
  `open_session.video.codec` + `extradata_b64`).
- Downlink payloads are chunks of a **self-describing container byte stream**
  (v1: streaming Matroska, announced as `downlink_container:"matroska"` in
  `session_opened`, alongside informational `downlink_codec` and
  `downlink_width/height`). The container carries the original PTS, so any
  demuxer-based player consumes it directly; the packet-header `pts` field
  holds the newest video PTS muxed into that chunk (buffer accounting only).
  A negotiated server-file session may also contain stream-copied original
  audio/subtitle tracks. Attachments are either embedded in every epoch or,
  when explicitly negotiated as cached, omitted and supplied through the
  authenticated manifest endpoint. A server may confirm `external` when a
  file cannot be remuxed efficiently.
  The outer framing is unchanged. Each
  epoch is its own complete container stream: the first chunk after session
  start or a seek carries the `discontinuity` flag and begins with a fresh
  container header — the receiver reopens its demuxer there. Chunk boundaries
  otherwise carry no meaning.

### 3.3 Cached attachment objects

For a session confirming `aux_attachments:"cached"`, the client fetches each
missing manifest object with:

```text
GET /attachments/<lowercase-sha256>
Authorization: Bearer <attachment_token>
```

The server accepts only hashes declared by that active session, never accepts
a client-selected source path, and expires the token at teardown. Clients must
enforce object/session size bounds, verify the declared SHA-256 before an
atomic cache publish, and discard partial or mismatched downloads. The current
cache-capable server confirms this mode only when every attachment is a font
type supported by the desktop libass font-directory path; otherwise it embeds
attachments or falls back to external auxiliary media.

## 4. Seek protocol (epochs)

Epoch is a uint32 starting at 0 per session, incremented only by seeks.

1. Client decides to seek to `T` (ticks). It sets `E' = E + 1`, immediately
   stops consuming downlink data, and sends `seek{target_pts:T, epoch:E'}`.
2. Server: aborts in-flight work, flushes decoder/inference/encoder queues,
   discards any uplink packet with `epoch < E'` (already-buffered or still
   arriving), then replies `seek_ready{epoch:E'}`.
3. Client restarts the uplink from the nearest keyframe at or before `T`,
   first packet flagged `discontinuity`, all packets stamped `E'`.
4. Server decodes and discards frames with `pts < T`, then resumes inference/
   encode/downlink; downlink packets are stamped `E'`, first one flagged
   `discontinuity` + `keyframe`. This discard window is serial dead time and
   dominates post-seek latency — measured at ~3.6 s for a 250-frame 1080p GOP
   (see [SEEK_LATENCY_PLAN.md](SEEK_LATENCY_PLAN.md)) — so the server sends
   `seek_progress` while it runs.
5. Client discards any downlink packet with `epoch < E'`, resets its decoder
   at the discontinuity, and resumes presentation at `pts >= T`.

A server started with `--seek-discard-max-s S` trades step 4 away when the
keyframe lands more than `S` seconds before `T`: it emits from the keyframe
instead, so the first downlink packet carries `pts < T`. Absolute PTS stay
authoritative, so the client's position readout remains correct — playback
simply starts earlier than requested. Clients must therefore not assume the
first packet of a new epoch has `pts >= T`. The default is off (frame-accurate).

Coalescing: a `seek` arriving while a previous seek is being processed simply
supersedes it — the server flushes again (cheap; queues are already empty),
adopts the newest epoch, and replies `seek_ready` once with that epoch. The
client must treat any `seek_ready{epoch}` older than its current epoch as
stale and keep waiting.

Rules that make this airtight:
- Receivers drop stale-epoch packets *before* any other processing.
- The uplink must never interleave epochs: after sending `seek`, the client
  stops sending old-epoch packets immediately (in-flight ones are fine — the
  server drops them).
- `buffer_report.playing_pts` is meaningless across epochs; the server ignores
  reports whose implied epoch predates the current one (clients should simply
  suppress reports between `seek` and first new-epoch downlink data).

## 5. Backpressure / pacing

- The client reports `buffered_ms` (downlink data decoded-or-queued ahead of
  the presentation clock) every ~500 ms.
- Server pacing: run the pipeline freely while the *estimated* client buffer
  is below `HIGH_WATERMARK` (default 10 000); pause inference when above;
  resume below RESUME_WATERMARK (default 9 500). The narrow band keeps the
  client buffer topped off near the watermark — the server must not let it
  drain substantially before resuming.
- The estimate is the last reported `buffered_ms` decayed by wall time while
  PLAYING (the client consumes in real time). Pacing must never act on a raw
  stale report: if reports stall (busy client, saturated link, dead reporter),
  a frozen high value would wedge the pause while the real buffer drains to
  zero. If reports stop entirely the estimate floors out and the pipeline
  free-runs; transport backpressure (bounded downlink queue) caps production.
- The server must never silently stall: if the pipeline cannot sustain
  real-time and the client buffer empties, the client sees it as `buffered_ms`
  → 0 and handles rebuffering UX; the server additionally emits
  `stats.pipeline_fps` so clients can warn early.

## 6. Versioning

`protocol_version` is an integer (current: 1). Server rejects mismatched
majors with `error{code:"unsupported_version", fatal:true}`. Additive fields
are allowed without a version bump (receivers ignore unknown fields).

## 7. Security status

The random media tokens authenticate TCP attachments to an already-open
session; the short-lived attachment bearer token authorizes only hashes in one
active manifest. Neither is user authentication. Control-channel pairing,
persistent client credentials, TLS, and authorization for `/library` and
`/media` are planned but not implemented. The current server is intended for a
trusted LAN.
