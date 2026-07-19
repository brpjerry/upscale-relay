"""First-run installer for the frozen Windows NVIDIA inference runtime.

The release executable deliberately does not contain the multi-gigabyte CUDA,
cuDNN, TensorRT, and ONNX Runtime wheels.  It carries pip's installer code and
uses it to build a versioned, per-user runtime on first launch.  Keeping the
runtime outside the application directory also means a new small server build
can reuse it while the pinned stack identifier is unchanged.

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
RUNTIME_STACK_ID = (
    f"ort1.23.2-trt10.13.3-cuda12.9-"
    f"py{sys.version_info.major}{sys.version_info.minor}-v1"
)

# All heavyweight packages are exact pins.  pip resolves their small Python
# dependencies at install time; these pins prevent CUDA/TensorRT components
# from silently drifting out of the validated combination.
NVIDIA_RUNTIME_PACKAGES = (
    "onnx==1.22.0",
    "onnxruntime-gpu==1.23.2",
    "tensorrt-cu12-libs==10.13.3.9.post1",
    "cuda-toolkit==12.9.2.0",
    "nvidia-cublas-cu12==12.9.2.10",
    "nvidia-cuda-nvrtc-cu12==12.9.86",
    "nvidia-cuda-runtime-cu12==12.9.79",
    "nvidia-cudnn-cu12==9.24.0.43",
    "nvidia-cufft-cu12==11.4.1.4",
    "nvidia-nvjitlink-cu12==12.9.86",
)

_REQUIRED_PROVIDERS = {
    "TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider",
}
_dll_directory_handles: list[object] = []


def runtime_base_dir() -> Path:
    """Return the user-writable root for managed inference runtimes."""
    override = os.environ.get("UPSCALE_RELAY_RUNTIME_DIR")
    if override:
        return Path(override)
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
    return data.get("stack_id") == RUNTIME_STACK_ID


def _native_dll_dirs(target: Path) -> list[Path]:
    dirs: list[Path] = []
    nvidia = target / "nvidia"
    if nvidia.is_dir():
        dirs.extend(path for path in nvidia.rglob("bin") if path.is_dir())
    for relative in ("tensorrt_libs", "onnxruntime/capi"):
        path = target / relative
        if path.is_dir():
            dirs.append(path)
    return dirs


def activate_runtime(path: Path | None = None, *, require_ready: bool = True) -> bool:
    """Expose an installed runtime to this process before importing ORT.

    The directory is appended, rather than prepended, so lightweight packages
    already frozen into the executable (notably numpy) remain authoritative.
    Windows DLL directory handles must be retained for the process lifetime.
    """
    target = runtime_dir() if path is None else path
    if require_ready and not runtime_ready(target):
        return False
    if not target.is_dir():
        return False

    target_text = str(target)
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
    packages: tuple[str, ...] = NVIDIA_RUNTIME_PACKAGES,
    *,
    ignore_installed: bool = False,
) -> int:
    # pip is deliberately collected into the frozen release.  Invoking its CLI
    # in this short-lived internal installer process avoids requiring Python on
    # the user's machine and keeps imports out of the long-running server.
    from pip._internal.cli.main import main as pip_main

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


def verify_native_runtime(path: Path | None = None) -> None:
    """Load the native TensorRT and ORT provider DLLs, not just registry names."""
    target = path
    if target is None:
        active = os.environ.get("UPSCALE_RELAY_ACTIVE_RUNTIME")
        if not active:
            return  # Source environments manage their own native library paths.
        target = Path(active)
    if os.name != "nt":
        return

    import ctypes

    libraries = (
        target / "tensorrt_libs" / "nvinfer_10.dll",
        target / "tensorrt_libs" / "nvinfer_builder_resource_10.dll",
        target / "tensorrt_libs" / "nvinfer_plugin_10.dll",
        target / "tensorrt_libs" / "nvonnxparser_10.dll",
        target / "onnxruntime" / "capi" / "onnxruntime_providers_tensorrt.dll",
    )
    missing = [str(library) for library in libraries if not library.is_file()]
    if missing:
        raise RuntimeError(f"NVIDIA runtime is missing native libraries: {missing}")
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


def install_runtime() -> int:
    """Install and verify the pinned stack, publishing it atomically."""
    final = runtime_dir()
    if runtime_ready(final):
        print(f"NVIDIA runtime already installed: {final}", flush=True)
        return 0

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

        print("Installing the pinned TensorRT/CUDA runtime (several GB)...", flush=True)
        print(f"Destination: {final}", flush=True)
        try:
            result = _run_pip(staging)
            if result:
                raise RuntimeError(f"pip exited with status {result}")
            print("Verifying TensorRT, CUDA, and CPU providers...", flush=True)
            result = _run_validation_process(staging)
            if result:
                raise RuntimeError(f"runtime validation exited with status {result}")
            _marker(staging).write_text(json.dumps({
                "stack_id": RUNTIME_STACK_ID,
                "packages": NVIDIA_RUNTIME_PACKAGES,
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
    )
    if on_process is not None:
        on_process(proc)
    assert proc.stdout is not None
    for line in proc.stdout:
        if on_line is not None:
            on_line(line.rstrip())
    return proc.wait()


def ensure_runtime_console() -> bool:
    if activate_runtime():
        return True
    print("The NVIDIA inference runtime is not installed yet.")
    result = run_installer_process(lambda line: print(line, flush=True))
    return result == 0 and activate_runtime()


if __name__ == "__main__":
    result = maybe_run_runtime_installer()
    raise SystemExit(2 if result is None else result)
