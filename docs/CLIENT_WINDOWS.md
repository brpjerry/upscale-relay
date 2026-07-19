# Windows client and development setup

The Windows server release binaries do **not** need libmpv. libmpv is required
only when running the desktop client or the complete GUI test suite from a
source checkout.

## 1. Create the Python environment

From PowerShell in the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[gui]" pytest
```

The `gui` extra installs PySide6, qasync, and the `python-mpv` binding. The
binding does not include the native libmpv DLL; install that separately below.

## 2. Install libmpv for the desktop client and GUI tests

1. Open the [mpv Windows installation list](https://mpv.io/installation/) and
   follow its link to the current shinchiro Windows builds.
2. Download the archive named
   `mpv-dev-x86_64-<date>-git-<hash>.7z`. Use the `mpv-dev` archive, not the
   similarly named `mpv-x86_64` player archive. The `x86_64-v3` build is also
   available, but the plain `x86_64` build is the compatible default.
3. Extract `libmpv-2.dll` from the archive directly into the repository's
   `mpv-dev` directory. The final layout must be:

```text
upscale-relay\
├── desktop_client\
├── mpv-dev\
│   └── libmpv-2.dll
└── pyproject.toml
```

Do not leave the DLL inside an additional extracted subdirectory. `mpv-dev`
is gitignored because the DLL is roughly 100 MB.

The desktop client automatically adds `<repo>\mpv-dev` to the process DLL
search path before importing `python-mpv`; no system-wide installation or
permanent `PATH` change is needed.

Verify the environment from the repository root:

```powershell
python -c "from desktop_client.mpv_view import mpv; print('libmpv', mpv._mpv_client_api_version())"
```

A tuple such as `libmpv (2, 5)` confirms that Python loaded the native DLL. If
the command reports that it cannot find `mpv-1.dll`, `mpv-2.dll`, or
`libmpv-2.dll`, recheck the archive choice and exact file layout above.

## 3. Run the client and tests

```powershell
relay-desktop
```

For the complete local test suite, including the optional desktop modules:

```powershell
$env:RELAY_LOSSLESS_HEVC_PROFILE = "x265-ultrafast"
python -m pytest tests -q
```

CI installs only the base dependencies in its core test job, so optional GUI
tests are skipped there. A developer environment with `.[gui]` installed will
collect those tests and therefore must also have `mpv-dev\libmpv-2.dll`.

## 4. Server-only Windows use

The downloadable `upscale-relay-server` and `upscale-relay-server-gui`
packages are intentionally small and do not contain the multi-gigabyte NVIDIA
stack. On first launch they install the pinned TensorRT 10.13/CUDA 12.9 runtime
under `%LOCALAPPDATA%\upscale-relay\runtimes`; the GUI shows a progress window
and the console build prints progress. Setup verifies TensorRT, CUDA, and CPU
providers before publishing the versioned runtime, so a partial download is
never used and is retried on the next launch. An NVIDIA driver, network access,
and several gigabytes of download and temporary disk space are required, but no
separate CUDA Toolkit or TensorRT installation is needed. CI does not download
or package the NVIDIA stack. The server packages do not load
`desktop_client.mpv_view` and do not require libmpv.

If setup fails, the GUI shows the actual installer error and the log path; its
Close button exits the application immediately. Relaunching retries from a
clean staging directory.

The tray GUI's configuration window includes a **Write server log** checkbox,
enabled by default. It applies immediately and writes normal server messages,
Python fault stacks, and native crash attribution to:

```text
%USERPROFILE%\Documents\upscale-relay-server.log
```

The checkbox shows the resolved path, including a redirected Documents folder.
When enabled, the log includes a `relay.stats` snapshot every two seconds for
each active session and a `FINAL` snapshot at teardown: pipeline/inference FPS,
per-stage milliseconds, output and encoder settings, queue depths, client
buffer/backpressure state, and frame counts.
The headless console server logs to its terminal instead.
