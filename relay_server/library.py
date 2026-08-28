"""Sandboxed server-side media library discovery and path resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import Iterable


PLAYABLE_SUFFIXES = frozenset({".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm"})

SORT_KEYS = ("name", "mtime")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class LibraryPathError(ValueError):
    pass


@dataclass(frozen=True)
class _Root:
    name: str
    path: Path


def _root_name(root: PurePath) -> str:
    """Return a client-safe label for a local directory or filesystem root."""
    if root.name:
        return root.name
    # A resolved UNC share root is all anchor: ``name`` is empty and ``drive``
    # is ``\\server\share``.  Exposing that drive verbatim puts backslashes in
    # an API path that deliberately accepts POSIX separators only, so use just
    # the final share component.  Drive roots similarly become ``C``.
    drive = root.drive.rstrip(":\\/").replace("\\", "/")
    return drive.rsplit("/", 1)[-1] if drive else "Library"


class MediaLibrary:
    """One or more sandboxed media roots behind the existing library API.

    A single root retains the original flat namespace.  With multiple roots,
    the API exposes a virtual directory for each root, named after its folder.
    This keeps paths unambiguous without changing anything in clients.
    """

    def __init__(self, roots: str | Path | Iterable[str | Path]):
        values = [roots] if isinstance(roots, (str, Path)) else list(roots)
        if not values:
            raise ValueError("at least one library root is required")

        resolved: list[Path] = []
        seen: set[str] = set()
        for value in values:
            root = Path(value).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"library root is not a directory: {root}")
            identity = os.path.normcase(str(root))
            if identity not in seen:
                resolved.append(root)
                seen.add(identity)

        self.root = resolved[0]  # Backward compatibility for single-root callers.
        self.roots = tuple(resolved)
        self._virtual = len(resolved) > 1
        self._root_entries = self._name_roots(resolved)
        self._roots_by_name = {entry.name: entry for entry in self._root_entries}

    @staticmethod
    def _name_roots(roots: list[Path]) -> tuple[_Root, ...]:
        """Assign stable, unique virtual names in configuration order."""
        entries: list[_Root] = []
        used: set[str] = set()
        for root in roots:
            base = _root_name(root)
            name = base
            suffix = 2
            while name.casefold() in used:
                name = f"{base} ({suffix})"
                suffix += 1
            used.add(name.casefold())
            entries.append(_Root(name=name, path=root))
        return tuple(entries)

    @staticmethod
    def _validated_parts(relative: str, *, allow_empty: bool = False) -> tuple[str, ...]:
        if "\\" in relative:
            raise LibraryPathError("invalid library path")
        if not relative:
            if allow_empty:
                return ()
            raise LibraryPathError("invalid library path")
        rel = PurePosixPath(relative)
        if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
            raise LibraryPathError("invalid library path")
        return rel.parts

    def _physical_parts(
        self, relative: str, *, allow_root: bool = False,
    ) -> tuple[_Root, tuple[str, ...]]:
        parts = self._validated_parts(relative, allow_empty=allow_root)
        if not self._virtual:
            return self._root_entries[0], parts
        if not parts:
            raise LibraryPathError("virtual library root has no filesystem path")
        entry = self._roots_by_name.get(parts[0])
        if entry is None:
            raise LibraryPathError("library path not found")
        return entry, parts[1:]

    @staticmethod
    def _contained(root: Path, parts: tuple[str, ...]) -> Path:
        candidate = root.joinpath(*parts).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as err:
            raise LibraryPathError("library path escapes root") from err
        return candidate

    def resolve_file(self, relative: str) -> Path:
        """Resolve an API-relative POSIX path without escaping its root."""
        entry, parts = self._physical_parts(relative)
        if not parts:
            raise LibraryPathError("invalid library path")
        candidate = self._contained(entry.path, parts)
        if not candidate.is_file():
            raise LibraryPathError("library file not found")
        if candidate.suffix.casefold() not in PLAYABLE_SUFFIXES:
            raise LibraryPathError("not a playable library file")
        return candidate

    def resolve_directory(self, relative: str) -> Path:
        """Resolve an API-relative directory; ``""`` is valid for one root."""
        entry, parts = self._physical_parts(relative, allow_root=True)
        candidate = self._contained(entry.path, parts)
        if not candidate.is_dir():
            raise LibraryPathError("library directory not found")
        return candidate

    def page(self, relative: str = "", *, offset: int = 0, limit: int = 100,
             sort: str = "name") -> tuple[dict, str | None]:
        """Return one sorted page of a directory's immediate playable children."""
        if sort not in SORT_KEYS:
            raise ValueError("invalid library sort")
        if offset < 0 or limit < 1:
            raise ValueError("invalid library page")
        if self._virtual and not relative:
            return self._virtual_root_page(offset=offset, limit=limit, sort=sort)

        entry, parts = self._physical_parts(relative, allow_root=True)
        directory = self.resolve_directory(relative)
        api_relative = relative
        children = self._directory_children(directory, api_relative, sort=sort)
        page_children = children[offset:offset + limit]
        next_offset = offset + len(page_children)
        node = {
            "type": "directory",
            "name": entry.name if self._virtual and not parts else directory.name,
            "path": relative,
            "children": page_children,
        }
        return node, str(next_offset) if next_offset < len(children) else None

    def _virtual_root_page(
        self, *, offset: int, limit: int, sort: str,
    ) -> tuple[dict, str | None]:
        entries = list(self._root_entries)
        if sort == "mtime":
            entries.sort(key=lambda entry: (-_mtime(entry.path), entry.name.casefold()))
        else:
            entries.sort(key=lambda entry: entry.name.casefold())
        children = [
            {"type": "directory", "name": entry.name, "path": entry.name, "children": []}
            for entry in entries
        ]
        page_children = children[offset:offset + limit]
        next_offset = offset + len(page_children)
        node = {
            "type": "directory", "name": "Libraries", "path": "",
            "children": page_children,
        }
        return node, str(next_offset) if next_offset < len(children) else None

    def _directory_children(self, directory: Path, relative: str, sort: str = "name") -> list[dict]:
        if sort == "mtime":
            key = lambda p: (not p.is_dir(), -_mtime(p), p.name.casefold())
        else:
            key = lambda p: (not p.is_dir(), p.name.casefold())
        children: list[dict] = []
        try:
            entries = sorted(directory.iterdir(), key=key)
        except OSError:
            entries = []
        for entry in entries:
            child_rel = f"{relative}/{entry.name}" if relative else entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    children.append({
                        "type": "directory", "name": entry.name, "path": child_rel, "children": [],
                    })
                elif entry.is_file() and entry.suffix.casefold() in PLAYABLE_SUFFIXES:
                    children.append({"type": "file", "name": entry.name, "path": child_rel})
            except OSError:
                continue
        return children
