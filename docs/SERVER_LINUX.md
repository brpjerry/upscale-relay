# Linux server setup

The relay server runs on Linux with the same TensorRT inference, NVENC
encoding and display mapping as on Windows. The Arch package is the supported
install; everything else in this document follows from what it sets up.

## Arch Linux release package

Download the three `upscale-relay-*.pkg.tar.zst` packages and `SHA256SUMS`
from the [GitHub release](https://github.com/brpjerry/upscale-relay/releases),
then in the download directory:

```bash
sha256sum -c --ignore-missing SHA256SUMS
yay -U ./upscale-relay-common-*.pkg.tar.zst ./upscale-relay-server-*.pkg.tar.zst
```

Include `./upscale-relay-client-*.pkg.tar.zst` in the same command if the client
is already installed (the 0.25 single-package client too), or for the player or
the optional tray GUI (below). Always upgrade every installed relay package in
one command: each requires the exact matching `upscale-relay-common`, and the
older client owns files that move to it. The packages are:

| Package | Contents |
|---|---|
| `upscale-relay-common` | `relay_protocol`, `relay_media`: modules both sides import |
| `upscale-relay-server` | `relay-server`, `relay-server-gui`, `upscale-cli`, the systemd user unit |
| `upscale-relay-client` | `relay-desktop`, `relay-client` ([client setup](CLIENT_LINUX.md)) |

Pacman installs the rest from Arch's repositories: CUDA 13 and cuDNN 9
(`cuda`, `cudnn`), FFmpeg with NVENC and libplacebo, `python-av` built on that
FFmpeg, `python-onnx`, and the NVIDIA driver's userspace (`nvidia-utils`).
CUDA is the large one: about 2.7 GiB to download, 5.4 GiB installed.

### The NVIDIA runtime (once per user)

No Arch package provides a TensorRT-capable ONNX Runtime: Arch's
`onnxruntime-cuda` is built without the TensorRT provider, and the AUR's
`tensorrt` is a major version ONNX Runtime 1.31 cannot load. So model sessions
need two pinned wheels, which one explicit command installs:

```bash
relay-server --install-nvidia-runtime
```

It downloads `onnxruntime-gpu` 1.31.0 and `tensorrt-cu13-libs` 10.16.1 (about
4 GB) into `~/.local/share/upscale-relay/runtimes/<stack>`, without their
dependencies (those are Arch packages), deletes TensorRT's builder resources
for Windows-targeted engines (2.8 GB remain), and builds a real TensorRT
session before it marks the runtime ready. It refuses to start the download
when the system CUDA libraries do not load, and names the package missing.

The server works without it: passthrough sessions need no inference, and it
logs at startup how to install the runtime. A model session without it fails
with the same instruction. A server that was already running picks the runtime
up only when it restarts: `systemctl --user restart upscale-relay-server`, or
quit and relaunch `relay-server-gui`.

The runtime is per Python minor version and stack. When Arch moves to a new
Python minor version, first rebuild or replace every installed relay package
for that interpreter (their Python dependency stops the upgrade otherwise),
then run the command again; the same goes for a release that changes the pinned
stack, which the server reports at startup as ONNX Runtime missing. The wheels
link CUDA 13 and cuDNN 9 by soname, which is why the package depends on
`libcudart.so=13` and `cudnn<10`: once Arch ships CUDA 14 or cuDNN 10,
`pacman -Syu` stops on that dependency (rather than leaving a runtime that
cannot load) until a release pins a new runtime stack.

### Running it

```bash
mkdir -p ~/.local/share/upscale-relay/models   # put .onnx models here
systemctl --user enable --now upscale-relay-server
journalctl --user -u upscale-relay-server -f
```

The user unit runs `relay-server --models-dir
~/.local/share/upscale-relay/models`. TensorRT engines are cached beside the
models, in `models/.trt_cache` (build them with an idle GPU: engines built
under load stay slow); with a read-only models folder the engine is rebuilt
at every session start instead. `systemctl --user stop` (SIGTERM) shuts the
server down cleanly: sessions close and the mDNS advertisement is withdrawn. Further options go in
`~/.config/upscale-relay/server.env`:

```bash
RELAY_SERVER_ARGS=--library /srv/video --library "/mnt/nas/My Films"
```

The variable is split on whitespace; quote a path that contains spaces, and
systemd passes it as one argument.

A user unit starts at login. To run the server on a headless box without
anyone logged in, enable lingering once: `loginctl enable-linger $USER`.

Open TCP 8590 (control and status) and 8591 (media) to clients, and UDP 5353
for mDNS discovery. With firewalld:

```bash
sudo firewall-cmd --permanent --add-port=8590-8591/tcp --add-service=mdns
sudo firewall-cmd --reload
```

`http://<server>:8590/status` reports per-session pipeline fps and per-stage
timings, as on Windows.

### Tray GUI (optional)

`relay-server-gui` is the Windows tray app, which also runs on Linux desktops
with a StatusNotifierItem tray (KDE, GNOME with the AppIndicator extension,
waybar's `tray` module on Hyprland and Sway). It borrows the player's theme,
so it needs `upscale-relay-client` installed too. It runs the server itself,
so stop the user unit first (`systemctl --user disable --now
upscale-relay-server`): only one server runs per user. Its models folder
defaults to the unit's, `~/.local/share/upscale-relay/models`, and its
settings (port, library folders) are its own. Unlike on Windows it
asks before downloading the NVIDIA runtime, and "Not now" still starts the
server for passthrough. "Start automatically when I sign in" writes an XDG
autostart entry, `~/.config/autostart/upscale-relay-server.desktop`; at sign-in
the tray waits up to a minute for the desktop's tray to appear.

## Building the packages

On Arch, from a checkout:

```bash
yay -S --needed base-devel python-build python-installer python-setuptools python-wheel
/usr/bin/python packaging/arch/prepare.py dist/pacman
cd dist/pacman
makepkg --syncdeps
```

`prepare.py` stages each package's modules with the version, Python
dependencies and commands of the root `pyproject.toml`. CI builds the same way
in an Arch container, then installs and checks each package in a fresh one.
Its runners have no NVIDIA GPU, so the server check installs the package with
CUDA, cuDNN and the driver assumed (`pacman --assume-installed`) and Arch's
CPU ONNX Runtime, and runs a passthrough and a model session through the
installed commands. TensorRT and NVENC are checked on real hardware.

## Verified on

An RTX 5090 desktop on Arch (2026-11-23): driver 620.73, `cuda` 13.5, `cudnn`
9.28, FFmpeg 9.1, Python 3.14. The runtime's TensorRT and CUDA providers load
against Arch's CUDA and cuDNN (the wheels were built against CUDA 13.0 and
cuDNN 9). A 4K lossless-HEVC session through a small test network ran on the
TensorRT worker and NVENC at ~110 fps, and the display-mapping probe found
libplacebo on Vulkan.

## Linux-specific notes

- ORT's TensorRT provider library has no RUNPATH, so it finds
  `libnvinfer.so.10` only when that is already loaded or on the
  `LD_LIBRARY_PATH` the process started with. Without either, TensorRT
  sessions quietly run on the CPU provider (the server refuses those).
  `relay-server` puts the runtime's `tensorrt_libs` on `LD_LIBRARY_PATH` for
  its inference worker, and `upscale_cli.infer` preloads the libraries in any
  process that runs TensorRT itself.
- The runtime directory goes first on `sys.path`, so its onnxruntime wins over
  a system `python-onnxruntime*`. It holds nothing else, so it shadows no
  other package.
- The inference worker attaches the parent's shared memory without
  registering it with its own resource tracker. On Linux a plain attach let
  the worker's tracker unlink the parent's blocks when the worker exited, so
  a replacement worker after a crash could not attach to them.
