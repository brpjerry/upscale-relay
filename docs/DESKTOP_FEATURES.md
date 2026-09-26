# Desktop library and playback preferences

The desktop client keeps its Local and Server tree browsers. The controls below
are shared by Linux and Windows; no protocol upgrade is required.

## Finding a server

Use **Nearby servers** beside the address field to browse
`_upscalerelay._tcp.local.` advertisements. Selecting a result fills the address;
press **Connect** to connect. Discovery updates and removals never replace a
manually entered address. Manual host/port entry remains available when multicast
is unavailable. The server advertises by default; `relay-server --no-mdns`
disables advertisement. `relay-desktop --no-discovery` disables browsing.

## History, resume, and autoplay

Stable playback progress is saved every five seconds and before stop, source
changes, local fallback, and close. Reopening automatically resumes positions of
at least ten seconds that are more than 90 seconds from the end. The final
90 seconds count as watched. Unknown-duration media retains an explicitly marked
watched state. Scrub previews, pending seeks, and intermediate reload positions
are excluded from history.

Both trees show watched state or percentage and last-played time. Right-click a
video for **Mark watched** or **Mark unwatched**; marking unwatched clears its
resume position. History keeps the 50 most recently played entries by default.
The Library settings group permits a limit from 1 to 1,000. There is no separate
Recent destination or history backup/import command.

Local identities use normalized absolute paths. Server identities combine the
normalized configured hostname/IP and control port with the library-relative
path. Different endpoints remain separate, even if they serve the same file;
alternate DNS names for the same machine are deliberately different identities.

**Play the next video automatically** defaults on. After natural completion and
confirmed current-epoch relay EOS, the next unwatched sibling is opened in
alphabetical order, regardless of browser sorting. Server pages are traversed
lazily. Eligible partial progress on the next file resumes normally. Autoplay
never enters subdirectories or wraps to the beginning, and stops at directory
end or on an open failure. Transport failures and direct local fallback playback
do not auto-advance. Turning autoplay off leaves the browser context in place.

## Sorting and browser restoration

The persisted **Name / Newest** preference applies to both trees. Directories
come first; names are compared without case, including timestamp tie-breaks.
Server requests send `sort=name` or `sort=mtime` only when that key is advertised
in `library_sort`. Older servers keep their supported name order while the
client retains the selected preference.

The selected Local/Server tab and each endpoint's expanded folders, selected
path, and scroll anchor survive refreshes and session transitions. Restoration
loads ancestors before descendants and at most ten pages per directory. Removed
paths fall back to the nearest available ancestor. Without library capability,
Local remains usable. The local root directory continues to be remembered.

## Playback settings and shared mpv configuration

The scrollable settings dock separates Streaming, mpv defaults, Library, and
Diagnostics. Model, stream quality, framing, and resize-filter changes restart
at the cached playback position while preserving pause intent, audio/subtitle
choices (including subtitles off), and delays. Rapid changes coalesce; new user
actions supersede older startup work. Track choices are remapped using type,
language, title, codec, channel/default/forced metadata, and duplicate occurrence
rather than relying on old numeric IDs.

The **mpv defaults** controls edit these global options:

| Control | mpv option |
|---|---|
| Default subtitle selection: Auto, Off, or an existing configured ID | `sid` |
| Ordered preferred subtitle languages, e.g. `en,ja` | `slang` |
| Video synchronization | `video-sync` |
| Motion interpolation | `interpolation` |
| Interpolation scaler | `tscale` |

**These settings modify mpv.conf and also affect standalone mpv.** The dock shows
the file resolved by libmpv's `expand-path ~~home/mpv.conf`, honoring its platform
configuration-directory selection. Language preferences use native mpv semantics;
there is intentionally no additional persistent subtitle-title preference.
The current-video track picker is session-only and takes precedence for that
session. These five defaults are not duplicated in QSettings.

Opening settings never writes the file. Edits reread it and atomically add only
the requested global assignment, preserving comments, unrelated options, named
profiles, newline style, and symlinks. Ordered includes and aliases are read;
configured custom values stay visible. Applied profile values are distinguished
from editable global defaults. Conditional profiles remain managed by mpv.
Unsupported or unreadable configuration is reported without rewriting it.

The file, included files, and their directories are watched, including atomic
replacements. External edits refresh controls and apply at the next newly opened
playback session. GUI changes apply only exposed properties when the player is
stable, or wait until its transition finishes. The application never reloads
the entire configuration into an active stream. Relay-owned options such as
absolute timestamps, buffering, stream timeout, and safe Linux copy-back decode
remain application-controlled. See the [mpv configuration reference](https://mpv.io/manual/stable/#configuration-files)
for native syntax, profiles, and language selection.

Scrubbing displays the proposed timestamp and signed delta. Release commits one
seek; Escape or loss of slider focus cancels it. The **−10 s / +10 s** buttons,
keyboard seeking, and chapter markers use the same bounded seek path. Proposed
timestamps do not replace actual playback progress in history.

## Diagnostics and logs

**Show playback diagnostics** and **Write client diagnostic logs** are independent,
persisted settings, both off by default. Hiding diagnostics leaves metrics,
buffer reports, history, and enabled logging running.

Client logs are timestamped `upscale-relay-client-*.log` files beneath the platform
Documents directory (including redirected Documents locations). Settings show
the actual file path. The writer keeps ten files, rolls at approximately 2 MiB,
and bounds its queue and line length. On overload, records may be dropped;
logging failures are shown in settings and never interrupt playback.

Logs include application/session lifecycle, discovery failures, seek/restart/
autoplay events, telemetry every ten seconds, mpv warnings/errors, and uncaught
Python, thread, and asyncio exceptions. Media URLs and authentication tokens
are redacted before records enter the writer queue. Configuration contents are
not dumped. Existing `--debug` and `--trace` console behavior is unchanged.
Server logs remain separate and retain their own defaults.

## Verification and test isolation

Tests must pass an isolated `settings_scope`. `DesktopOptions.mpv_config_path`
and `log_root` (also `--mpv-config-path` / `--log-root`) override filesystem
locations. An isolated scope or headless mode supplies temporary config/log paths
when overrides are omitted; real discovery is disabled by default in those
modes. Tests never use the user's mpv defaults or production history.

The feature tests cover history policy, configuration merging/watchers, discovery,
sorting/restoration, startup cancellation, pause/track continuity, autoplay,
logging, and scrub controls. Real passthrough tests cover local and server sources,
resume, paused restart, seek, reopen, natural completion, and hidden-diagnostics
buffer reporting. Run native Qt tests through `tests/qt_helpers.py::playback_loop`.

```bash
python -m pytest tests -q
# CPU-only test host, with no NVIDIA encoder:
RELAY_LOSSLESS_HEVC_PROFILE=x265-ultrafast python -m pytest tests -q
# On a machine with an active Wayland compositor:
QT_QPA_PLATFORM=wayland RELAY_TEST_WAYLAND=1 python -m pytest tests/test_desktop_parity_playback.py -q
```

The full Linux suite passed on 2026-09-26: **342 passed, 4 skipped**, using
`RELAY_LOSSLESS_HEVC_PROFILE=x265-ultrafast` on the CPU-only test host. Native
Wayland and headless passthrough checks also passed for both sources. This is not
a new GPU throughput benchmark. Windows playback smoke still needs a Windows
machine with the documented libmpv DLL; it was not run on this Linux host.
Automatic network recovery, actionable performance warnings, fullscreen settings
access, recents, and backups remain outside these changes.
