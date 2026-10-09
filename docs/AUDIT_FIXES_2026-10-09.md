# October 2026 audit fixes

Findings: `docs/CODE_REVIEW_2026-10-09.md` (baseline `ae8dec4`, report
`65bc736`). Three sessions fixed them in parallel, each finding reproduced
before its fix and given a regression test that failed on the old code:

- client (Linux laptop): `fix/audit-client-2026-10-09`
- server (Windows RTX 5090 box): `fix/audit-server-2026-10-09`
- Android (separate repository): `fix/audit-android-2026-10-09`

The client and server branches land together: the desktop sends a source's
sample aspect ratio (finding 7) through the shared `relay_media` change on the
server branch. They merge without conflicts.

## Decisions

Trade-offs went to the user:

| Question | Decision |
| --- | --- |
| Anamorphic uplinks (7) | Optional `open_session.video.sample_aspect_ratio`; absent means 1:1, no protocol version bump. The server falls back to the H.264/HEVC parameter sets when a client sends none. |
| Untagged colour (16) | Resolve unspecified matrix/primaries/range/transfer with mpv's own guess from the source geometry, and write them into the output. |
| History per server (Android F05) | Optional `capabilities.server_id` (1-64 of `[A-Za-z0-9_-]`, persisted in the server's state directory); clients fall back to `host:port`. Existing server-file history starts fresh on both clients; local-file history carries over on the desktop. |
| `relay-desktop --debug` on Windows | Dump only the faulting thread, as the Windows tests now do (see below). |
| Android local read errors | Android's MediaExtractor reports a mid-file read error as end of stream; left as a documented known issue in that repository. |

## Client

| Finding | Result | Commit |
| --- | --- | --- |
| 5: local/SMB read failure buffers forever | A failed source read ends the downlink with "Could not read the source file: …": the desktop shows it and stops through the normal teardown, the CLI exits 1. Cancelled or superseded pumps and a closed uplink stay quiet. | `7a072ef` |
| 8: input.conf pause lost after seek/restart | mpv pauses made by a binding become the caller's pause intent (toolbar, server, restart snapshot); the player's own load-time holds and stop-time restores are excluded. | `1abf44b` |
| 9: Linux inference import fails with `tensorrt_libs` | The import-time DLL setup runs on Windows only and joins PATH with `os.pathsep`. | `f4e6356` |
| 10: concurrent startup evicts another player's fonts | Each open links every object into its leased view as soon as it is found or published, under the cross-process cache lock; eviction removes only `objects/` names. | `bcc8712` |
| 11: interrupted font downloads exceed the cache limit | Every publication evicts under the same lock, protecting the open's manifest, so the store stays bounded whatever happens to the rest of the open. | `0323c00` |
| 12: Windows local autoplay stops after one file | Local basenames use the native `Path`; server paths keep `PurePosixPath`. | `59c3773` |
| 13: playback during initial library load strands the browser | The listing finishes on its own task when the connect transition is cancelled; a cancelled refresh leaves a refreshable tab and resets browser-state persistence. | `ff7a3b3` |
| 15: obsolete uplink worker ends the new seek early | The iterator is claimed on the event loop; only the newest pump of the current epoch may send EOS. | `b319111` |

Follow-ups found during the work:

| Change | Commit |
| --- | --- |
| Two desktop tests compared path strings and failed on Windows; they compare `Path` values. | `07bc03d` |
| Desktop history and browser state keyed by `server_id` (version 2 history record). | `5cd14dd` |
| Windows tests crashed with an access violation in 2 of 8 desktop playback sequences: LuaJIT (mpv's built-in scripts) raises and catches SEH `0xe24c4a02`, and Windows faulthandler dumps every thread, unsynchronized, on each one. `tests/conftest.py` dumps only the faulting thread there: 0 of 16 afterwards, with LuaJIT still throwing 118-139 times per run. | `5c5b7af` |
| `relay-desktop --debug` does the same on Windows. | `f76bca0` |

## Server

| Finding | Result | Commit |
| --- | --- | --- |
| 1: 16 GiB of compressed input per session | The pipeline input queue bounds queued payload bytes (128 MiB, video and server-file auxiliary packets alike) as well as packets; feeds stop waiting once the pipeline is closed. | `ac1e912` |
| 2: cancelled seeks drop the new epoch's audio/subtitles | The source loop claims its auxiliary-iterator generation on the event loop (`AuxiliaryTrack.reserve()`, `packets(generation=)`), so a superseded worker cannot retire the new epoch's iterator. | `fcbce18` |
| 3: VFR timestamps retimed by encoding | Relay and offline encoders take the source time base through the shared `add_video_encoder_stream`, for every tier and lossless-HEVC profile. | `1daa1eb`, `39eb1dd` |
| 4: TensorRT rejects 4K sources | The worker's shared memory is sized beyond 2560x1440, so 4K sources reach the worker's own tiling (~123 ms/frame with AnimeJaNai). | `34344d4` |
| 6: FFV1 sources fail to decode | Relay decoders get the source's coded width and height. | `f4a7435` |
| 7: anamorphic sources distorted | Fit and cover use the displayed shape and output is square pixels; server-file sessions and the desktop uplink fill the new field from `relay_media`. | `1de9ded` |
| 14: mDNS failures leak Zeroconf | The advertiser owns its `AsyncZeroconf` from creation and closes it on failed or cancelled registration and failed unregistration. | `e50175f` |
| 16: colour signalling lost | Output colour is tagged and converted from one resolved source description (`upscale_cli.color.VideoColor`); verified through NVENC, x265, FFV1 and TensorRT within one level. | `a7dd8b9` |
| 17: muxed audio tails held until EOS | Auxiliary packets are drained after each one once the epoch's first video chunk is out (`NO_TS` chunks). | `b23b2f5` |
| 18: invalid tile sizes return unwritten output | Tile sizes not larger than the overlap, and negative or odd overlaps, are rejected before any model session or output exists. | `9ea6e98` |
| 19: worker recovery accepts CPU | The GPU-provider check is applied after every worker READY, crash replacements included; a CPU replacement is reaped and fails the session. | `4f36e94` |

Follow-ups found during the work:

| Change | Commit |
| --- | --- |
| Instance-lock tests compare against the PID the holder reports (the Windows virtualenv launcher's child), not `Popen.pid`. | `5d9a3ee` |
| Stable per-install `capabilities.server_id` in a `server-id` file in the per-user state directory. | `b95e94c` |
| Without a client `sample_aspect_ratio`, the server reads it from H.264 SPS VUI (`relay_server/source_aspect.py`) or HEVC parameter sets before announcing the output size. | `a2ec997` |
| CLAUDE.md rules for the encoder time base and colour range, and the aspect fallback. | `7cf56f7` |

The Windows `.venv-cuda` moved to PyAV 19.0.1 (matching the release build),
NumPy 2.5.3, PySide6 6.12.0, aiohttp 3.14.4 and zeroconf 0.151.5. The pinned
NVIDIA stack (ONNX Runtime 1.28, TensorRT 10.16, CUDA 13.3) is unchanged.

## Android

The Android audit is `docs/CODE_REVIEW_AUDIT_2026-10-09.md` in that
repository; commits are on its `fix/audit-android-2026-10-09`.

| Finding | Result | Commit |
| --- | --- | --- |
| F01 | An open made while a launch or wake connect waits for the network keeps its file; the connect clears only older playback. | `c261431` |
| F02 | A length-prefixed NAL sample whose first length looks like a start code is converted; whole samples are parsed with header checks. | `f636a99` |
| F03 | The loopback's player-connect window starts when the endpoint is handed out, not at bind, so slow preparation or seek acknowledgements no longer fail the session. | `df38afc` |
| F04 | The diagnostic-log setting is restored at launch and applied by backup imports. | `733bc56` |
| F05 | History, watched flags and recents are scoped by `server_id`, falling back to normalized `host:port`. | `e585c8f`, `9bf37ab` |
| F06 | A late child-directory load lands only on the listing it was opened from instead of undoing Up. | `25758aa` |
| F07 | A sort change drops old-order cursors and refetches the Up chain, so paging never mixes orders. | `20ba049` |
| F08 | Settings shows connection errors and marks invalid host/port fields (TalkBack live region). | `614b0dc` |
| F09 | Player controls stay up while the seek bar is dragged. | `d80c159` |
| F10 | "Play original" after a failed first relay open keeps progress, resume and end-of-file handling. | `38ea270` |
| Finding 7 | Local files send `sample_aspect_ratio` (MP4 via MediaExtractor, MKV display sizes via a Matroska reader); bitstream-only aspect is left to the server. | `2655f3a`, `ca45724` |

Against the server branch on `:8690`, Android verified `server_id`-scoped
history, three anamorphic clips at 4:3 (one with bitstream-only aspect) and SD
colour relayed identically to direct playback (the baseline gave 5:4 and a
shifted colour). Host tests 128 of 128; device suite 17 of 18 (the remaining
one is the manual `Phase4PtsDeviceTest`).

## Verification

Suites: the client branch passed 461 (Linux) and 460 of 462 (Windows; the two
failures were the server's instance-lock tests, fixed on its branch). The merge
of both branches passed **545, 5 skipped** on Linux and **556, 2 skipped** on
Windows, clearing all six Windows failures the audit recorded.

End to end, the Linux laptop client ran against the real Windows relay
(`192.168.0.115:8690`, TensorRT, NVENC, library on SMB), first on the unfixed
baseline and then on the server branch, from the merged tree:

| Check | Baseline | Fixed |
| --- | --- | --- |
| Uplink playthrough, seek storm, overlapping seeks, AnimeJaNai model | pass | pass, exact source PTS |
| Injected source read error (5) | silent hang | ends at once with the error |
| Two player processes on a full font cache (10) | 1 of 5 rounds `FileNotFoundError` | 0 of 20 |
| Cancelled font opens across two files, 11 MB limit (11) | peak 15.9 MB | peak 10.99 MB |
| Server-file muxed seek storm, 5 seeks 30 ms apart (2, 17) | — | final epoch: audio and subtitles to the end, one discontinuity, audio-only `NO_TS` chunks |
| 720x576 SAR 16:15 uplink (7), with and without the field | — | 1440x1080, SAR 1:1 |
| Untagged 640x480 and full-range BT.709 sources (16) | — | tagged as mpv would guess / carried; decoded RGB identical to the source |
| 3840x2160 uplink through TensorRT (4) | — | 12 of 12 frames |
| `server_id` | — | announced, stable across restarts |

A visible Wayland `MainWindow` against the same server passed 10 of 10: library
listing, `server_id`-keyed history and browser state, cached fonts, an
input.conf pause across a relay seek and a settings restart, resume by binding,
and the source-read error shown with playback stopped. Every run left the
server with no sessions and no native teardown error.

Not covered: sustained throughput, audible sync, full-length playback, and the
previously reported intermittent native GPU crash.
