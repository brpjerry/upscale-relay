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

    def resolve_directory(self, relative: str) -> Path:
        """Resolve a share-relative directory, allowing ``""`` for the root."""
        if "\\" in relative:
            raise LibraryPathError("invalid library path")
        if not relative:
            return self.root
        rel = PurePosixPath(relative)
        if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
            raise LibraryPathError("invalid library path")
        candidate = self.root.joinpath(*rel.parts).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as err:
            raise LibraryPathError("library path escapes root") from err
        if not candidate.is_dir():
            raise LibraryPathError("library directory not found")
        return candidate

    def page(self, relative: str = "", *, offset: int = 0, limit: int = 100) -> tuple[dict, str | None]:
        """Return one sorted page of a directory's immediate playable children."""
        if offset < 0 or limit < 1:
            raise ValueError("invalid library page")
        directory = self.resolve_directory(relative)
        children = self._directory_children(directory, relative)
        page_children = children[offset:offset + limit]
        next_offset = offset + len(page_children)
        node = {
            "type": "directory",
            "name": directory.name,
            "path": relative,
            "children": page_children,
        }
        return node, str(next_offset) if next_offset < len(children) else None

    def _directory_children(self, directory: Path, relative: str) -> list[dict]:
        children: list[dict] = []
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
                    children.append({
                        "type": "directory", "name": entry.name, "path": child_rel, "children": [],
                    })
                elif entry.is_file() and entry.suffix.casefold() in PLAYABLE_SUFFIXES:
                    children.append({"type": "file", "name": entry.name, "path": child_rel})
            except OSError:
                continue
        return children
