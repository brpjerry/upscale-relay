# upscale-relay

Play local/SMB video on a thin client while a GPU server upscales every frame
through .onnx super-resolution models in real time — video streams to the
server as-is, comes back upscaled in the selected quality tier, and the client
plays it in sync with the original file's audio and subtitles. The server can
also host the source through its optional media library; capable desktop
clients receive original audio/subtitles inside each relay epoch and cache
subtitle fonts by content hash instead of reopening the full source file.

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

The `nvidia` extra moved from CUDA 12 to CUDA 13, which renamed most of its
wheels (`nvidia-cublas-cu12` became `nvidia-cublas`, and so on). pip sees no
upgrade relationship between the old and new names, so an environment created
before that change keeps both stacks installed. The two TensorRT builds are the
problem: they ship the same `tensorrt_libs/nvinfer_10.dll` for different CUDA
majors. Install into a fresh virtualenv, or drop the superseded packages first:

```
pip uninstall -y tensorrt-cu12 tensorrt-cu12-libs tensorrt-cu12-bindings \
    nvidia-cublas-cu12 nvidia-cuda-nvrtc-cu12 nvidia-cuda-runtime-cu12 \
    nvidia-cudnn-cu12 nvidia-cufft-cu12 nvidia-nvjitlink-cu12
```

### The GPU environment (`.venv-cuda`)

The GPU server runs from its own virtualenv, `.venv-cuda/` in the repository
root — gitignored, and roughly 5.7 GB across 25 000 files once the NVIDIA
stack is in it. Python 3.12+; 3.14 is what the current server box uses, and
what the Windows release binaries are built with.

```powershell
py -3.14 -m venv .venv-cuda
.\.venv-cuda\Scripts\python.exe -m pip install --extra-index-url https://pypi.nvidia.com -e ".[gui,nvidia]" pytest
```

Verify it before trusting a benchmark: the provider list must contain
`TensorrtExecutionProvider`, or the server silently falls back to CPU.

```powershell
.\.venv-cuda\Scripts\python.exe -c "import onnxruntime as ort; print(ort.get_available_providers())"
.\.venv-cuda\Scripts\upscale-cli.exe --help
.\.venv-cuda\Scripts\python.exe -m pytest tests -q
```

**A virtualenv is not relocatable.** Moving or renaming `.venv-cuda`, or the
repository directory above it, breaks it in ways that read as something else:

- The `Scripts\*.exe` console-script launchers embed the absolute path of the
  interpreter that generated them. After a move they exit with status 1 and
  print *nothing* — indistinguishable at a glance from a CLI that crashed on
  startup. `Scripts\python.exe` keeps working, because it resolves its home
  through the adjacent `pyvenv.cfg`, so `python -m pytest` and `python -m pip`
  succeed while `pytest.exe` and `pip.exe` fail. That split is the tell.
- `pyvenv.cfg` and `Scripts\activate{,.bat,.fish}` record the old prefix.
- An editable install records the *source tree's* absolute path in
  `Lib\site-packages\__editable___*_finder.py`. That path tracks the
  repository, not the venv, so it survives a venv-only move and breaks on a
  repository move.

Recreating the venv is the supported fix. When redownloading the multi-gigabyte
NVIDIA wheels is not worth it, repair in place instead — reinstall the project
to regenerate its five launchers and the editable path map, then rewrite the
interpreter path left in every other launcher and in the activate scripts:

```powershell
.\.venv-cuda\Scripts\python.exe -m pip install -e . --no-deps --no-build-isolation
.\.venv-cuda\Scripts\python.exe tools\relocate_venv.py "C:\old\path\.venv-cuda" .venv-cuda
```

`tools/relocate_venv.py` takes `--dry-run` and reports each file it would
touch. `--no-build-isolation` keeps the reinstall offline by using the
setuptools already in the venv.

On Windows the server also ships as a double-click tray app: `relay-server-gui`
(the `upscale-relay-server-gui.exe` release binary, installable from source with
the `.[server-gui,nvidia]` extras) starts the server from its last-saved
configuration and drops an icon in the notification area. The downloadable ZIP
includes the lightweight ONNX graph tooling required for the fast
`uint8-wrapped` TensorRT path, while staying small: on first launch the program downloads the pinned TensorRT
10.16/CUDA 13.3 stack into `%LOCALAPPDATA%\upscale-relay\runtimes`. It verifies
TensorRT, CUDA, and CPU providers before marking that versioned runtime ready;
an interrupted or failed installation is retried on the next launch. The GUI
shows setup progress, while the console build prints it. Its configuration pane sets the
execution provider, control port, media library folder, models folder,
file logging, and whether the tray app starts automatically at Windows sign-in
(an `HKCU` Run-key entry, GUI build only), then restarts the listeners in
place. GUI logging defaults on and
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
