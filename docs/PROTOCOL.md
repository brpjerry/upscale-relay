# Upscale Relay Protocol v1

Contract between any client (desktop, Android) and the upscale server. Three
channels per session:

| channel  | transport                  | content                                   |
|----------|----------------------------|-------------------------------------------|
| control  | WebSocket `/control`       | JSON messages (this spec, §2)             |
| uplink   | TCP, framed packets (§3)   | source video elementary stream, client→server |
| downlink | TCP, framed packets (§3)   | chunks of an epoch Matroska stream, server→client |

Status/metrics: `GET /status` on the control port returns JSON. Most of it is
diagnostic (pipeline rates, stage times, queue depths; consumed by GUIs and
tests) and may change without notice. The fields in §1.1 are a stable contract.
The server-library HTTP surface is documented in
[SERVER_LIBRARY.md](SERVER_LIBRARY.md).

### 1.1 `GET /status` fields clients may rely on

A client that loses its control connection never receives `closed`, so it
cannot tell from the session protocol whether the server released its
session's native resources. It may poll `GET /status` instead.

| Field | Meaning |
|---|---|
| `sessions[].id` | one entry per session the server still holds; equals `session_opened.session_id`. `sessions` is always present (empty when there are none) and every entry has an `id` |
| `restart_required` | always present; `true` once a native session close has failed. The server then answers every `open_session` with the fatal error `server_restart_required` until it is restarted |
| `native_teardown_error` | `null`, or `{session_id, error, restart_required: true}` describing the most recent failed close. Informational: `restart_required` is `true` in every response where this is non-null (it is derived from it), and it is the flag to act on, since the named session is not necessarily the poller's |

Ordering guarantee: a session leaves `sessions` only after its native close
(pipeline workers, encoder/mux and inference owners) has returned, whatever
ended the session: `teardown`, a dropped control connection, a failed open or
server shutdown. When that close failed, `restart_required` and
`native_teardown_error` are set no later than the removal, so no response shows
the session gone with its failure still unrecorded. A client that polls until
its session id is absent therefore reads the outcome from that same response:
`restart_required: false` is equivalent to having received `closed`; `true`
means it must not open a replacement session until the server is restarted.

Timing: the server notices a dead control connection through its WebSocket
heartbeat (a ping every 20 s and 10 s for the pong, so within 30 s), and the
pipeline close that follows is bounded at 15 s, after which it counts as
failed. A session that had finished opening is therefore gone, or
`restart_required` is set, within 45 s; it stays listed until then, so a
listed session means "keep polling", not "stuck". One case takes longer: a
session whose `open_session` was still running (a first-use TensorRT engine
build) is closed only after that build returns, which can take minutes.

Recovery procedure both clients implement: a client that did not receive
`closed` remembers the session id with the server's host and port. On its next
control connection to that server, before any `open_session`, it polls
`GET /status` at once and then every second, for 60 s:

1. `restart_required: true`: stop; the server must be restarted (checked first).
2. Its session id absent from `sessions[].id`: released; proceed.
3. Still listed after 60 s: stop.
4. A non-2xx or malformed answer (no `sessions` array, an entry without `id`):
   stop. A request that fails in transport is not an answer: the question
   stays open and is asked again on the next connection.

The server's worst case for an opened session is 45 s, the sum of the two
timeouts above (`CONTROL_HEARTBEAT_S` x 1.5 and `PIPELINE_CLOSE_TIMEOUT_S`).
Clients poll for 60 s so that one which reconnects the moment its connection
died still sees the release. A server change to either timeout must stay
within 45 s, which `tests/test_server_status_contract.py` checks.

Design invariants:
- **Video PTS is never rewritten.** Outer packet timestamps and pipeline video
  timestamps are integer ticks in the *source video stream's* `time_base`, end
  to end. A server-file downlink carries the source video PTS just as an uplink
  session carries the client's. Muxed auxiliary streams retain their own
  source timestamps/time bases and the Matroska muxer performs only the normal
  container rescale.
- **Epochs guard seeks.** Every media packet carries the epoch it was produced
  in; anything from an older epoch is discarded on arrival (§4).
- **Confirmed auxiliary mode is authoritative.** A client uses in-band
  audio/subtitles only after `session_opened.aux_tracks == "muxed"`; otherwise
  it retains the external original-media path. Request fields alone never
  authorize a client to omit that fallback.
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
  begin. `play` is valid once the downlink is attached and, for an `uplink`
  source, its uplink is attached. A `server_file` session has no uplink.
- `PLAYING` vs `PAUSED` gate downlink pacing only; the server may keep filling
  the client buffer up to the watermark in both states.
- Any fatal error ⇒ server sends `error{fatal:true}` and moves to CLOSED.
- WS disconnect at any point ⇒ server tears down the pipeline and closes media
  sockets.

## 2. Control channel (JSON over WebSocket)

Every message: `{"type": "<name>", ...fields}`. Unknown fields must be
ignored. A server returns `error{code:"bad_message"}` for an unknown client
request type; a client ignores an unknown server event type so additive
notifications remain backward compatible.

Both ends send WebSocket pings every 20 s and close the connection when a
pong does not arrive in time, so a peer that vanished without closing (a
suspended laptop, a dropped network) is noticed while idle rather than at the
next request. No message type is involved and no protocol version change is
required.

### client → server

| type | fields | notes |
|---|---|---|
| `hello` | `protocol_version:int`, `client_name:str`, `display:{w:int,h:int}` | first message; server replies `capabilities` |
| `open_session` | `source:"uplink" \| {type:"server_file",path:str}` plus `file:{name:str, duration_s:float?, chapters:[{start_s:float, end_s:float?, title:str?}]?}`, `video:{codec:str, extradata_b64:str?, width:int, height:int, time_base:[num,den], avg_rate:[num,den]?, sample_aspect_ratio:[num,den]?, color:{primaries:int, transfer:int, matrix:int, range:"limited"\|"full"?}?, bit_depth:int?, hdr:{...}?, dolby_vision:{...}?, hdr10plus:bool?}`, `model:str`, `quality_tier:str`, `display:{w:int,h:int}`, `fit_mode:str?`, `resize_algorithm:str?`, `aux_tracks:"external"\|"muxed"?`, `aux_attachments:"embedded"\|"cached"?`, `high_bit_depth:"hdr"\|"all"?`, `display_target:{transfer:"pq"\|"sdr", peak_nits:float?, sdr_white_nits:float?}?` | `source` defaults to `"uplink"`; `video` is omitted for `server_file`; omitting `resize_algorithm` selects the server default. `file.chapters` carries the source container's chapter marks for uplink sessions (only the client can read them). `video.sample_aspect_ratio` is the source's pixel width over height, including any container override (e.g. Matroska display dimensions); send it for anamorphic sources. Absent, zero, non-integer or implausible (outside 1/10..10) values mean square pixels, as before this field existed. The server fits and crops by the displayed shape and delivers square pixels: a 720x576 16:15 source on a 1920x1080 display gives `downlink_width/height` 1440x1080. For `server_file` the server reads it from the file itself. When an uplink omits it, the server reads the aspect from the parameter sets in `extradata_b64` where it can (H.264 SPS VUI, HEVC), so a client need only send what its container declares; an explicit value always wins. Additive: no protocol version change. `aux_tracks` defaults to `"external"`; request `"muxed"` only for `server_file` after `capabilities.muxed_aux_tracks == true`. `aux_attachments` defaults to `"embedded"`; request `"cached"` only together with muxed tracks after `attachment_cache >= 1`. The server may still confirm a fallback mode. `high_bit_depth` (after `capabilities.high_bit_depth`) and the `video.color` / `bit_depth` / `hdr` / `dolby_vision` fields are §3.5's; `display_target` (after `capabilities.display_mapping`) and `video.hdr10plus` are §3.6's; all additive |
| `play` | | |
| `pause` | | |
| `seek` | `target_pts:int`, `epoch:int`, `display_target:{...}?` | epoch = client's current epoch + 1; see §4. `display_target` (§3.6) applies from this epoch on; absent keeps the current one |
| `buffer_report` | `buffered_ms:int`, `playing_pts:int?` | send ~every 500 ms while media attached |
| `teardown` | | graceful close; `closed` is sent only after pipeline workers and native encoder/mux/inference owners have released their resources |

### server → client

| type | fields | notes |
|---|---|---|
| `capabilities` | `protocol_version:int`, `server_name:str`, `models:[{name, scale_factor}]`, `quality_tiers:[str]`, `quality_options:[{id,label,codec,lossless,android_supported,p95_mbps}]`, `resize_algorithms:[str]`, `default_resize_algorithm:str`, `library:bool`, `library_sort:[str]`, `muxed_aux_tracks:bool`, `attachment_cache:int`, `high_bit_depth:bool?`, `display_mapping:bool?`, `server_id:str?` | `quality_tiers` remains the authoritative ID list; `quality_options` supplies presentation metadata. `library` means the library HTTP API is available. `muxed_aux_tracks` permits the opt-in mode. `attachment_cache >= 1` permits authenticated content-addressed attachment manifests; absent/zero means embedded attachments only. `high_bit_depth` permits `open_session.high_bit_depth` (§3.5). `display_mapping` permits `display_target` (§3.6). `server_id` is an opaque, non-secret identity for this server install, for keying per-server client state (history, watched flags, recents): 1-64 characters from `[A-Za-z0-9_-]`, stable across restarts, settings changes, address or port changes and upgrades. Clients must treat a missing value, or one outside that format, as absent and fall back to their own host:port. The server generates it once and keeps it in a `server-id` file in its per-user state directory (beside the instance lock; `UPSCALE_RELAY_LOCK_DIR` overrides it); deleting the file makes a new id, and a copied state directory shares its id. Additive: no protocol version change |
| `session_opened` | `session_id:str`, `media_port:int`, `uplink_token:str?`, `downlink_token:str`, `epoch:int(=0)`, `source:str`, `time_base:[num,den]`, `duration_s:float?`, `avg_rate:[num,den]?`, `chapters:[...]?`, `downlink_container:"matroska"`, `downlink_codec:str`, `downlink_extradata_b64:str?`, `downlink_width:int`, `downlink_height:int`, `fit_mode:str`, `resize_algorithm:str`, `aux_tracks:"external"\|"muxed"`, `aux_attachments:"embedded"\|"cached"`, `attachment_manifest:[{name,mimetype,size,sha256}]?`, `attachment_token:str?`, `source_has_audio:bool?`, `source_has_auxiliary:bool?`, `downlink_bit_depth:int?`, `downlink_color:{primaries,transfer,matrix,range}?`, `downlink_dolby_vision:bool?`, `source_bit_depth:int?`, `downlink_mapping:{...}?` | Effective choices are authoritative. The `downlink_*` colour fields and `source_bit_depth` are §3.5's, `downlink_mapping` §3.6's; absent means an 8-bit downlink from a server that predates them. A client omits its external original-media source after muxed confirmation or confirmed absence of source audio/subtitle tracks. `cached` means attachment bodies are omitted from every epoch and both manifest and token are required; `embedded` means any accepted attachments remain in each Matroska header. Absent auxiliary mode fields mean `external` and `embedded` |
| `state` | `state:"open"\|"playing"\|"paused"\|"closed"` | emitted on every transition |
| `session_progress` | `stage:str`, `message:str`, `elapsed_s:float` | ticked ~every 2 s while a slow `open_session` is being processed (today: `stage:"pipeline_init"`, e.g. a first-use TensorRT engine build, which can run for minutes). Clients must treat it as a keepalive for the pending open — refresh the open_session timeout on every tick — and should surface `message` as a loading indicator. Quick opens send none |
| `seek_ready` | `epoch:int`, `downlink_color:{...}?`, `downlink_mapping:{...}?` | server has flushed; an uplink client may start sending the new epoch. A server-file source is restarted by the server. **Not a readiness signal for media** — it is dispatched within milliseconds, before any frame of the new epoch exists (§4) |
| `seek_progress` | `epoch:int`, `target_pts:int`, `keyframe_pts:int?`, `frames_discarded:int`, `elapsed_s:float` | ticked ~every 500 ms while a seek has produced no downlink bytes yet, starting ~750 ms after the `seek`. Purely informational: the client renders it as seek progress so a long discard window (§4 step 4) is distinguishable from a wedged pipeline. `keyframe_pts` is null until the server's demuxer reports where the seek landed. A seek that produces media promptly sends none, and the server stops ticking after 60 s without downlink bytes or advancing subtitle-index progress. Additive and safe to ignore — clients that do not know the type must skip it, so it carries no protocol-version change |
| `stats` | `pipeline_fps:float`, `queue_depths:{...}`, `buffered_ahead_ms:int` | periodic, informational |
| `error` | `code:str`, `message:str`, `fatal:bool` | non-fatal errors leave the session usable |
| `closed` | | resource-release barrier acknowledging successful teardown; no acknowledgement is sent if a native owner times out |

For `server_file`, the optional `source_has_audio` and `source_has_auxiliary`
booleans describe the original file, independently of the negotiated auxiliary
mode. `source_has_auxiliary` covers audio or subtitle tracks. In external mode,
a confirmed `false` omits the original-media attachment; otherwise a confirmed
`source_has_audio:false` selects one delayed `sub-add` instead of `audio-add`.
Absent source metadata preserves the older external `audio-add` behavior.
Uplink clients use their own cached source metadata.

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
`unknown_model`, `unknown_tier`, `unknown_resize_algorithm`, `decode_error`,
`pipeline_error`, `media_timeout`, `server_restart_required`, `internal`.

## 3. Media framing (uplink and downlink)

### 3.1 Connection handshake

After TCP connect, the peer sends exactly 41 bytes:

```
magic     6 bytes   "UPRLY1"
direction 1 byte    0x01 = uplink, 0x02 = downlink
token     34 bytes  ASCII hex token from session_opened
```

Tokens are 17-byte random values hex-encoded (34 ASCII chars). The server
validates the token, associates the socket with the session, and replies with
1 byte: `0x00` OK, `0x01` reject (then closes). On the downlink connection the
*server* starts sending frames after the OK byte; the client only reads.

### 3.2 Packet format

All integers little-endian. An uplink packet contains one encoded video access
unit. A downlink packet contains one opaque chunk of the epoch's Matroska byte
stream and can therefore include header, video, audio, subtitle, attachment,
cluster, cue, or trailer bytes.

```
offset size  field
0      4     payload_len (uint32)
4      1     flags: bit0 keyframe, bit1 discontinuity, bit2 end_of_stream
5      4     epoch (uint32)
9      8     pts (int64, ticks in source stream time_base; INT64_MIN = none)
17     8     dts (int64, INT64_MIN = none)
25     n     payload (uplink access unit or downlink Matroska bytes)
```

- `discontinuity` is set on the first packet after a seek (new epoch). On
  uplink it resets the server decoder; on downlink it begins a fresh container
  and the client replaces its demuxer/player input.
- `end_of_stream`: payload_len = 0; uplink: file fully sent; downlink: pipeline
  fully drained after its source iterator(s) end (never during a seek flush).
- A payload is limited to 64 MiB. Senders enforce the limit and receivers
  reject an oversized length immediately after reading the header, before
  allocating or reading its body. File size is independent of packet size.
- Uplink payloads are in the source codec's storage format (e.g. length-
  prefixed AVCC for H.264, as extracted; codec-specific detail is carried by
  `open_session.video.codec` + `extradata_b64`).
- Downlink payloads are chunks of a **self-describing container byte stream**
  (v1: streaming Matroska, announced as `downlink_container:"matroska"` in
  `session_opened`, alongside informational `downlink_codec` and
  `downlink_width/height`). The container carries the original PTS, so any
  demuxer-based player consumes it directly; the packet-header `pts` field
  identifies the video frame that triggered that chunk's flush (buffer
  accounting only, and `INT64_MIN` for header/trailer-only flushes).
  The video stream always carries explicit colour tags (matrix, range,
  primaries, transfer): the source's own, with any it leaves unspecified
  resolved as mpv resolves them for the source's size, so a player shows
  the upscaled picture in the colours of the original.
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

### 3.3 Negotiated auxiliary-track behavior

The negotiation matrix is:

| Source / confirmation | Client media behavior |
|---|---|
| `uplink` | Relay downlink plus the client-local original as external audio/subtitles |
| `server_file`, `aux_tracks:"external"` (including absent field) | Relay downlink plus `GET /media/<path>` as one external original-media source |
| `server_file`, `aux_tracks:"muxed"` | Relay downlink alone; audio/subtitles are stream-copied into every epoch |

For a confirmed muxed session, clients must not also attach `/media`: one
`audio-add` exposes all tracks from that external file, so doing both creates
duplicate tracks, duplicate source reads, and a second seek lifecycle. Each
epoch is a fresh Matroska file. Clients must re-enumerate its tracks after
reload and reapply the user's explicit audio/subtitle choice (including
subtitles off); track IDs are not a cross-protocol identity.

`aux_attachments:"embedded"` needs no separate client transfer. With
`aux_attachments:"cached"`, clients must materialize and register the verified
font view before loading the first epoch. Seeks within that session reuse the
same view and perform no attachment HTTP reads. A later session or another
file reuses objects by SHA-256, subject to bounded eviction. If a requested
mode is not confirmed, follow the confirmed fallback; never infer that
attachments were omitted merely because `cached` was requested.

The server's auxiliary input is a second demuxer. It seeks against the source
video stream's keyframe cues while emitting only audio/subtitle packets;
Matroska audio streams are commonly not indexed, and using audio as the seek
anchor can force a long linear scan. Original auxiliary PTS and codec packets
are preserved. A small audio preroll and subtitle events overlapping the seek
target may precede the first video PTS; the player performs normal timestamp
synchronization.

The current server preserves overlapping ASS/SSA, SubRip, WebVTT, and plain-text
subtitle events with a temporary per-session packet index. Normal demux fills
it progressively; an unseen forward seek scans at most 10 s of source before
the target (never the whole unplayed prefix, which made far seeks in long files
read nearly the entire file) without decoding video, and reports `seek_progress.stage:"subtitle_index"`,
`message`, and `subtitle_indexed_s` while doing so. Repeated/backward seeks reuse
the index and retain the video-keyframe seek anchor. The index has a 256 MiB
disk limit and a 2 MiB SQLite page cache, is removed at session teardown, and
reports an explicit session error if exhausted. Stateful bitmap subtitles
(such as PGS/VobSub) currently confirm `aux_tracks:"external"` so their full
display/clear state survives seeks; clients must honor that confirmation.

### 3.4 Cached attachment objects

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
cache-capable server confirms this mode only when every attachment is a
recognized font type that a client can register with libass; otherwise it
embeds attachments or falls back to external auxiliary media.

`name` is a sanitized basename for a per-session font view, `mimetype` is
informational, `size` is the exact response size, and `sha256` is 64 lowercase
hexadecimal characters. Duplicate hashes describe one object. The current
client/server safety limits are 64 MiB per object and 256 MiB per manifest;
clients should also bound their persistent object store (the desktop default
is 512 MiB). Responses use `Cache-Control: private, max-age=31536000,
immutable`, but the content hash—not HTTP freshness—is the integrity check.
Bearer tokens and source paths must not be written to ordinary logs.

### 3.5 Deep colour and HDR

A server advertising `capabilities.high_bit_depth: true` accepts
`open_session.high_bit_depth`:

| Request | Downlink |
|---|---|
| absent | 8-bit 4:2:0, as every server before this field. HDR sources keep their PQ/HLG and BT.2020 tags but lose precision and all HDR metadata |
| `"hdr"` | 10-bit 4:2:0 (HEVC Main 10, or 10-bit FFV1) for sources whose transfer is PQ (16) or HLG (18); 8-bit for the rest |
| `"all"` | as `"hdr"`, and also 10-bit for any other source deeper than 8 bits (e.g. 10-bit SDR anime) |

A server with no 10-bit encoder for the requested tier streams 8 bits, as if
nothing had been asked; `downlink_bit_depth` says which.

A 10-bit HEVC downlink of an HDR source also carries, in its bitstream,
what the source's had:

- the mastering display colour volume and content light level SEI on every
  keyframe, as the source frame encoded there has them: from the source
  bitstream, else from the source container (`video.hdr`);
- the source frame's HDR10+ messages (ITU-T T.35 SEI) on that frame;
- the Dolby Vision RPU NAL unit (type 62, last in the access unit) on its frame,
  when the source is Dolby Vision profile 8 (`video.dolby_vision.profile == 8`),
  whatever its base layer: HDR10 (8.1), HLG (8.4) or SDR (8.2, which is 10-bit
  only under `"all"`). `session_opened.downlink_dolby_vision` says so. Other
  profiles' RPUs are dropped: profile 7's composes an enhancement layer the
  relay does not carry, and profile 5 has no displayable base layer without it
  (its colours are wrong with or without this field). There is no `dvcC`
  configuration record in the Matroska header.

Static and dynamic metadata are copied from the source frame with the same
PTS, dynamic metadata byte for byte from its access unit. The FFV1 tier
carries colour tags only.

The decision is made at `open_session`, so the server needs the source's
colour and depth before any frame decodes. For `server_file` it reads them
from the file. An uplink client should send, from its own demuxer:

- `video.color`: ITU-T H.273 code points (FFmpeg's enum values) as the
  container declares them, `2` for unspecified, and `range` only when known.
  The depth is decided from these. The downlink is tagged with the first
  decoded frame's tags where the bitstream specifies them, as it always was
  (FFmpeg applies an alternative transfer SEI, such as HLG broadcast over
  BT.2020-10 signalling, only to frames), and with these where it does not,
  so container-only tags are honoured.
- `video.bit_depth`: bits per sample of the decoded format.
- `video.hdr`: container-level static metadata,
  `{mastering_display:{primaries:[[x,y] red, green, blue], white_point:[x,y], max_luminance, min_luminance}, content_light_level:{max_cll, max_fall}}`,
  chromaticities in 0.00002 units, luminance in 0.0001 cd/m², light levels in cd/m²
  (the SEI's units).
- `video.dolby_vision`: `{profile, level, bl_signal_compatibility_id}` from the
  Dolby Vision configuration record.

When an HEVC uplink omits `color` or `bit_depth`, the server reads them from
the parameter sets in `extradata_b64`. Otherwise unknown means 8-bit.

`session_opened` reports the outcome: `downlink_bit_depth` (8 or 10),
`downlink_color` (the resolved tags the downlink is encoded with, `range` by
name; for an uplink, as resolved from `video.color` before its first frame,
and null when that is unknown too),
`downlink_dolby_vision`,
and `source_bit_depth`. `GET /status` `sessions[].pipeline` adds `bit_depth`,
`color`, and `hdr_metadata` (null for SDR): the static metadata written and the
counts of frames given an RPU or HDR10+. These `/status` fields are
diagnostics, not part of the §1.1 contract.

### 3.6 Display-mapped HDR (dynamic metadata)

Dolby Vision and HDR10+ describe each scene for the display that shows it.
Clients whose renderer uses static HDR10 metadata only (the desktop player:
libmpv's render API runs mpv's `gpu` renderer) cannot apply them, so a server
advertising `capabilities.display_mapping: true` applies them for the
client's display instead, where it can.

The client describes the display it presents on in `open_session` (with
`high_bit_depth`), and again in any `seek` after it changes:

```
display_target: {transfer: "pq" | "sdr", peak_nits: float?, sdr_white_nits: float?}
```

- `"pq"`: the client shows PQ, BT.2020, as HDR, up to `peak_nits` (the
  display's peak), with SDR white at `sdr_white_nits` (203 when absent).
- `"sdr"`: the client shows SDR. `sdr_white_nits` is the display's white
  level, when known.

The sources a server maps carry dynamic metadata a client cannot use: a
Dolby Vision RPU (profiles 5, 7 and 8; profile 7's enhancement layer stays
dropped) or HDR10+ messages. What it applies depends on the target:

| `transfer` | sources mapped | downlink |
|---|---|---|
| `"sdr"` | Dolby Vision whose base layer is HDR (7, 8.1, 8.4) or not displayable (5); HDR10+ | SDR, BT.709 primaries and transfer, tone-mapped by the dynamic metadata (SMPTE ST 2094-10 for Dolby Vision, which wins when a source has both, ST 2094-40 for HDR10+); 10-bit under `high_bit_depth`. No RPU or HDR10+ messages: they are applied |
| `"pq"` | Dolby Vision profile 5 | 10-bit PQ, BT.2020: the RPU's reshaping applied at the source's own brightness. It is not fitted to `peak_nits`; the client's renderer fits it to the display, as it does HDR10. Static metadata as §3.5 writes it (the source's, when it has any) |

Every other source and target pair streams as §3.5 says: HDR10, HLG and SDR
sources whatever the target, and Dolby Vision 7 and 8 and HDR10+ sources
for a `"pq"` target, whose HDR10 or HLG base layer the client's renderer fits
to the display itself (a profile 8 RPU and HDR10+ messages still ride along
for renderers that use them). A request without `display_target`, or to a
server without the capability, is §3.5's.

The server decides at `open_session` whether a session is mapped, for any
target: a mapped epoch decodes through a different stage. Dolby Vision is
known from `video.dolby_vision`. HDR10+ messages ride in the frames, so an
uplink says whether its first keyframe carries one in `video.hdr10plus`
(`relay_media.hdr.has_hdr10plus` reads it from the packet). An uplink
that omits it is mapped for Dolby Vision only; its HDR10+ streams as §3.5
says. For `server_file` the server reads both itself.

A server advertises `display_mapping` only when it can map: it needs a
tone mapper that applies Dolby Vision RPUs and HDR10+ (FFmpeg with
libplacebo on Vulkan), and checks for it at start-up.

`session_opened.downlink_mapping` is null on a session the server never
maps: no `display_target`, or a source it maps for no target. On a session
it maps for some target it is set on every epoch, and says what that epoch
applied:

```
downlink_mapping: {transfer: "pq" | "sdr", peak_nits: float?, sdr_white_nits: float?,
                   applied: ["dolby_vision" | "hdr10plus", ...]}
```

- `transfer`: the `display_target.transfer` the epoch was made for.
- `peak_nits`, `sdr_white_nits`: the luminances the stream was fitted to,
  null when it was not fitted to the client's (both are null for now).
- `applied`: the dynamic metadata applied. Empty means that this epoch streams
  as §3.5 says for that target: a profile 8.1 source for a `"pq"` target.

`downlink_color` describes the epoch's stream (BT.709 for a mapped `"sdr"`
one), so a client that knows nothing of §3.6 still tags and plays it
correctly.

A `seek` whose `display_target` has another `transfer` remaps from that epoch
on: the client changed screens, its display switched between SDR and HDR,
or it is about to show the video fullscreen where the compositor switches to
HDR. Its `seek_ready` repeats `downlink_color` and `downlink_mapping` for the
new epoch, which is a fresh Matroska file (§4) and may change between PQ and
SDR, mapped and not. A `seek` without `display_target`, or one with the same
`transfer`, keeps the mapping. `seek_ready` carries the two fields only on a
mapped session. The first frame of a mapped epoch comes ~0.5 s later than an
unmapped seek's: the mapping stage starts again with each epoch.

## 4. Seek protocol (epochs)

Epoch is a uint32 starting at 0 per session, incremented only by seeks.

1. Client decides to seek to `T` (ticks). It sets `E' = E + 1`, immediately
   stops consuming downlink data, and sends `seek{target_pts:T, epoch:E'}`.
2. Server: aborts in-flight work, flushes decoder/inference/encoder queues,
   discards any uplink packet with `epoch < E'` (already-buffered or still
   arriving), then replies `seek_ready{epoch:E'}`.
3. For an `uplink` source, the client restarts its demux/uplink from the
   nearest keyframe at or before `T`, first packet flagged `discontinuity`, all
   packets stamped `E'`. For `server_file`, the server restarts both its video
   and (when muxed) auxiliary iterators; the client starts no uplink.
4. Server decodes and discards video frames with `pts < T`, then resumes inference/
   encode/downlink; downlink packets are stamped `E'`, first one flagged
   `discontinuity` + `keyframe`. This discard window is serial dead time and
   dominates post-seek latency — measured at ~3.6 s for a 250-frame 1080p GOP
   (see [SEEK_LATENCY_PLAN.md](SEEK_LATENCY_PLAN.md)) — so the server sends
   `seek_progress` while it runs.
5. Client discards any downlink packet with `epoch < E'`, retires the old
   localhost stream, and opens the new epoch as a fresh Matroska file. It does
   not pass `start=`: absolute Matroska PTS place the epoch. A muxed session
   gets its new audio/subtitle tracks from that file; an external session
   reattaches its one original-media demuxer after playback restart.

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
