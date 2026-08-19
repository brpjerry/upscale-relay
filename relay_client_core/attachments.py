"""Verified content-addressed cache for negotiated subtitle attachments."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

import aiohttp


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024 * 1024


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
    safe_session = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:64] or "session"
    view = root / "sessions" / safe_session
    if view.exists():
        shutil.rmtree(view)
    view.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    for entry in entries:
        name = entry["name"]
        if name in used:
            stem, suffix = os.path.splitext(name)
            name = f"{stem}-{entry['sha256'][:8]}{suffix}"
        used.add(name)
        source = root / "objects" / entry["sha256"]
        target = view / name
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
    return view


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
    view = await asyncio.to_thread(_materialize_view, cache_root, session_id, entries)
    await asyncio.to_thread(_evict, cache_root, {entry["sha256"] for entry in entries})
    return view


async def remove_attachment_view(path: Path | None) -> None:
    if path is not None:
        await asyncio.to_thread(shutil.rmtree, path, True)
