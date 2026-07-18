"""Sandboxed server-side media library discovery and path resolution."""

from __future__ import annotations

from pathlib import Path, PurePosixPath


PLAYABLE_SUFFIXES = frozenset({".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm"})


class LibraryPathError(ValueError):
    pass


class MediaLibrary:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError(f"library root is not a directory: {self.root}")

    def resolve_file(self, relative: str) -> Path:
        """Resolve a share-relative POSIX path without escaping ``root``."""
        if not relative or "\\" in relative:
            raise LibraryPathError("invalid library path")
        rel = PurePosixPath(relative)
        if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
            raise LibraryPathError("invalid library path")
        candidate = self.root.joinpath(*rel.parts).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as err:
            raise LibraryPathError("library path escapes root") from err
        if not candidate.is_file():
            raise LibraryPathError("library file not found")
        if candidate.suffix.casefold() not in PLAYABLE_SUFFIXES:
            raise LibraryPathError("not a playable library file")
        return candidate

    def tree(self) -> dict:
        return self._directory_node(self.root, "")

    def _directory_node(self, directory: Path, relative: str) -> dict:
        children = []
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        except OSError:
            entries = []
        for entry in entries:
            child_rel = f"{relative}/{entry.name}" if relative else entry.name
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    children.append(self._directory_node(entry, child_rel))
                elif entry.is_file() and entry.suffix.casefold() in PLAYABLE_SUFFIXES:
                    children.append({"type": "file", "name": entry.name, "path": child_rel})
            except OSError:
                continue
        return {
            "type": "directory",
            "name": directory.name,
            "path": relative,
            "children": children,
        }
