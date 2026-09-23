"""Verified content-addressed cache for negotiated subtitle attachments."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading

import aiohttp


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024 * 1024
_VIEW_LEASES: dict[Path, object] = {}
_CACHE_MUTEX = threading.RLock()


def _lock_file(handle, *, blocking: bool) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lease_file(path: Path):
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    return handle


@contextmanager
def _cache_guard(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with _CACHE_MUTEX, _lease_file(root / ".views.lock") as handle:
        _lock_file(handle, blocking=True)
        try:
            yield
        finally:
            _unlock_file(handle)


def _prune_abandoned_views(root: Path) -> None:
    sessions = root / "sessions"
    sessions.mkdir(exist_ok=True)
    for view in sessions.iterdir():
        if not view.is_dir() or view in _VIEW_LEASES:
            continue
        # New clients hold the lease throughout playback. OS locks disappear
        # on crash, unlike timestamps or PID files; another live player keeps
        # its font view even if this process evicts the original object name.
        with _lease_file(view / ".lease") as handle:
            try:
                _lock_file(handle, blocking=False)
            except OSError:
                continue
            _unlock_file(handle)
        shutil.rmtree(view)


def _safe_name(value: object, digest: str) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f")
    name = _SAFE_NAME_RE.sub("_", name).strip(" ._")[:128]
    return name or f"font-{digest[:12]}"


def validate_manifest(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("attachment manifest must be a list")
    out = []
    total = 0
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid attachment manifest entry")
        digest = str(item.get("sha256", "")).lower()
        size = item.get("size")
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError("invalid attachment hash")
        if not isinstance(size, int) or size < 0 or size > MAX_ATTACHMENT_BYTES:
            raise ValueError("invalid attachment size")
        if digest in seen:
            continue
        seen.add(digest)
        total += size
        if total > MAX_MANIFEST_BYTES:
            raise ValueError("attachment manifest exceeds session size limit")
        out.append({
            "sha256": digest,
            "size": size,
            "name": _safe_name(item.get("name"), digest),
            "mimetype": str(item.get("mimetype") or "application/octet-stream"),
        })
    return out


def _verified(path: Path, size: int, digest: str) -> bool:
    try:
        if path.stat().st_size != size:
            return False
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            return False
        os.utime(path, None)
        return True
    except OSError:
        return False


def _publish_object(path: Path, data: bytes, digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{digest}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _materialize_view(root: Path, session_id: str, entries: list[dict]) -> Path:
    with _cache_guard(root):
        _prune_abandoned_views(root)
        return _create_view(root, session_id, entries)


def _create_view(root: Path, session_id: str, entries: list[dict]) -> Path:
    safe_session = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:64] or "session"
    view = Path(tempfile.mkdtemp(prefix=f"{safe_session}-", dir=root / "sessions"))
    lease = _lease_file(view / ".lease")
    try:
        _lock_file(lease, blocking=False)
        _VIEW_LEASES[view] = lease
        _populate_view(root, view, entries)
    except BaseException:
        _VIEW_LEASES.pop(view, None)
        lease.close()
        shutil.rmtree(view, ignore_errors=True)
        raise
    return view


def _populate_view(root: Path, view: Path, entries: list[dict]) -> None:
    used: set[str] = set()
    for entry in entries:
        name = entry["name"]
        if name.casefold() in used:
            stem, suffix = os.path.splitext(name)
            index = 1
            while name.casefold() in used:
                name = f"{stem}-{entry['sha256'][:8]}-{index}{suffix}"
                index += 1
        used.add(name.casefold())
        source = root / "objects" / entry["sha256"]
        target = view / name
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)


def _evict(root: Path, protected: set[str]) -> None:
    objects = root / "objects"
    try:
        files = [path for path in objects.iterdir() if path.is_file()]
    except OSError:
        return
    sized = []
    total = 0
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        total += stat.st_size
        sized.append((stat.st_mtime_ns, stat.st_size, path))
    for _mtime, size, path in sorted(sized):
        if total <= MAX_CACHE_BYTES:
            break
        if path.name in protected:
            continue
        try:
            path.unlink()
            total -= size
        except OSError:
            pass


async def materialize_attachment_cache(
    http: aiohttp.ClientSession,
    base_url: str,
    session_id: str,
    manifest: object,
    token: str,
    cache_root: Path,
) -> Path:
    """Fetch cache misses, verify hashes, and return this session's font dir."""
    entries = validate_manifest(manifest)
    objects = cache_root / "objects"
    await asyncio.to_thread(objects.mkdir, parents=True, exist_ok=True)
    for entry in entries:
        digest = entry["sha256"]
        target = objects / digest
        if await asyncio.to_thread(_verified, target, entry["size"], digest):
            continue
        url = f"{base_url.rstrip('/')}/attachments/{digest}"
        async with http.get(
            url, headers={"Authorization": f"Bearer {token}"},
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.content.iter_chunked(1024 * 1024):
                received += len(chunk)
                if received > entry["size"] or received > MAX_ATTACHMENT_BYTES:
                    raise ValueError("attachment body exceeds declared size")
                chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("attachment size/hash mismatch")
        await asyncio.to_thread(_publish_object, target, data, digest)
    publishing = asyncio.create_task(asyncio.to_thread(
        _materialize_view, cache_root, session_id, entries))
    try:
        view = await asyncio.shield(publishing)
    except asyncio.CancelledError:
        try:
            view = await publishing
        except Exception:
            pass
        else:
            await remove_attachment_view(view)
        raise
    try:
        await asyncio.to_thread(_evict, cache_root, {entry["sha256"] for entry in entries})
    except BaseException:
        await remove_attachment_view(view)
        raise
    return view


async def remove_attachment_view(path: Path | None) -> None:
    if path is not None:
        await asyncio.to_thread(_remove_view, path)


def _remove_view(path: Path) -> None:
    with _cache_guard(path.parent.parent):
        lease = _VIEW_LEASES.pop(path, None)
        if lease is not None:
            lease.close()
        shutil.rmtree(path, ignore_errors=True)
