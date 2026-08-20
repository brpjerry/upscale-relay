# Linux client setup

The desktop client (PySide6 + libmpv + `relay_client_core`) is pure Python on
top of two native pieces: libmpv and PyAV's bundled ffmpeg. Nothing in it is
Windows-specific. Python 3.12+ required (3.13/3.14 fine); the Windows server
binaries are built on 3.14, but the client is not tied to that version.

## 1. System packages

Debian/Ubuntu (24.04 names):

```bash
sudo apt install python3-venv libmpv2 libxcb-cursor0
```

- `libmpv2` — libmpv runtime; python-mpv finds `libmpv.so.2` via ldconfig.
  If `import mpv` still can't find it, add `libmpv-dev` (provides the
  unversioned `libmpv.so` symlink).
- `libxcb-cursor0` — required by Qt 6.5+ on X11; PySide6 wheels bring the
  rest of Qt themselves.

Fedora: `sudo dnf install mpv-libs libxcb` · Arch: `sudo pacman -S mpv`

For hardware decode of the HEVC tiers (optional but recommended), install
your GPU's VA-API/NVDEC userspace:
- Intel: `intel-media-va-driver-non-free` (Ubuntu) / `intel-media-driver`
- AMD: `mesa-va-drivers`
- NVIDIA: the proprietary driver includes NVDEC; mpv uses it via `hwdec=auto-safe`.

FFV1 has no hardware decoder on any platform — it always decodes on the CPU.

The embedded Qt/OpenGL player uses mpv's safe copy-back hardware decode mode on
Linux. In particular, it overrides `hwdec=vaapi` from a user's `mpv.conf` with
`auto-copy-safe`: a captured Intel iHD crash landed in
`paintGL → mpv_render_context_render → vaSyncSurface` when the zero-copy VA-API
surface was retired during live playback. Decode still runs on the GPU; only
the decoded frame is copied before Qt renders it. Use `--no-hwdec` to disable
hardware decode completely.

## 2. Get the code onto the laptop

Clone or pull the repository normally. To copy an existing working tree from
the Windows machine instead, exclude Windows/server-local artifacts:

```bash
rsync -av --exclude '.venv*' --exclude 'mpv-dev' --exclude 'models' \
      --exclude '*.mkv' --exclude '__pycache__' \
      user@windows-box:/c/Users/b580v/Documents/CC/video-upscale-relay/ \
      ~/video-upscale-relay/
```

(or zip the directory minus those folders and copy it any way you like —
`models/`, `mpv-dev/`, and the venvs are server/Windows-only).

## 3. Install

```bash
cd ~/video-upscale-relay
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[gui]"
```

The `gui` extra pulls PySide6, qasync, and python-mpv on top of the base
deps (av, numpy, aiohttp). No onnxruntime/CUDA/TensorRT on the client —
inference is the server's job.

## 4. Run

```bash
relay-desktop
```

- **Wayland sessions** work natively through mpv's render API. If a driver has
  an OpenGL/Wayland interop problem, `QT_QPA_PLATFORM=xcb relay-desktop` is a
  useful XWayland fallback.
- Enter the server as `<windows-box-ip>:8590` in the toolbar and Connect.
- Settings persist in `~/.config/upscale-relay/`.

When a capable server-library session confirms muxed auxiliary tracks, the
desktop client gets audio and subtitles from the relay Matroska stream and does
not reopen the full source through `/media`. Subtitle fonts are downloaded by
SHA-256 into the Qt standard cache location, verified before use, and reused
across seeks and later sessions until bounded eviction. A seek never downloads
the font bodies again. Local files and old/external server confirmations retain
the original-media attach path.

## 5. Open the server's firewall (on the Windows box, once)

The server listens on 8590 (control/status) and 8591 (media). From an
admin PowerShell on the server machine:

```powershell
New-NetFirewallRule -DisplayName "upscale-relay" -Direction Inbound `
  -Protocol TCP -LocalPort 8590,8591 -Action Allow
```

Then verify from the laptop: `curl http://<windows-box-ip>:8590/status`

## 6. Client flags (all optional)

| flag | effect |
|---|---|
| `relay-desktop --debug` | enable faulthandler crash dumps in the client |
| `relay-desktop --mpv-osc` | re-enable mpv's native OSC overlay (known to destabilize seeks) |
| `relay-desktop --no-hwdec` | force software video decode |
| `relay-desktop --trace` | verbose consume-loop tracing to stderr |
| `relay-desktop --mpv-scripts` | load user mpv scripts (off by default) |

Run `relay-desktop --help` for all client options, including the headless and
isolated-settings flags used by smoke tests.

## 7. Bandwidth expectations

Wired GbE handles the bandwidth-labeled HEVC choices. The lossy selector spans
roughly `~50` through `~350 Mbps` P95 classes; constant-QP output remains
content-dependent. True Lossless HEVC can exceed those estimates and wants a
strong link. `lossless-ffv1` (~200-800 Mbps) is realistically Ethernet-only —
and is also CPU-decoded on the laptop. True Lossless HEVC is the recommended
lossless tier for playback.
