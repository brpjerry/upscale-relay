# upscale-relay

Play local/SMB video on a thin client while a GPU server upscales every frame
through .onnx super-resolution models in real time — video streams to the
server as-is, comes back upscaled in the selected quality tier, and the client
plays it in sync with the original file's audio and subtitles. The server can
also host the source through its optional media library.

- **[Documentation](docs/README.md)** — documentation index
- **[Architecture and roadmap](docs/PLAN.md)** — current implementation,
  design decisions, and remaining work
- **[Wire protocol](docs/PROTOCOL.md)** — control channel, media framing, and
  seek/epoch semantics
- **[Linux setup](docs/CLIENT_LINUX.md)** — desktop client installation
- **[Android client](https://github.com/brpjerry/upscale-relay-android)** — Kotlin/Compose client
  (separate repository)

## Layout

| package | role |
|---|---|
| `upscale_cli/` | offline pipeline + ONNX inference stages (TensorRT/CUDA/DirectML), bench harness |
| `relay_protocol/` | shared wire-format code |
| `relay_server/` | streaming server: session manager, 3-stage pipeline, muxed downlink |
| `relay_client_core/` | reusable client library + mock CLI |
| `desktop_client/` | PySide6 + libmpv desktop player |
| [upscale-relay-android](https://github.com/brpjerry/upscale-relay-android) | Kotlin/Compose client + libmpv/MediaCodec (separate repo) |

## Quick start (dev box)

```
pip install -e .[gui]           # plus onnxruntime-gpu/-directml for the server
relay-server --models-dir models --ep tensorrt
relay-desktop
```

On Windows the server also ships as a double-click tray app: `relay-server-gui`
(the `upscale-relay-server-gui.exe` release binary, installable with the
`.[server-gui]` extra) starts the server from its last-saved configuration and
drops an icon in the notification area. Its configuration pane sets the
execution provider, control port, media library folder, and models folder, then
restarts the listeners in place. The headless `relay-server` CLI is unchanged.

Add `--library <folder-or-UNC-path>` to browse and play server-hosted media.
The final post-ONNX scale defaults to Lanczos; set a different server default
with `--resize-algorithm area` (or choose a per-session override in a client).
Clients offer six bandwidth-labeled NVENC HEVC choices, True Lossless HEVC,
and optional GPU debanding; desktop also offers Lossless FFV1. True Lossless
HEVC defaults to the P4 low-delay profile. Server-wide experimental overrides
remain available through `--lossless-hevc-profile`; see
[quality tier notes](docs/TIER_NOTES.md#lossless-hevc-server-profiles).

Models are user-supplied `.onnx` files dropped into `models/` with a small
JSON manifest (`{"scale_factor": 2, "channel_order": "rgb", "value_range": [0.0, 1.0]}`).
The `mpv-dev/` folder (Windows) holds the libmpv DLL; Linux uses the distro's
libmpv. Both are gitignored — see [the documentation](docs/README.md).
