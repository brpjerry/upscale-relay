"""Single-instance guard shared by ``relay-server`` and ``relay-server-gui``.

Two servers on one box fight over the GPU (and usually the ports), so a second
launch must refuse to start. The guard is an OS-level exclusive lock on a file
in the per-user app-data dir: the kernel drops it when the holding process
exits, crashes included, so there is never a stale lock to clean up. The file
itself is left behind on purpose; only the lock on it means anything.

The holder's PID is written at the start of the file for the error message.
The locked byte sits past that text because a Windows byte-range lock is
mandatory: a reader touching the locked range would fail.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_LOCK_NAME = "relay-server.lock"
_LOCK_OFFSET = 64  # past the PID text, see module docstring

# The lock this process holds, so a second acquire (the frozen CLI entry takes
# it before the runtime install, then ``main`` asks again) is a no-op rather
# than a self-conflict: a POSIX flock on a second descriptor would fail.
_held: InstanceLock | None = None


class AlreadyRunningError(RuntimeError):
    """Another relay server process holds the instance lock."""

    def __init__(self, path: Path, pid: int | None):
        self.path = path
        self.pid = pid
        who = f" (PID {pid})" if pid is not None else ""
        super().__init__(
            f"Another Upscale Relay server is already running{who}. "
            f"Stop it before starting a new one. (lock: {path})"
        )


def lock_dir() -> Path:
    """Per-user directory holding the lock file.

    ``UPSCALE_RELAY_LOCK_DIR`` overrides it (tests, or isolating two
    deliberately separate servers).
    """
    override = os.environ.get("UPSCALE_RELAY_LOCK_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "upscale-relay"
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "upscale-relay"


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _read_pid(fd: int) -> int | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return int(os.read(fd, _LOCK_OFFSET).split(b"\0", 1)[0].strip())
    except (OSError, ValueError):
        return None


class InstanceLock:
    """Holds the server instance lock until :meth:`release` or process exit."""

    def __init__(self, path: Path, fd: int):
        self.path = path
        self._fd: int | None = fd

    def release(self) -> None:
        global _held
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        if _held is self:
            _held = None
        if sys.platform == "win32":
            import msvcrt

            try:
                os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)  # closing also drops a POSIX flock

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire_instance_lock(directory: Path | None = None) -> InstanceLock:
    """Take the instance lock or raise :class:`AlreadyRunningError`."""
    global _held
    directory = lock_dir() if directory is None else directory
    path = directory / _LOCK_NAME
    if _held is not None and _held.path == path:
        return _held
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if not _try_lock(fd):
            raise AlreadyRunningError(path, _read_pid(fd))
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode().ljust(_LOCK_OFFSET, b"\0")[:_LOCK_OFFSET])
    except BaseException:
        os.close(fd)
        raise
    _held = InstanceLock(path, fd)
    return _held
