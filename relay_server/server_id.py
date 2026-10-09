"""Stable identity advertised as ``capabilities.server_id``.

Clients key per-server state (history, watched flags, recents) by it instead
of by host and port, which change with DHCP leases and port settings. The id
is generated once and kept in the same per-user state directory as the
single-instance lock, outside any install directory, so it survives restarts,
tray Apply, address changes and upgrades. Deleting the file makes a new one.
It is not a secret.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path

from .instance_lock import lock_dir

log = logging.getLogger("relay.server")

FILE_NAME = "server-id"
# What clients may rely on (docs/PROTOCOL.md): 1-64 URL-safe characters.
VALID_SERVER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def load_or_create_server_id(directory: Path | None = None) -> str | None:
    """Return this install's server id, creating it on first use.

    None when the state directory cannot be read or written: the server then
    advertises no id and clients fall back to host:port.
    """
    path = (directory if directory is not None else lock_dir()) / FILE_NAME
    try:
        existing = _read(path)
        if existing is not None:
            return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        created = uuid.uuid4().hex
        try:
            # Exclusive create: a concurrent first start keeps the winner's id.
            with open(path, "x", encoding="ascii") as file:
                file.write(created + "\n")
            return created
        except FileExistsError:
            existing = _read(path)
            if existing is not None:
                return existing
        # Unreadable or damaged (e.g. truncated by a crash): replace it.
        temporary = path.with_name(f"{FILE_NAME}.{os.getpid()}.tmp")
        temporary.write_text(created + "\n", encoding="ascii")
        os.replace(temporary, path)
        return created
    except OSError as error:
        log.warning("not advertising a server id: %s (%s)", error, path)
        return None


def _read(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="ascii", errors="replace").strip()
    except FileNotFoundError:
        return None
    return value if VALID_SERVER_ID.fullmatch(value) else None
