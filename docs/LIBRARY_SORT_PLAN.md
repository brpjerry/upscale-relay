# Plan: sortable library listings (`GET /library` sort parameter)

Status: **implemented** — server (`relay_server/library.py`,
`relay_server/server.py`) and the Android client both speak this protocol.
The optional per-node `mtime` response field (section 3) remains deferred.

## Motivation

The Android file picker gained a sort toggle: alphabetical (A–Z) or newest
first (by modification time). Local SAF browsing sorts on the client because
the full directory listing is in hand. Server library browsing cannot: pages
arrive through `GET /library` with an offset cursor, so ordering across pages
must come from the server. Sorting only the loaded page on the client would
interleave wrongly with `Load more`.

## Protocol changes

### 1. Capability advertisement

Add one key to the `capabilities` control message, next to `library`:

```json
{
  "type": "capabilities",
  "library": true,
  "library_sort": ["name", "mtime"]
}
```

- `library_sort` lists the sort keys `GET /library` accepts.
- Omit the key (or send `[]`) when no library is configured.
- Clients treat a missing key as "server predates sorting" and fall back to
  the server's fixed name order. The Android client hides its date-sort
  toggle for the server tab in that case, and only adds `sort=` to requests
  when the requested key is present in `library_sort`.

No `protocol_version` bump: the addition is backward and forward compatible
(old clients ignore the new key; new clients probe for it).

### 2. `GET /library` query parameter

```
GET /library?path=<relative-dir>&limit=100&cursor=<offset>&sort=<key>
```

- `sort=name` (default when omitted): the current behavior — directories
  first, then case-folded lexicographic name order. Explicitly passing
  `sort=name` must give exactly the same order as omitting it.
- `sort=mtime`: directories first, then files by **descending** modification
  time (newest first). Ties (identical mtime, e.g. copied sets) break by
  case-folded name ascending so the order is total and stable. Directories
  also order by descending mtime among themselves.
- Unknown `sort` values → HTTP 400, same handling as a malformed `limit`.
  (Do not silently fall back: a silent fallback would be indistinguishable
  from correct results on the client.)
- No separate `order` parameter for now. Each key carries its natural
  direction (`name` ascending, `mtime` descending). If reverse orders are
  ever wanted, extend `library_sort` with explicit `name_desc` /
  `mtime_asc` keys rather than adding a second axis — the capability list
  stays a flat menu the client can render directly.

### 3. Response shape

Unchanged: `{"tree": {...}, "next_cursor": "<offset>|null"}`. Optionally each
file node MAY gain `"mtime": <unix-seconds>` so clients can render dates in
the picker later; the Android client tolerates unknown node keys today, so
this can ship in the same change or be deferred.

## Server implementation sketch

All changes live in `relay_server/library.py` and `relay_server/server.py`.

### `relay_server/library.py`

```python
SORT_KEYS = ("name", "mtime")

def page(self, relative="", *, offset=0, limit=100, sort="name"):
    if sort not in SORT_KEYS:
        raise ValueError("invalid library sort")
    ...
    children = self._directory_children(directory, relative, sort=sort)
```

`_directory_children` today sorts with
`key=lambda p: (not p.is_dir(), p.name.casefold())`. Add the `mtime` variant:

```python
if sort == "mtime":
    key = lambda p: (not p.is_dir(), -_mtime(p), p.name.casefold())
else:
    key = lambda p: (not p.is_dir(), p.name.casefold())
```

with `_mtime` returning `p.stat().st_mtime` and `0.0` on `OSError` (the
iteration loop already tolerates entries vanishing mid-listing; the sort key
must too). Note `entry.stat()` on a `Path` from `iterdir()` performs one
`os.stat` per entry — on network shares (UNC/NAS roots are the documented use
case) this is the expensive part. See "Performance" below.

### `relay_server/server.py` — `handle_library`

```python
sort = request.query.get("sort", "name")
tree, next_cursor = await asyncio.to_thread(
    self.library.page, relative, offset=offset, limit=limit, sort=sort,
)
```

`ValueError` already maps to HTTP 400 in the existing handler.

### `capabilities` message

In the `hello` reply, alongside `"library"`:

```python
"library": self.library is not None,
"library_sort": ["name", "mtime"] if self.library is not None else [],
```

## Pagination semantics (unchanged, but worth stating)

The cursor stays a plain offset into the freshly computed, fully sorted child
list. Two consequences carry over from the existing name-sorted behavior and
are acceptable:

- Each page recomputes the listing; a file added/removed between pages can
  shift entries by one. This is already true today.
- `mtime` order is more volatile than name order (a file being written moves
  toward the front). Same shift-by-one class of artifact; no new mechanism
  needed. If it ever matters, the fix is a snapshot token in the cursor, not
  a client change.

Clients must not mix pages fetched with different `sort` values; the Android
client refetches from offset 0 whenever the toggle changes.

## Performance

- The per-entry `stat()` for mtime runs inside the existing
  `asyncio.to_thread` call, so the event loop is not blocked.
- Directory sizes in the library use case are a few thousand entries at
  worst; one stat per entry per page request is acceptable. If profiling on
  a NAS share says otherwise, add a per-directory `(path, generation)` →
  listing LRU with a short TTL in `MediaLibrary` — an internal change,
  invisible to the protocol.

## Tests to add/extend

`tests/test_server_library.py`:

- `page(sort="name")` equals `page()` (default unchanged).
- `page(sort="mtime")`: directories first, files newest-first, name tiebreak
  (create files with `os.utime` to pin mtimes, including two identical ones).
- Pagination under `sort="mtime"`: `limit=1` walk over three files yields the
  full newest-first sequence and a terminal `next_cursor=None`.
- `page(sort="bogus")` raises `ValueError`; via HTTP → 400.
- Capabilities: `library_sort == ["name", "mtime"]` with `--library`, absent
  or `[]` without.

Desktop client (`desktop_client/main_window.py`) is out of scope; it can
adopt the same parameter later without protocol changes.

## Client status (already shipped in upscale-relay-android)

- `Capabilities.librarySortKeys` parses `library_sort` (default empty).
- `GET /library` requests append `sort=` only when the key is advertised.
- The server-tab sort toggle is hidden unless `mtime` is advertised; the
  local-files toggle always works (client-side sort).
- Auto-advance ("play next file") always requests `sort=name` explicitly so
  episode order is alphabetical regardless of the browse toggle.
