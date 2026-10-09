"""Versioned playback history stored in the application's isolated QSettings."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import ipaddress
import json
import math
import os
import posixpath
import re
import time


def endpoint_key(host: str, port: int) -> str:
    host = host.strip().strip("[]").rstrip(".").casefold()
    try:
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    return f"[{host}]:{int(port)}"


# docs/PROTOCOL.md: capabilities.server_id is 1-64 of [A-Za-z0-9_-].
_SERVER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def server_identity(host: str, port: int, server_id=None) -> str:
    """The server a library path belongs to: the id it announces in its
    capabilities, which follows it across address changes, else its address."""
    if isinstance(server_id, str) and _SERVER_ID.fullmatch(server_id):
        return "id:" + server_id
    return endpoint_key(host, port)


def source_key(source: str, path: str, host: str = "", port: int = 8590, server_id=None) -> str:
    if source in ("local", "uplink"):
        return "local:" + os.path.normcase(os.path.abspath(os.path.expanduser(path)))
    return ("server:" + server_identity(host, port, server_id) + "/"
            + posixpath.normpath(path.replace("\\", "/")).lstrip("/"))


@dataclass
class HistoryEntry:
    key: str
    position: float = 0.0
    duration: float | None = None
    watched: bool = False
    last_played: float = 0.0

    @property
    def resume(self) -> float:
        if self.watched or self.position < 10:
            return 0.0
        if self.duration is not None and self.position >= self.duration - 90:
            return 0.0
        return self.position

    @property
    def progress(self) -> str:
        """Short form for the file browser: a check once watched, else a percentage."""
        if self.watched:
            return "\u2713"
        return f"{min(99, int(self.position / self.duration * 100))}%" if self.duration else ""

    @property
    def description(self) -> str:
        progress = "Watched" if self.watched else (
            f"{min(100, int(self.position / self.duration * 100))}%" if self.duration else "In progress"
        )
        return f"{progress} · {datetime.fromtimestamp(self.last_played):%Y-%m-%d %H:%M}"


class HistoryStore:
    def __init__(self, settings, limit: int = 50):
        self.settings = settings
        self.limit = max(1, min(1000, limit))
        self.entries: dict[str, HistoryEntry] = {}
        try:
            data = json.loads(settings.value("history/v2", "") or "null")
            if data is None:
                # Version 1 keyed server files by address alone. Its server
                # entries are dropped rather than guessed onto a server id;
                # local files carry over.
                data = json.loads(settings.value("history/v1", "{}"))
                records = [r for r in data.get("entries", []) if isinstance(r, dict)
                           and str(r.get("key", "")).startswith("local:")] if data.get("version") == 1 else []
            else:
                records = data.get("entries", []) if data.get("version") == 2 else []
        except (ValueError, TypeError, AttributeError):
            records = []
        for record in records if isinstance(records, list) else []:
            try:
                entry = HistoryEntry(**record)
                if not isinstance(entry.key, str) or not entry.key.startswith(("local:", "server:")):
                    continue
                if type(entry.watched) is not bool:
                    continue
                if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
                       for v in (entry.position, entry.last_played)):
                    continue
                if entry.duration is not None and (type(entry.duration) not in (int, float)
                        or not math.isfinite(entry.duration) or entry.duration <= 0):
                    continue
                # A finite number can still lie outside the platform's
                # timestamp range; reject it before tree labels format dates.
                datetime.fromtimestamp(entry.last_played)
                self.entries[entry.key] = entry
            except (TypeError, ValueError, OverflowError, OSError):
                continue
        self._trim()

    def _trim(self):
        self.entries = dict(sorted(self.entries.items(), key=lambda p: p[1].last_played, reverse=True)[:self.limit])

    def flush(self):
        self._trim()
        self.settings.setValue("history/v2", json.dumps({
            "version": 2, "entries": [asdict(e) for e in self.entries.values()],
        }))
        self.settings.sync()

    def save(self, key: str, position: float, duration: float | None, *, completed=False):
        if not math.isfinite(position) or position < 0:
            return
        duration = duration if duration and math.isfinite(duration) and duration > 0 else None
        previous = self.entries.get(key)
        watched = completed or (duration is not None and position >= max(0, duration - 90))
        if duration is None and previous is not None:
            watched = watched or previous.watched
        self.entries[key] = HistoryEntry(key, position, duration, watched, time.time())
        self.flush()

    def mark(self, key: str, watched: bool):
        entry = self.entries.setdefault(key, HistoryEntry(key))
        entry.watched = watched
        entry.position = entry.position if watched else 0.0
        entry.last_played = time.time()
        self.flush()
