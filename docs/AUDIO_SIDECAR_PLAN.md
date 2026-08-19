# In-band audio/subtitle tracks in the epoch downlink

**Status:** server and Qt client implemented; Android negotiation and remaining
subtitle/device gates are in progress. The former persistent `/media-audio`
sidecar/cache proposal was replaced on 2026-08-18 by negotiated auxiliary
tracks in the existing per-epoch Matroska downlink.

## Goal

For a server-library source, play original audio and subtitles without sending
the complete pre-upscale container to the client. Preserve arbitrary relay
seeks, every audio/subtitle track, absolute timestamps, metadata, attachments,
and explicit client track choices.

Today the upscaled video arrives through the relay downlink while mpv opens
`GET /media/<path>` as an external file. Matroska interleaves all tracks, so a
client wanting roughly 1 Mbps of audio still downloads the source video. On
the measured 27.54 GiB remux, video was 97.4% of the bytes: a full watch moved
about 27.5 GiB to use about 0.9 GiB of audio/subtitle data.

## Key observation

The external file has to be HTTP-seekable only because mpv must reposition an
independent, full-duration demuxer after playback starts. The relay downlink
does not seek internally. A relay seek already:

1. increments the epoch;
2. seeks the server source;
3. abandons the old streaming Matroska container;
4. opens a fresh container whose packets retain absolute source PTS; and
5. makes the client reload that new one-shot stream.

Audio and subtitles inside that fresh container therefore begin at the epoch
position and need no HTTP range seek. The existing downlink framing already
carries opaque chunks of a self-describing Matroska byte stream, so adding
tracks inside it does not require a new media frame format.

## Design

```text
server source video packets -> decode -> infer -> fit/encode --+
                                                             +-> epoch MKV -> existing downlink
server source audio/subtitle packets ------ stream copy ------+
source attachments/track metadata -------- container header --+
```

### Negotiation and compatibility

- `capabilities.muxed_aux_tracks: bool` advertises support.
- A client requests `open_session.aux_tracks: "muxed"` only for a
  `server_file` source.
- `session_opened.aux_tracks` confirms `"muxed"` or `"external"`.
- The default is `"external"`. Old clients therefore continue receiving a
  video-only downlink and attaching `/media`; old servers ignore the additive
  request and omit the confirmation, which new clients interpret as
  `"external"`.
- Client-local/uplink sessions keep their local file as the external source.
  They create no network waste and the server does not possess their auxiliary
  tracks.

No protocol-version bump is needed: negotiation is additive, opt-in, and the
outer downlink remains the same epoch-stamped sequence of Matroska chunks.

### Server source and native ownership

The first implementation uses two input containers:

- the existing `VideoTrack`, which is still the authoritative video seek and
  decode source;
- an `AuxiliaryTrack`, which owns a separate, lock-serialized PyAV container
  and demuxes only audio/subtitle packets.

Both iterators seek for every epoch. Their packets are merged in timestamp
order before entering the existing bounded pipeline queue. Auxiliary packets
pass unchanged through the decode and inference queues and are muxed only by
the finish thread. Keeping the actual `av.Packet` preserves packet side data;
containers and codecs never have concurrent owners.

This can read the interleaved source twice on the server/NAS, but it fixes the
client/Wi-Fi bandwidth problem on the first play without a build delay or disk
cache. A later optimization may replace the two readers with a unified
server-source demux after correctness is proven.

The finish thread opens a metadata-only template container for the same source
and recreates, in source order:

- every audio track;
- every subtitle track;
- language, title, disposition, time base, codec parameters, and extradata;
- Matroska attachments such as SSA/ASS fonts.

The encoded video remains track 0. Auxiliary output headers are deterministic
across epochs so mpv track ids remain stable for the session.

### Seek boundary

For a target `T`:

- video behavior is unchanged: decode from the preceding video keyframe and
  discard until `T`, unless the configured keyframe-mode threshold deliberately
  chooses an earlier effective start;
- auxiliary demux seeks independently to the same source time;
- audio keeps a small preroll window and any packet overlapping `T`; mpv's
  initial audio sync trims samples before the first video PTS;
- a subtitle packet whose declared duration overlaps `T` is retained;
- stale auxiliary packets are dropped by the same epoch checks as video;
- EOS is emitted only after both demux iterators are exhausted and the encoder
  and Matroska muxer have drained.

Subtitle formats with stateful seek behavior, particularly PGS/VobSub, remain
a physical-device gate. If libav's indexed seek does not reproduce an active
event, the auxiliary demuxer will need codec-specific preroll rather than a
persistent whole-file sidecar.

### Client behavior

When `session_opened.aux_tracks == "muxed"`:

- do not construct or attach `/media`;
- load the epoch loopback stream by itself;
- enumerate embedded audio/subtitle tracks after every reload;
- reapply explicit track choices to the deterministic ids;
- expose audio and subtitle track/delay controls with the same behavior as the
  Android player's track sheet;
- let mpv own cache prebuffering and release a muxed epoch after its
  `playback-restart`; external desktop epochs release after the post-restart
  `audio-add` returns because `audio-pts` is unavailable while held paused.

The Qt client is the first implementation/test client. Android support follows
after the server/Qt stream and seek gates pass; Android can then remove its
per-epoch `audio-add`, HTTP range seek, audio-ready attach hold, and external
demuxer drift path for negotiated sessions.

## Why not the persistent `.mka` sidecar

The old proposal remuxed every played source to a seekable audio/subtitle-only
file in a 20 GiB LRU cache. It was compatible with the existing external-file
client, but did not achieve the actual goal on a first watch:

- building the measured 27.5 GiB movie required a complete 4–5 minute source
  read;
- playback continued using `/media` until a later seek after the build, or
  until a later watch;
- a straight watch-once movie still transferred the entire original file;
- the server simultaneously read the source for playback and for an unpaced
  cache build;
- cache invalidation, eviction, negative caching, build deduplication, polling,
  and an ffmpeg/PyAV attachment packaging decision were all new machinery.

The sidecar remains a fallback option only if a target client cannot consume a
multi-track live Matroska epoch correctly.

## Acceptance gates

### Automated

- A server without a library advertises `muxed_aux_tracks: false`; a library
  server advertises true.
- Negotiation defaults to `external` and confirms `muxed` only when requested.
  Embedded mode retains the live-header limit; cache-capable clients can keep
  muxed tracks for large supported font sets without repeating their bodies.
- A multi-track source produces one downlink video track plus all original
  audio/subtitle tracks in source order with matching codecs and metadata.
- Initial and post-seek auxiliary PTS match the source within muxer time-base
  rescaling tolerance.
- Video PTS equivalence, discontinuity, stale-epoch rejection, seek storms,
  pacing, and natural EOS remain green.
- The Qt client passes no original-media URL to mpv after muxed confirmation
  and retains the HTTP path against an old/external server.

### Qt/libmpv smoke

- Headless/offscreen playback reaches valid audio PTS from the embedded track.
- A far seek reloads once, produces audio without `audio-add`, and keeps A/V
  drift within 50 ms.
- An explicit subtitle choice, including off, survives an epoch reload.
- SSA text spanning the seek target renders with its attached font.

### Android device gate

- Thirty-minute A/V run and 25-action seek storm remain within the existing
  drift/drop limits.
- Paused seeks and PiP drift-watchdog reloads converge.
- Non-default audio and subtitle selections survive every reload.
- SSA and a real PGS/VobSub sample render correctly at and after a seek.
- `/media` receives no request during a negotiated server-file session.

## Work breakdown

1. **Implemented (initial slice):** capability/request/confirmation fields,
   `AuxiliaryTrack`, timestamp-merged bounded pipeline path, stream-copy
   muxing, attachments, and Qt opt-in.
2. **Partial:** synthetic AAC initial/seek PTS coverage and all existing
   video-only tests pass. Add synthetic subtitle fixtures and subtitle PTS
   equivalence coverage.
3. **Implemented:** the Qt offscreen tests and a real headless Qt/libmpv smoke
   confirm that the negotiated stream loads without attaching the source file.
   A live passthrough run against the multi-track library remux on 2026-08-18
   exposed two audio and ten subtitle tracks, switched to the non-default audio
   track, retained both track choices across a 60 s epoch seek, and settled at
   60.018 s with sub-millisecond reported A/V drift. The same run verified that
   a mid-play HEVC tier change reopened the session at that position while
   keeping muxed auxiliary mode.
   A rendered live timing run later found the same remux's first muxed packet
   was 35.3 MiB of attachment-heavy header data. Legacy embedded mode confirms
   `external` above 4 MiB; the new negotiated attachment cache transfers each
   verified font hash once and omits its body from every epoch header.
4. Validate SSA attachment fonts and obtain a PGS/VobSub sample for the
   stateful-subtitle gate.
5. Implement Android negotiation and delete the external attach path only for
   confirmed muxed sessions.
6. Measure client `/media` bytes, downlink bitrate, seek latency, server source
   I/O, and A/V drift on the original 27.54 GiB remux; record results here.
