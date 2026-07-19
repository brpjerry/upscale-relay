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
- **[Windows setup](docs/CLIENT_WINDOWS.md)** — desktop/libmpv and development
  environment
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
pip install --extra-index-url https://pypi.nvidia.com -e ".[gui,nvidia]"
relay-server --models-dir models --ep tensorrt
relay-desktop
```

On Windows the server also ships as a double-click tray app: `relay-server-gui`
(the `upscale-relay-server-gui.exe` release binary, installable from source with
the `.[server-gui,nvidia]` extras) starts the server from its last-saved
configuration and drops an icon in the notification area. The downloadable ZIP
includes the lightweight ONNX graph tooling required for the fast
`uint8-wrapped` TensorRT path, while staying small: on first launch the program downloads the pinned TensorRT
10.13/CUDA 12.9 stack into `%LOCALAPPDATA%\upscale-relay\runtimes`. It verifies
TensorRT, CUDA, and CPU providers before marking that versioned runtime ready;
an interrupted or failed installation is retried on the next launch. The GUI
shows setup progress, while the console build prints it. Its configuration pane sets the
execution provider, control port, media library folder, models folder, and
file logging, then restarts the listeners in place. GUI logging defaults on and
writes `upscale-relay-server.log` to the user's Documents folder. While a
session is active it records a performance snapshot every two seconds plus a
final snapshot on close. The headless
`relay-server` CLI continues to log to its console.

The first-run NVIDIA download is several gigabytes and needs an NVIDIA driver,
network access, and enough temporary disk space. It happens on the user's
machine—not while CI builds or packages the release—and is reused until the
pinned runtime stack changes. `--help` remains offline and does not trigger it.

Add `--library <folder-or-UNC-path>` to browse and play server-hosted media.
The final post-ONNX scale defaults to Lanczos; set a different server default
with `--resize-algorithm area` (or choose a per-session override in a client).
Clients offer six bandwidth-labeled NVENC HEVC choices, True Lossless HEVC,
and optional GPU debanding; desktop also offers Lossless FFV1. True Lossless
HEVC defaults to the P4 low-delay profile. Server-wide experimental overrides
remain available through `--lossless-hevc-profile`; see
[quality tier notes](docs/TIER_NOTES.md#lossless-hevc-server-profiles).

Models are user-supplied `.onnx` files dropped into `models/`. When a matching
JSON manifest is absent, the server creates one with RGB `[0, 1]` defaults and
a 2x scale. Filename markers such as `3x`/`x3` or `4x`/`x4` select that scale
instead. An explicit manifest can override the defaults
(`{"scale_factor": 2, "channel_order": "rgb", "value_range": [0.0, 1.0]}`).
On Windows source installs, the desktop client and full GUI tests require
`mpv-dev/libmpv-2.dll` from an `mpv-dev-x86_64` archive; the server release
binaries do not. See the exact [Windows setup](docs/CLIENT_WINDOWS.md). Linux
uses the distro's libmpv package.
