# Server-side media library

Status: **implemented**, except for shared-mount path mapping.

The server can expose a local folder, mounted share, or Windows UNC path as a
media library. Clients browse the server tree, ask the server to demux and
upscale a selected file, and let mpv read the original audio/subtitles through
the server's Range-capable HTTP endpoint.

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
- `GET /library?path=<relative-directory>&limit=100&cursor=<offset>` returns
  one sorted page of that directory's immediate children. The response carries
  an opaque `next_cursor` (or `null`); `path` is empty for the root. Omitted
  query parameters use those root/100/initial-cursor defaults. Recursive
  full-tree responses are not supported.
- `GET /media/<relative-path>` serves the original file with HTTP Range
  support through aiohttp `FileResponse`.
- The `capabilities` message includes `library: true` while the feature is
  configured.
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
- libmpv attaches `/media` as both `audio-file` and `sub-files`, so original
  audio and subtitles remain aligned by absolute PTS.
- Local fallback is hidden for server files because the client has no local
  source to play directly.

Coverage lives in `tests/test_server_library.py` and
`tests/test_server_library_gui.py`, including path sandboxing, HTTP Range,
server-source PTS equivalence, seeks, capability-driven UI, and media URLs.

## Why the main pipeline did not change

The pipeline consumes encoded video packets and produces a streaming Matroska
downlink. Whether those input packets come from the client's framed uplink or a
server-local `VideoTrack` does not affect decode, inference, fit/cover,
encoding, muxing, epochs, or pacing.

Audio and subtitles deliberately remain outside the upscaled downlink. Muxing
them into every epoch would complicate seek remuxing and duplicate a path mpv
already handles correctly.

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

HTTP delivery is the only implemented original-track path for server files.
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
