# Server-side media library

Status: **implemented**, except for shared-mount path mapping.

The server can expose a local folder, mounted share, or Windows UNC path as a
media library. Clients browse the server tree and ask the server to demux and
upscale a selected file. Capable clients negotiate original audio/subtitles in
the epoch downlink; the Range-capable HTTP endpoint remains the compatibility
path for older/external-mode clients.

## Run it

```powershell
relay-server --models-dir models --ep tensorrt --library \\nas\media\Videos
```

The post-ONNX resize uses Lanczos by default. Add, for example,
`--resize-algorithm area` to change the server default; desktop, headless, and
Android clients can select any algorithm advertised by the server per session.

Local Windows/Linux paths and OS-mounted network shares work the same way. If
`--library` is omitted, no library capability or routes are advertised and the
desktop retains its local-only browser appearance.

## Implemented behavior

### Server

- `relay_server.library.MediaLibrary` resolves every request beneath the
  configured root and rejects traversal and non-playable files.
- `GET /library?path=<relative-directory>&limit=100&cursor=<offset>&sort=<key>`
  returns one sorted page of that directory's immediate children. The response
  carries an opaque `next_cursor` (or `null`); `path` is empty for the root.
  Omitted query parameters use root/100/initial-cursor/`name` defaults.
  Recursive full-tree responses are not supported.
- `sort=name` (the default) orders directories first, then case-folded
  lexicographic names. `sort=mtime` orders directories first, then descending
  modification time (newest first) with a case-folded name ascending tiebreak;
  directories also order by descending mtime among themselves. Unknown keys
  are an HTTP 400, not a silent fallback. Clients must not mix pages fetched
  with different `sort` values; refetch from offset 0 when switching. See
  [LIBRARY_SORT_PLAN.md](LIBRARY_SORT_PLAN.md) for rationale.
- `GET /media/<relative-path>` serves the original file with HTTP Range
  support through aiohttp `FileResponse`.
- The `capabilities` message includes `library: true` and
  `library_sort: ["name", "mtime"]` while the feature is configured
  (`library_sort` is `[]` otherwise).
- `muxed_aux_tracks: true` advertises the opt-in stream-copy path.
  `attachment_cache: 1` additionally permits clients to fetch verified fonts
  once by content hash. Legacy/embedded requests still fall back to `external`
  when attachments exceed 4 MiB; cached mode avoids that repeated header.
- `open_session.source` accepts `{type: "server_file", path: "..."}`.
- Server-file sessions create a shared `relay_media.VideoTrack` locally and do
  not allocate or wait for an uplink attachment.
- The server derives `time_base`, duration, frame rate, codec parameters, and
  extradata from the selected file and returns the playback metadata in
  `session_opened`.
- Seek commands operate on the server-side `VideoTrack`; downlink epoch,
  discontinuity, decode-and-discard, and pacing rules are unchanged.

### Client core and desktop UI

- `RelayClient.fetch_library_page()` loads immediate directory pages and
  `media_url()` builds the Range URL for original tracks.
- The sidebar becomes a tab widget only when a connected server advertises a
  library. The existing Local tree is unchanged; the Server tab fetches a
  directory when it is expanded and adds a page only when requested, supports
  refresh, and reports empty/error states.
- The Android browser uses the same shallow directory pages, keeps each
  directory's cursor while navigating, and appends another page from its
  `Load more` footer without discarding already loaded entries.
- Double-clicking a server file opens a `server_file` session without creating
  a client `VideoTrack` or uplink sender.
- The Qt client requests muxed auxiliary tracks from capable servers and does
  not open `/media` after `session_opened` confirms them. Older/external-mode
  sessions still attach `/media` for audio/subtitles.
- Local fallback is hidden for server files because the client has no local
  source to play directly.

Coverage lives in `tests/test_server_library.py` and
`tests/test_server_library_gui.py`, including path sandboxing, HTTP Range,
server-source PTS equivalence, seeks, capability-driven UI, and media URLs.

## Auxiliary tracks in the main pipeline

The default/legacy pipeline remains video-only. When a server-file client opts
in, a separate seekable auxiliary demuxer feeds original audio/subtitle packets
through the same bounded epoch pipeline without decoding them; the finish
thread stream-copies them into the fresh Matroska container. The auxiliary
demuxer anchors seeks on the source video stream's keyframe cues
while emitting only auxiliary packets. Matroska commonly does not index its
audio stream, so using audio as the anchor can otherwise force a long linear
scan before any post-seek video reaches the pipeline. Attachments remain
embedded for old clients. Cache-capable desktop clients receive a sanitized
hash manifest, download missing font objects through a session bearer token,
verify them before atomic publication, and point libass at a per-session font
view; cached epoch headers omit the bodies. Unsupported attachment types retain
the embedded/external fallback. Relay seeks replace the whole container, so a
confirmed muxed session does not need a seekable external demuxer. See
[AUDIO_SIDECAR_PLAN.md](AUDIO_SIDECAR_PLAN.md).

## SMB and network shares

No SMB protocol implementation is included. Both supported operating systems
expose mounted shares as ordinary filesystem paths:

- Linux: mount the share, then pass its mount path to `--library`.
- Windows: prefer a UNC root such as `\\host\share\Videos`. `pathlib` and
  PyAV use the Windows redirector and inherit the interactive user's existing
  credentials.
- Mapped Windows drives also work for interactive sessions, but drive mappings
  are per logon session and are unreliable for a future Windows service.

The application does not store SMB credentials and does not use ffmpeg's
`smb://` protocol.

## Remaining planned work: shared-mount mapping

HTTP delivery is the only implemented *external-file* original-track path for
server files; negotiated clients normally use muxed auxiliary tracks instead.
When both machines mount the same share under different roots, a future option
can map one library-relative identity to each machine's local root, for example:

```text
library-relative: Shows/Episode.mkv
server root:      \\nas\media
client root:      /mnt/nas/media
```

The client could then attach `/mnt/nas/media/Shows/Episode.mkv` for
audio/subtitles while the server reads `\\nas\media\Shows\Episode.mkv` for
video. This mapping is intentionally not implemented yet; no current setting
or protocol field should be documented as if it exists.

Other operational questions remain outside the shipped feature: credentials
for an unattended service and seek/readahead behavior on high-latency shares.
