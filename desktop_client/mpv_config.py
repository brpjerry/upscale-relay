"""Conservative, lossless edits to exposed global mpv defaults.

Includes are read in order; named profile bodies are never rewritten. A GUI
edit appends an explicit global assignment, leaving original spelling/comments
intact. External changes are reread before each atomic, symlink-aware commit.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import tempfile

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

DEFAULTS = {"sid": "auto", "slang": "", "video-sync": "audio",
            "interpolation": "no", "tscale": "oversample"}
ALIASES = {"sub": "sid"}
MAX_BYTES = 2 * 1024 * 1024


def assignment(line: str) -> tuple[str, str] | None:
    text = line.strip()
    if not text or text.startswith(("#", "[")):
        return None
    name, sep, value = text.partition("=")
    name = name.strip().removeprefix("--")
    value = value.strip()
    if not sep:
        value = "yes"
    elif value.startswith(("\"", "'")):
        quote = value[0]
        end = value.find(quote, 1)
        suffix = value[end + 1:].strip()
        if end < 0 or (suffix and not suffix.startswith("#")):
            raise ValueError("Invalid quoted mpv configuration value")
        value = value[1:end]
    elif value.startswith("%"):
        match = re.match(r"%(\d+)%", value)
        if not match:
            raise ValueError("Invalid length-quoted mpv value")
        raw = value[match.end():].encode("utf-8")
        size = int(match[1])
        if len(raw) < size or (raw[size:].strip() and not raw[size:].strip().startswith(b"#")):
            raise ValueError("Invalid length-quoted mpv value")
        value = raw[:size].decode("utf-8")
    else:
        value = value.split("#", 1)[0].strip()
    if name.startswith("no-") and not sep:
        name, value = name[3:], "no"
    return ALIASES.get(name, name), value



class MpvConfig:
    def __init__(self, path: Path, expand=None):
        self.path = Path(path)
        self.expand = expand or (lambda p: str(Path(p).expanduser()))
        self.dependencies: set[Path] = set()
        self.profile_options: set[str] = set()
        self.values = dict(DEFAULTS)
        self.effective_values = dict(DEFAULTS)

    def read(self) -> dict[str, str]:
        values = dict(DEFAULTS)
        effective = dict(DEFAULTS)
        dependencies: set[Path] = set()
        profile_options = set()
        profiles = {}
        total = 0

        def apply(target, name, value):
            if name in target:
                target[name] = value
            elif name.startswith("slang-"):
                action = name[6:]
                old = list(filter(None, target["slang"].split(",")))
                items = list(filter(None, value.split(",")))
                if action == "set":
                    old = items
                elif action in ("append", "add"):
                    old += items
                elif action == "pre":
                    old = items + old
                elif action == "clr":
                    old = []
                elif action in ("remove", "del"):
                    old = [v for v in old if v not in items]
                elif action == "toggle":
                    old = [v for v in old if v != value] if value in old else old + [value]
                target["slang"] = ",".join(old)

        def execute(options, stack, active=(), globals=True):
            for name, value in options:
                if name == "include":
                    visit(Path(self.expand(value)), stack, globals=globals)
                elif name == "profile":
                    for profile in value.split(","):
                        profile_options.add(profile)
                        if profile in active or len(active) >= 16:
                            raise ValueError("Recursive mpv profile")
                        if profile in profiles:
                            execute(profiles[profile], stack, (*active, profile), globals=False)
                else:
                    if globals:
                        apply(values, name, value)
                    apply(effective, name, value)

        def visit(path, stack, globals=True):
            nonlocal total
            path = path.resolve()
            if path in stack or len(stack) >= 16:
                raise ValueError("Recursive mpv.conf include")
            dependencies.add(path)
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                if not stack:
                    return
                raise
            total += len(raw)
            if total > MAX_BYTES:
                raise ValueError("mpv configuration exceeds the safe editor size limit")
            profile = "default"
            defaults = []
            for line in raw.decode("utf-8-sig").splitlines():
                stripped = line.strip()
                if stripped.startswith("["):
                    match = re.fullmatch(r"\[([^]]+)\]\s*(?:#.*)?", stripped)
                    if not match:
                        raise ValueError("Unrecognized mpv profile header")
                    profile = match[1]
                    continue
                option = assignment(line)
                if option is None:
                    continue
                name, value = option
                if profile == "default":
                    defaults.append(option)
                else:
                    profiles.setdefault(profile, []).append(option)
                    if name in values or name == "profile-cond":
                        profile_options.add(profile)
            execute(defaults, (*stack, path), globals=globals)
        # Retain dependencies even after an error, allowing a watcher to notice
        # when a missing include is created or repaired.
        try:
            visit(self.path, ())
        finally:
            self.dependencies = dependencies | {self.path.absolute()}
        self.profile_options = profile_options
        self.values = values
        self.effective_values = effective
        return dict(values)

    def write(self, name: str, value: str) -> dict[str, str]:
        if name not in DEFAULTS or any(c in value for c in '\r\n\x00"'):
            raise ValueError("Invalid mpv default")
        self.read()  # Fail closed on unreadable includes or unknown syntax.
        target = self.path.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        before = target.read_bytes() if target.exists() else b""
        metadata = target.stat() if target.exists() else None
        if metadata and not metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError(f"Configuration is read-only: {self.path}")
        newline = "\r\n" if b"\r\n" in before else "\n"
        text = before.decode("utf-8-sig")
        profile = "default"
        for line in text.splitlines():
            m = re.match(r"\s*\[([^]]+)\]", line)
            if m:
                profile = m[1]
        addition = (newline if before and not before.endswith(b"\n") else "")
        if profile != "default":
            addition += "[default]" + newline
        addition += f'{name}="{value}"' + newline
        after = before + addition.encode("utf-8")
        fd, temp = tempfile.mkstemp(prefix=".mpv-conf-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(after)
                stream.flush()
                os.fsync(stream.fileno())
            if metadata:
                os.chmod(temp, stat.S_IMODE(metadata.st_mode))
            current = target.read_bytes() if target.exists() else b""
            current_stat = target.stat() if target.exists() else None
            identity = lambda st: (st.st_ino, st.st_mtime_ns, st.st_ctime_ns) if st else None
            if current != before or self.path.resolve() != target or identity(current_stat) != identity(metadata):
                raise RuntimeError("mpv.conf changed during the edit; please try again")
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        self.values[name] = value
        self.effective_values[name] = value
        return dict(self.values)


class ConfigWatcher(QObject):
    changed = Signal(dict)
    failed = Signal(str)

    def __init__(self, config: MpvConfig, parent=None):
        super().__init__(parent)
        self.config = config
        self.watcher = QFileSystemWatcher(self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        self.watcher.fileChanged.connect(lambda _: self.timer.start())
        self.watcher.directoryChanged.connect(lambda _: self.timer.start())
        self._last = None
        self.refresh()

    def refresh(self):
        try:
            values = self.config.read()
            fingerprint = (values, sorted(self.config.profile_options))
            if fingerprint != self._last:
                self._last = fingerprint
                self.changed.emit(values)
        except (OSError, ValueError) as err:
            self.failed.emit(str(err))
        paths = self.config.dependencies | {self.config.path.absolute(), self.config.path.resolve()}
        for path in tuple(paths):
            parent = path.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            paths.add(parent)
        old = self.watcher.files() + self.watcher.directories()
        if old:
            self.watcher.removePaths(old)
        existing = [str(p) for p in paths if p.exists()]
        if existing:
            self.watcher.addPaths(existing)

    def close(self):
        self.timer.stop()
        paths = self.watcher.files() + self.watcher.directories()
        if paths:
            self.watcher.removePaths(paths)
