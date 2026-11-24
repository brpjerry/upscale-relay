"""Installer for the per-user NVIDIA inference runtime.

Windows: the release executable deliberately does not contain the
multi-gigabyte CUDA, cuDNN, TensorRT, and ONNX Runtime wheels.  It carries
pip's installer code and uses it to build a versioned, per-user runtime on
first launch.  Keeping the runtime outside the application directory also
means a new small server build can reuse it while the pinned stack identifier
is unchanged.

Linux: CUDA and cuDNN come from the system (Arch's ``cuda`` and ``cudnn``
packages, which the server package depends on).  Arch's ONNX Runtime is built
without the TensorRT provider and the AUR's TensorRT is a major version ORT
1.31 cannot load, so only those two wheels are installed, without their
dependencies, by ``relay-server --install-nvidia-runtime``.

This module must remain importable without numpy, ONNX Runtime, or TensorRT:
the frozen entry points activate the external runtime before importing any of
those packages.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

RUNTIME_INSTALL_ARG = "--install-nvidia-runtime"
RUNTIME_INSTALL_CHECK_ARG = "--check-nvidia-runtime-installer"
RUNTIME_INSTALL_SMOKE_ARG = "--smoke-nvidia-runtime-installer"
RUNTIME_VALIDATE_ARG = "--validate-nvidia-runtime"
SOURCE_VALIDATE_ARG = "--validate-source-runtime"
_LINUX = sys.platform.startswith("linux")
RUNTIME_STACK_ID = (
    f"ort1.31.0-trt10.16.1-syscuda13-"
    f"py{sys.version_info.major}{sys.version_info.minor}-linux-v1"
    if _LINUX else
    f"ort1.31.0-trt10.16.1-cuda13.4-"
    f"py{sys.version_info.major}{sys.version_info.minor}-v1"
)

# All heavyweight packages are exact pins.  pip resolves their small Python
# dependencies at install time; these pins prevent CUDA/TensorRT components
# from silently drifting out of the validated combination.
#
# CUDA 13 dropped the "-cu12" suffix from the NVIDIA component wheels, so most
# names here changed rather than just their versions.  TensorRT stays on 10.x:
# ONNX Runtime 1.31.0's provider still imports nvinfer_10.dll /
# nvonnxparser_10.dll (checked in the wheel; upstream microsoft/onnxruntime
# #32278 tracks TensorRT 11), so the cu13 build of TensorRT 10, whose last
# release is 10.16.1, is the only one it can load.
NVIDIA_RUNTIME_PACKAGES = (
    "onnx==1.23.2",
    "onnxruntime-gpu==1.31.0",
    "tensorrt-cu13-libs==10.16.1.11",
    "cuda-toolkit==13.4.2",
    "nvidia-cublas==13.8.0.4",
    "nvidia-cuda-nvrtc==13.4.92",
    "nvidia-cuda-runtime==13.4.92",
    "nvidia-cudnn-cu13==9.27.0.42",
    "nvidia-cufft==12.4.0.43",
    "nvidia-nvjitlink==13.4.92",
)

# Linux takes CUDA and cuDNN from the system, so it installs only the two
# wheels no Arch package provides, with --no-deps: their Python dependencies
# (numpy, protobuf, flatbuffers, packaging) are the system's too, and a numpy
# in the runtime would shadow the one python-av was built against.
LINUX_RUNTIME_PACKAGES = (
    "onnxruntime-gpu==1.31.0",
    "tensorrt-cu13-libs==10.16.1.11",
)

# The system libraries the Linux ORT providers link against (readelf -d of
# libonnxruntime_providers_tensorrt.so and _cuda.so), and the Arch package
# that provides each.  The wheels were built against CUDA 13.0 / cuDNN 9; any
# later 13.x / 9.x keeps these sonames.
_SYSTEM_LIBRARIES = {
    "libcuda.so.1": "nvidia-utils",
    "libcudart.so.13": "cuda",
    "libcublas.so.13": "cuda",
    "libcublasLt.so.13": "cuda",
    "libcurand.so.10": "cuda",
    "libcudnn.so.9": "cudnn",
}

# TensorRT's soname major, which its Windows DLLs carry in their file names.
_TENSORRT_MAJOR = "10"
# Loaded in this order: the plugin and parser libraries need nvinfer.
_TENSORRT_LINUX_LIBRARIES = (
    f"libnvinfer.so.{_TENSORRT_MAJOR}",
    f"libnvinfer_plugin.so.{_TENSORRT_MAJOR}",
    f"libnvonnxparser.so.{_TENSORRT_MAJOR}",
)

_REQUIRED_PROVIDERS = {
    "TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider",
}
_dll_directory_handles: list[object] = []


def runtime_packages() -> tuple[str, ...]:
    return LINUX_RUNTIME_PACKAGES if _LINUX else NVIDIA_RUNTIME_PACKAGES


def runtime_base_dir() -> Path:
    """Return the user-writable root for managed inference runtimes."""
    override = os.environ.get("UPSCALE_RELAY_RUNTIME_DIR")
    if override:
        return Path(override)
    if _LINUX:
        # XDG base directories must be absolute; a relative value is ignored.
        root = Path(os.environ.get("XDG_DATA_HOME") or "")
        if not root.is_absolute():
            root = Path.home() / ".local" / "share"
        return root / "upscale-relay" / "runtimes"
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "upscale-relay" / "runtimes"
    return Path.home() / "AppData" / "Local" / "upscale-relay" / "runtimes"


def runtime_dir() -> Path:
    return runtime_base_dir() / RUNTIME_STACK_ID


def _marker(path: Path) -> Path:
    return path / ".runtime-ready.json"


def runtime_ready(path: Path | None = None) -> bool:
    target = runtime_dir() if path is None else path
    try:
        data = json.loads(_marker(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("stack_id") == RUNTIME_STACK_ID


def _native_dll_dirs(target: Path) -> list[Path]:
    # The NVIDIA wheels do not agree on a layout: cuDNN keeps its libraries
    # directly in nvidia/cudnn/bin, while the CUDA 13 components put theirs one
    # level deeper in nvidia/cu13/bin/x86_64.  Matching directories by name
    # would miss the latter, so select any directory that actually holds a DLL.
    dirs: list[Path] = []
    nvidia = target / "nvidia"
    if nvidia.is_dir():
        dirs.extend(sorted({path.parent for path in nvidia.rglob("*.dll")}))
    for relative in ("tensorrt_libs", "onnxruntime/capi"):
        path = target / relative
        if path.is_dir():
            dirs.append(path)
    return dirs


def activate_runtime(path: Path | None = None, *, require_ready: bool = True) -> bool:
    """Expose an installed runtime to this process before importing ORT.

    Windows: the directory is appended, rather than prepended, so lightweight
    packages already frozen into the executable (notably numpy) remain
    authoritative.  Windows DLL directory handles must be retained for the
    process lifetime.

    Linux: the directory goes first, so its onnxruntime wins over a system
    ``python-onnxruntime*`` (it holds nothing else that could shadow a system
    package).  TensorRT's directory goes on ``LD_LIBRARY_PATH`` for the
    inference worker and validation children: ORT's TensorRT provider has no
    RUNPATH, and the loader reads that variable only when a process starts.
    A process that runs TensorRT itself preloads the libraries instead
    (``upscale_cli.infer``).
    """
    target = runtime_dir() if path is None else path
    if require_ready and not runtime_ready(target):
        return False
    if not target.is_dir():
        return False

    target_text = str(target)
    if _LINUX:
        # First, even if a .pth file already listed it after a system build.
        if target_text in sys.path:
            sys.path.remove(target_text)
        sys.path.insert(0, target_text)
        libs = str(target / "tensorrt_libs")
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        if libs not in existing.split(os.pathsep):
            os.environ["LD_LIBRARY_PATH"] = libs + (os.pathsep + existing if existing else "")
        os.environ["UPSCALE_RELAY_ACTIVE_RUNTIME"] = target_text
        return True
    if target_text not in sys.path:
        sys.path.append(target_text)

    dll_dirs = _native_dll_dirs(target)
    if dll_dirs:
        existing_path = os.environ.get("PATH", "")
        prefix = os.pathsep.join(str(path) for path in dll_dirs)
        os.environ["PATH"] = prefix + (os.pathsep + existing_path if existing_path else "")
        if hasattr(os, "add_dll_directory"):
            for path in dll_dirs:
                _dll_directory_handles.append(os.add_dll_directory(str(path)))
    os.environ["UPSCALE_RELAY_ACTIVE_RUNTIME"] = target_text
    return True


def _prepare_pip_resources() -> None:
    """Teach distlib how to read resources collected by PyInstaller.

    pip uses distlib to generate scripts while installing wheels. PyInstaller's
    loader is not in distlib's built-in finder registry, even when every file
    is present, which otherwise fails with "Unable to locate finder".
    """
    distlib = importlib.import_module("pip._vendor.distlib")
    resources = importlib.import_module("pip._vendor.distlib.resources")
    resources.register_finder(distlib.__loader__, resources.ResourceFinder)


def _run_pip(
    target: Path,
    packages: tuple[str, ...] | None = None,
    *,
    ignore_installed: bool = False,
) -> int:
    # pip is deliberately collected into the frozen release.  Invoking its CLI
    # in this short-lived internal installer process avoids requiring Python on
    # the user's machine and keeps imports out of the long-running server.
    # On Linux it is the system's pip (python-pip); --target installs are
    # outside PEP 668's externally-managed check.
    from pip._internal.cli.main import main as pip_main

    if packages is None:
        packages = runtime_packages()
        no_deps = _LINUX
    else:
        no_deps = False
    _prepare_pip_resources()
    args = [
        "install",
        "--only-binary=:all:",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--extra-index-url", "https://pypi.nvidia.com",
        "--target", str(target),
    ]
    if ignore_installed:
        args.append("--ignore-installed")
    if no_deps:
        args.append("--no-deps")
    args.extend(packages)
    return int(pip_main(args))


@contextmanager
def _install_lock(base: Path):
    """Serialize first-run setup across concurrently launched server apps."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(
            None, False, f"Local\\UpscaleRelayRuntime-{RUNTIME_STACK_ID}",
        )
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        wait_result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        if wait_result not in (0x00000000, 0x00000080):
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
        try:
            yield
        finally:
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
    else:  # Allows unit tests and source use on non-Windows hosts.
        import fcntl

        lock_file = open(base / ".nvidia-runtime-install.lock", "a+b")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()


def _validate_runtime(target: Path) -> None:
    activate_runtime(target, require_ready=False)
    ort = importlib.import_module("onnxruntime")
    available = set(ort.get_available_providers())
    missing = _REQUIRED_PROVIDERS - available
    if missing:
        raise RuntimeError(
            f"NVIDIA runtime is missing providers {sorted(missing)}; "
            f"available providers: {sorted(available)}"
        )
    verify_native_runtime(target)
    if _LINUX:
        # The provider list is compiled in and says nothing about whether the
        # system CUDA libraries load: build real TensorRT and CUDA sessions
        # (--ep cuda uses the second without TensorRT).
        _validate_source_runtime("tensorrt")
        _validate_source_runtime("cuda")


def missing_system_libraries() -> list[str]:
    """Linux: the system CUDA libraries the runtime needs that do not load,
    each with the Arch package that provides it."""
    if not _LINUX:
        return []
    import ctypes

    missing = []
    for soname, package in _SYSTEM_LIBRARIES.items():
        try:
            ctypes.CDLL(soname)
        except OSError:
            missing.append(f"{soname} (package {package})")
    return missing


def _prune_runtime(target: Path) -> None:
    """Linux: drop TensorRT's builder resources for Windows-targeted engines.

    The Linux wheel carries them (1.8 GB of its 4.6) for cross-building
    engines that run on Windows, which this server never does.
    """
    if not _LINUX:
        return
    for path in (target / "tensorrt_libs").glob("libnvinfer_builder_resource_win_*"):
        path.unlink()


def _native_library_check(
    target: Path, *, linux: bool | None = None,
) -> tuple[list[Path], list[str]]:
    """Return the libraries to load and the names that are absent.

    Kept separate from loading so the required-file set stays testable on
    hosts that cannot load the platform's libraries.
    """
    linux = _LINUX if linux is None else linux
    tensorrt_libs = target / "tensorrt_libs"
    if linux:
        libraries = [tensorrt_libs / name for name in _TENSORRT_LINUX_LIBRARIES]
        libraries.append(
            target / "onnxruntime" / "capi" / "libonnxruntime_providers_tensorrt.so"
        )
        missing = [str(library) for library in libraries if not library.is_file()]
        # Linux names carry the full version: libnvinfer_builder_resource_sm120.so.10.16.1.
        pattern = f"libnvinfer_builder_resource*.so.{_TENSORRT_MAJOR}*"
        if not any(tensorrt_libs.glob(pattern)):
            missing.append(str(tensorrt_libs / pattern))
        return libraries, missing
    libraries = [
        tensorrt_libs / f"nvinfer_{_TENSORRT_MAJOR}.dll",
        tensorrt_libs / f"nvinfer_plugin_{_TENSORRT_MAJOR}.dll",
        tensorrt_libs / f"nvonnxparser_{_TENSORRT_MAJOR}.dll",
        target / "onnxruntime" / "capi" / "onnxruntime_providers_tensorrt.dll",
    ]
    missing = [str(library) for library in libraries if not library.is_file()]

    # Recent TensorRT splits the single nvinfer_builder_resource DLL into one
    # per GPU architecture (nvinfer_builder_resource_sm120_10.dll is the
    # Blackwell one).  Which of them ship depends on the build, so require the
    # set to be non-empty rather than naming any one.  These are resource DLLs
    # the builder loads on demand; loading them here would cost gigabytes.
    pattern = f"nvinfer_builder_resource*_{_TENSORRT_MAJOR}.dll"
    if not any(tensorrt_libs.glob(pattern)):
        missing.append(str(tensorrt_libs / pattern))
    return libraries, missing


def verify_native_runtime(path: Path | None = None) -> None:
    """Load the native TensorRT and ORT provider DLLs, not just registry names."""
    target = path
    if target is None:
        active = os.environ.get("UPSCALE_RELAY_ACTIVE_RUNTIME")
        if not active:
            return  # Source environments manage their own native library paths.
        target = Path(active)
    if os.name != "nt" and not _LINUX:
        return

    import ctypes

    libraries, missing = _native_library_check(target)
    if missing:
        raise RuntimeError(f"NVIDIA runtime is missing native libraries: {missing}")
    if _LINUX:
        absent = missing_system_libraries()
        if absent:
            raise RuntimeError(f"system CUDA libraries do not load: {absent}")
        for library in libraries:
            ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
        return
    for library in libraries:
        ctypes.WinDLL(str(library))


def _validation_command(target: Path) -> list[str]:
    args = [RUNTIME_VALIDATE_ARG, str(target)]
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "relay_server.runtime_bootstrap", *args]


def _run_validation_process(target: Path) -> int:
    # The validator exits before staging is renamed. Windows can otherwise
    # keep imported .pyd/DLL files mapped and block atomic publication.
    return subprocess.run(_validation_command(target), check=False).returncode


def _validate_source_runtime(ep: str, *, saved_choice: bool = False) -> None:
    """Exercise the source environment in a disposable process, including DLLs.

    ``saved_choice``: ``ep`` is the tray's saved preference, which the server
    itself replaces with auto when this build lacks it; probe it the same way
    rather than offering a multi-gigabyte download.
    """
    import numpy as np
    import onnx
    from upscale_cli.infer import ort, _EP_ORDER

    available = set(ort.get_available_providers())
    substituted = False
    if saved_choice and ep not in {alias for alias, name in _EP_ORDER if name in available}:
        ep, substituted = "auto", True
    providers = [name for alias, name in _EP_ORDER
                 if name in available and (ep == "auto" or alias == ep)]
    if not providers:
        raise RuntimeError(f"execution provider {ep!r} is unavailable")
    graph = onnx.helper.make_graph(
        [onnx.helper.make_node("Identity", ["input"], ["output"])],
        "source-runtime-check",
        [onnx.helper.make_tensor_value_info("input", onnx.TensorProto.FLOAT, [1, 3, 8, 8])],
        [onnx.helper.make_tensor_value_info("output", onnx.TensorProto.FLOAT, [1, 3, 8, 8])],
    )
    model = onnx.helper.make_model(graph, opset_imports=[onnx.helper.make_opsetid("", 13)], ir_version=10)
    session = ort.InferenceSession(model.SerializeToString(), providers=providers)
    if session.get_providers()[0] != providers[0]:
        raise RuntimeError(f"execution provider {providers[0]} failed to initialize")
    if _LINUX or substituted:
        # The pipeline refuses CPU inference unless --ep cpu asked for it, so
        # a CPU-only onnxruntime must not count as a usable runtime: the tray
        # would then skip offering the GPU setup (on Linux any CPU-only build;
        # elsewhere when a stale preference was replaced by auto).
        from upscale_cli.infer_worker import require_gpu_provider

        require_gpu_provider(session.get_providers()[0], ep)
    session.run(None, {"input": np.zeros((1, 3, 8, 8), dtype=np.float32)})


def source_runtime_ready(ep: str = "auto") -> bool:
    """Keep native provider validation out of the long-running tray process."""
    if getattr(sys, "frozen", False):
        return False
    try:
        result = subprocess.run(
            [sys.executable, "-m", "relay_server.runtime_bootstrap", SOURCE_VALIDATE_ARG, ep],
            capture_output=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def install_runtime() -> int:
    """Install and verify the pinned stack, publishing it atomically."""
    final = runtime_dir()
    if runtime_ready(final):
        print(f"NVIDIA runtime already installed: {final}", flush=True)
        return 0

    absent = missing_system_libraries()
    if absent:
        # Fail before a multi-gigabyte download that could not load anyway.
        print(
            "The NVIDIA runtime needs these system libraries, which do not load: "
            + ", ".join(absent),
            file=sys.stderr, flush=True,
        )
        return 1

    base = runtime_base_dir()
    base.mkdir(parents=True, exist_ok=True)
    with _install_lock(base):
        # Another process may have completed setup while this one waited.
        if runtime_ready(final):
            print(f"NVIDIA runtime already installed: {final}", flush=True)
            return 0

        for partial in base.glob(f".{RUNTIME_STACK_ID}.installing-*"):
            shutil.rmtree(partial, ignore_errors=True)
        staging = base / f".{RUNTIME_STACK_ID}.installing-{os.getpid()}"
        staging.mkdir()

        if _LINUX:
            print("Installing the pinned ONNX Runtime and TensorRT (about 4 GB to download)...",
                  flush=True)
        else:
            print("Installing the pinned TensorRT/CUDA runtime (several GB)...", flush=True)
        print(f"Destination: {final}", flush=True)
        try:
            result = _run_pip(staging)
            if result:
                raise RuntimeError(f"pip exited with status {result}")
            _prune_runtime(staging)
            print("Verifying TensorRT, CUDA, and CPU providers...", flush=True)
            result = _run_validation_process(staging)
            if result:
                raise RuntimeError(f"runtime validation exited with status {result}")
            _marker(staging).write_text(json.dumps({
                "stack_id": RUNTIME_STACK_ID,
                "packages": runtime_packages(),
            }, indent=2), encoding="utf-8")
            if final.exists():
                shutil.rmtree(final)
            staging.replace(final)
        except Exception as err:
            print(f"NVIDIA runtime setup failed: {err}", file=sys.stderr, flush=True)
            shutil.rmtree(staging, ignore_errors=True)
            return 1

    print(f"NVIDIA runtime is ready: {final}", flush=True)
    return 0


def maybe_run_runtime_installer(argv: list[str] | None = None) -> int | None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == SOURCE_VALIDATE_ARG:
        try:
            _validate_source_runtime(args[1], saved_choice=True)
        except Exception as err:
            print(f"Source runtime validation failed: {err}", file=sys.stderr, flush=True)
            return 1
        return 0
    if args == [RUNTIME_INSTALL_CHECK_ARG]:
        # Frozen-build smoke test: prove pip's internal CLI was collected
        # without starting the multi-gigabyte network installation in CI.
        importlib.import_module("pip._internal.cli.main")
        _prepare_pip_resources()
        return 0
    if len(args) == 2 and args[0] == RUNTIME_INSTALL_SMOKE_ARG:
        target = Path(args[1])
        target.mkdir(parents=True, exist_ok=True)
        return _run_pip(
            target, ("humanfriendly==10.0",), ignore_installed=True,
        )
    if len(args) == 2 and args[0] == RUNTIME_VALIDATE_ARG:
        try:
            _validate_runtime(Path(args[1]))
        except Exception as err:
            print(f"NVIDIA runtime validation failed: {err}", file=sys.stderr, flush=True)
            return 1
        return 0
    if args != [RUNTIME_INSTALL_ARG]:
        return None
    return install_runtime()


def installer_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, RUNTIME_INSTALL_ARG]
    return [sys.executable, "-m", "relay_server.runtime_bootstrap", RUNTIME_INSTALL_ARG]


def run_installer_process(
    on_line: Callable[[str], None] | None = None,
    on_process: Callable[[subprocess.Popen], None] | None = None,
) -> int:
    """Run first-time setup in an isolated child and stream its output."""
    proc = subprocess.Popen(
        installer_command(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        # Its own group on POSIX, so terminate_process_tree can signal it whole.
        start_new_session=os.name != "nt",
    )
    if on_process is not None:
        on_process(proc)
    assert proc.stdout is not None
    for line in proc.stdout:
        if on_line is not None:
            on_line(line.rstrip())
    return proc.wait()


def terminate_process_tree(proc) -> None:
    """Stop the installer and everything it started.

    The installer runs its own children (the validation probe), and they
    inherit its stdout pipe. Terminating only the installer leaves them
    running and the pipe open, so ``run_installer_process`` never sees EOF and
    a cancelled setup never finishes cancelling.
    """
    if proc.poll() is not None:
        return
    pid = getattr(proc, "pid", None)
    try:
        if pid is not None and os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        elif pid is not None:
            import signal

            os.killpg(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


def ensure_runtime_console() -> bool:
    if activate_runtime():
        return True
    print("The NVIDIA inference runtime is not installed yet.")
    result = run_installer_process(lambda line: print(line, flush=True))
    return result == 0 and activate_runtime()


if __name__ == "__main__":
    result = maybe_run_runtime_installer()
    raise SystemExit(2 if result is None else result)
