# Desktop and Android feature parity

Updated **2026-09-26** after the desktop parity implementation. Discovery,
history/resume, autoplay, sorting/restoration, shared mpv defaults, restart
continuity, seek previews, and optional diagnostics/logging are now delivered.
The remaining gaps include automatic recovery, recents, settings/history backup,
fullscreen settings access, and actionable performance warnings.

## Scope and evidence

Compared the local checkouts at:

| Client | Checkout | Revision |
|---|---|---|
| Desktop | `upscale-relay` | `d24c04c5e6874834b8d0a716f9e4a026d3554ea5` |
| Android | `../upscale-relay-android` | `8bf834ebaef539e4787ee68fafe86d954de0c7cb` |

The original comparison inspected clean working trees at those revisions.
The completed-items section below describes the subsequent desktop working-tree
implementation and its tests. Android evidence remains the original static
review; no new Android device or network-failure tests were run. “Missing” means no
equivalent was found in the shipped desktop code;
it does not rule out a user-supplied mpv script or desktop environment feature.
“Partial” means desktop already supplies part of the behavior, or the engine
supports it without an equivalent application control.

Desktop links point into this repository. Android links are pinned to the
reviewed revision so the evidence remains stable. Source code takes precedence
over roadmap descriptions. The roadmap and agent guidance have been reconciled
with the delivered features.

## Delivered desktop parity

| Feature | Delivered behavior and verification |
|---|---|
| C1 — Discover nearby servers | Nearby-server browsing uses asynchronous zeroconf and the shared DNS-SD service constant. Selection fills the address; Connect is explicit. Adds/updates/removals, stale resolutions, manual entry, and cleanup are tested. [Discovery](../desktop_client/discovery.py), [tests](../tests/test_desktop_discovery.py). |
| L1 — Saved progress, watched state, and resume on reopening | Versioned history saves stable progress every five seconds and on lifecycle boundaries; resume occurs before the first player load. Ten-second/final-90-second thresholds, watched actions, 50-entry default retention (1–1,000), invalid records, and endpoint separation are tested. [History](../desktop_client/history.py), [tests](../tests/test_desktop_state.py). |
| L3 — Play the next unwatched video automatically | Autoplay defaults on, requires confirmed natural relay completion, and walks next unwatched siblings alphabetically across pages. No recursion, wrap, transport-failure advance, or direct-fallback advance. Partial next-file history resumes normally. [Integration](../desktop_client/features.py), [GUI tests](../tests/test_desktop_features_gui.py), [native playback](../tests/test_desktop_parity_playback.py). |
| L4 — Choose and remember name/date sorting | One persisted Name/Newest preference drives the local proxy and capability-gated server requests. Unsupported servers keep name order; listing generations reject stale replies. [Browser state](../desktop_client/browser_state.py), [client API](../relay_client_core/client.py). |
| L5 — Restore the server browsing location and navigation context | Selected tab and endpoint-specific expanded directories, selection, and scroll anchors restore across refresh/reconnect, with ancestors first, a ten-page-per-directory limit, and nearest-ancestor fallback. The existing trees remain. [Browser state](../desktop_client/browser_state.py), [tests](../tests/test_desktop_features_gui.py). |
| P1 — Remember subtitle defaults and the preferred subtitle | GUI subtitle defaults (`sid`) and ordered language preferences (`slang`) persist in shared `mpv.conf`, with explicit standalone-mpv disclosure. Current-file selection is session-only. **Intentional difference:** no additional persistent track-title preference; native language semantics are used. [Configuration adapter](../desktop_client/mpv_config.py), [feature guide](DESKTOP_FEATURES.md). |
| P2 — Preserve pause and track choices when applying playback settings | Serialized/coalesced model, quality, framing, and resize restarts retain cached position, pause intent (including changes during startup), metadata-based track choices, subtitles off, and delays. [State](../desktop_client/playback_state.py), [native playback tests](../tests/test_desktop_parity_playback.py). |
| P3 — Remap selected tracks when numeric IDs change | Implemented as a dependency of P2: matching uses track type, language, title, codec, channel/default/forced metadata, and duplicate occurrence. Changed IDs, duplicate tracks, type separation, and missing candidates are tested. [Matching](../desktop_client/playback_state.py), [tests](../tests/test_desktop_state.py). |
| P4 — GUI controls for display-resample sync and interpolation | The settings dock exposes video sync, interpolation, and tscale through shared `mpv.conf`, retaining custom values and preserving unrelated configuration/profiles. External edits defer to the next session; GUI property changes wait for stable playback. [Adapter](../desktop_client/mpv_config.py), [tests](../tests/test_desktop_config.py). |
| P6 — Show the proposed seek time during scrubbing; provide short-seek buttons | Scrubbing shows the proposed timestamp/delta, commits once on release, and supports cancellation without a seek. Explicit ±10-second buttons share the bounded relay/local seek path. Chapter markers and keyboard controls remain. [GUI tests](../tests/test_desktop_features_gui.py). |
| D2 — Opt-in diagnostic file logging from settings | Opt-in timestamped Documents logs retain ten files, roll by size, use a bounded asynchronous queue, redact URLs/tokens before queuing, and record lifecycle, ten-second telemetry, mpv warnings/errors, and chained exception handlers. [Logger](../desktop_client/diagnostics.py), [tests](../tests/test_desktop_logging.py). |
| D3 — Toggle application diagnostics independently of playback controls | Diagnostic display is an independent persisted toggle, off by default, and hides only the label. Pacing, history, metrics, and enabled logging continue. Real passthrough tests assert buffer reporting with diagnostics hidden. [Native tests](../tests/test_desktop_parity_playback.py). |

The Linux checks include pure policy tests, Qt GUI tests, real headless libmpv
passthrough, and **native Wayland passthrough for both local and server sources**.
The full CPU-profile suite passed: **342 passed, 4 skipped**. The native checks
cover saved resume, paused restart, seek, reopening, and natural completion
into autoplay. Windows native smoke was not run on this Linux host. See the
[desktop guide](DESKTOP_FEATURES.md#verification-and-test-isolation) for commands
and configuration/log isolation. This does not claim new NVIDIA throughput or
Android device verification.

The sections below list only remaining gaps and intentional platform differences.

## Connection and recovery

| ID / feature | Android implementation | Desktop gap and evidence |
|---|---|---|
| C2 — Automatic reconnect and playback resume | Classifies recoverable failures, retries with exponential backoff, reopens the same local/server source at its captured position, and exposes recovery status, cancellation, and local fallback. The policy allows up to six attempts, with a 60-second eligibility budget and delays capped at eight seconds. It also reconnects the library while browsing and reacts to restored connectivity/foregrounding. [Recovery flow][a-recovery], [policy][a-reconnect-policy], [recovery UI][a-recovery-ui]. | **Missing as a recovery workflow.** Desktop failure handling shows an error and tears down playback, clearing its source and position. Teardown may make one fresh control connection, but does not resume the video or run a retry policy. The existing “Auto connect” setting only acts on launch. [Failure/teardown][d-teardown], [startup auto-connect][d-startup]. Local fallback exists during an active local-source session, but is disabled by failure teardown; Android keeps it available from recovery/error UI. |
| C3 — Detect a live connection that has stopped producing playable media | Uses WebSocket pings, a startup/seek watchdog that fails after 60 seconds without playable media or advancing subtitle-index progress, and a playback watchdog for 15 seconds without incoming bytes while the player is starved. These feed the recovery/error flow. [Control configuration][a-control], [startup/seek watchdog][a-seek-watchdog], [playback watchdog][a-watchdogs]. | **Partial.** Desktop has request/open timeouts and progress callbacks. It waits for `seek_ready`, but does not then impose an equivalent deadline for playable media. Its downlink socket becomes blocking without a read timeout, and the player can wait on its queue indefinitely. No explicit WebSocket heartbeat is configured. A healthy user pause must remain exempt from any future watchdog. [Control/requests][d-control], [receiver][d-receiver], [seek][d-seek], [consumer][d-consumer]. |
| C4 — Repair a large A/V synchronization gap automatically | In steady playback, a residual A/V error of at least two seconds can trigger a new epoch at the audio position, with a cooldown. This addresses gaps after Android Surface/background transitions and uses residual synchronization error so deliberate audio delay is not treated as a fault. [Watchdogs][a-watchdogs]. | **Missing as application-level recovery.** Desktop displays drift and relies on mpv's normal synchronization; it has no comparable corrective relay seek. [Stats loop][d-stats]. This is a robustness difference, not evidence that desktop needs Android's exact thresholds or suffers the same Surface problem. |

## Library, history, and preferences

| ID / feature | Android implementation | Desktop gap and evidence |
|---|---|---|
| L2 — Recent files and recent local folders | Stores bounded lists of server files, local documents, and local roots. Server files appear in a Recent destination, with a clear action; local recents and roots appear in Local. [Preferences][a-prefs], [Recent UI][a-recents], [Local UI][a-local-ui]. | **Missing.** Desktop remembers one local browse directory, with no recent-file list or recent-folder shortcuts. [Settings][d-settings], [browser setup][d-browser]. Android's Recent page is specifically for server files, not a combined cross-source history page. |
| L6 — Export/import settings and history | Provides human-readable JSON backup of connection and playback preferences, recents, and watch history. Import validates data, preserves existing values for absent fields, and reports status. [Backup UI][a-backup-ui], [codec][a-backup-codec]. | **Missing.** Desktop has QSettings persistence but no application export/import commands or portable backup format. [Settings][d-settings], [window controls][d-window]. Copying a platform-specific settings file manually is not the same workflow. |
| L7 — Preview the selected server file before starting playback | Shows the selected filename plus configured model, quality, framing, and resize filter, with an explicit Play action. [Library detail][a-library-detail]. | **Missing as a browse view.** Desktop's server tree displays names/icons and starts files on double-click; there is no corresponding selection detail panel. [Server tab][d-server-tab], [file activation][d-open]. This Android panel is a selection summary, not a metadata database or thumbnail feature. |

## Player behavior and controls

| ID / feature | Android implementation | Desktop gap and evidence |
|---|---|---|
| P5 — Change model/quality/filter without leaving fullscreen | The immersive player includes Model and Playback settings actions. [Player controls][a-player-chrome]. | **Partial.** Desktop supports these settings in windowed playback, but fullscreen hides both the toolbar toggle and the settings dock. The revealed transport overlay contains tracks, chapters, volume, and playback controls, but no settings opener. [Fullscreen transition][d-fullscreen], [transport construction][d-transport]. |

## Diagnostics and visible feedback

| ID / feature | Android implementation | Desktop gap and evidence |
|---|---|---|
| D1 — Actionable performance warnings | Shows dismissible warnings after repeated rebuffers or sustained decoder drops, suggesting a smaller model or lower-bandwidth quality. [Detection][a-watchdogs], [warning UI][a-player-screen]. | **Missing.** Desktop reports buffering, cache, throughput, decoder, drift, and dropped-frame values, but does not turn sustained problems into these warnings or suggestions. [Stats loop][d-stats], [buffering handler][d-position]. Android's server warning is a rebuffer heuristic, not proof that inference rather than network throughput is the bottleneck. |
| D4 — Keep loading/seek/buffering progress visible in immersive playback | Renders preparation, seek progress, buffering, and recovery overlays outside the auto-hidden controls. Short waits use a delayed overlay to avoid flashes. [Player screen][a-player-screen], [loading overlay][a-loading-ui]. | **Partial.** Desktop already has an opening indicator and handles server opening/seek/subtitle-index progress. Seek and buffering messages are placed in the status bar, which fullscreen hides; there is no equivalent relay overlay on the fullscreen video. [Progress handlers][d-progress], [buffering handler][d-position], [fullscreen][d-fullscreen]. Ordinary progress support is therefore not missing. |

## Android features requiring a desktop-specific design

These are present in Android and lack an application-level desktop equivalent,
but their Android APIs or interaction patterns should not be copied literally.

| ID / feature | Android implementation | Desktop comparison |
|---|---|---|
| O1 — System media controls and metadata | A MediaSession publishes title, duration, and position; notification/lock-screen controls and system media keys route play, pause, seek, stop, and chapter actions through relay-aware handlers. [MediaSession][a-system-media], [service][a-service]. | No desktop media-session integration was found. Keyboard events received by the player are supported, and Linux D-Bus is used for screen-idle inhibition, but neither provides an application media-control service. [Keys][d-keys], [idle inhibition][d-idle]. A desktop equivalent would need platform-specific integration. |
| O2 — Picture-in-Picture | Enters Android PiP with the stream's aspect ratio, a play/pause action, and a retained playback Surface. [Activity][a-activity], [player screen][a-player-screen]. | Desktop has windowed/fullscreen playback, with no application mini-player/PiP mode. A compositor's independent window rules are outside this comparison. [Fullscreen/window behavior][d-fullscreen]. |
| O3 — Explicit background policy and audio-focus behavior | Offers continue-in-background versus pause-on-background, uses a foreground service with wake/Wi-Fi locks, and pauses/resumes for audio-focus transitions. [Lifecycle policy][a-background], [service][a-service], [audio focus][a-system-media]. | Desktop can already keep playing while its window is unexposed and handles hidden render updates. What is missing is an application background-policy setting and audio-focus response, not basic background playback. Android power locks have no direct one-to-one desktop requirement. [Hidden rendering][d-hidden-rendering], [settings][d-settings]. |
| O4 — Touch gestures and control lock | Horizontal drags preview/commit seeks; vertical drags adjust brightness or volume. A setting enables gestures and a player lock prevents accidental gesture/control activation. [Gestures][a-gestures], [control lock][a-player-screen]. | Desktop supports mouse/keyboard controls and double-click fullscreen but has no touch gesture layer, per-window brightness gesture, or equivalent control lock. This is primarily relevant to touch-enabled desktops. [Input handlers][d-keys]. |

## Features already shared, or not yet present in Android

The following should **not** be counted as desktop omissions:

| Capability | Evidence / qualification |
|---|---|
| Local and server-library relay playback, paginated server browsing | Desktop implements both source paths and “Load more.” [Server browsing][d-server-tab], [session start][d-session-start]. Android SAF is its platform's file-access mechanism, not a missing desktop filesystem capability. |
| Model selection, HEVC quality ladder, fit/cover, resize filter, debanding | Already exposed by desktop, including active-session restarts. P2 and P4 are delivered above; fullscreen access (P5) remains. [Settings panel][d-settings-panel], [settings handlers][d-settings-restart]. |
| Play/pause, volume/mute, seeking, chapters, audio/subtitle selection and delays | Already implemented in desktop. Different icons, keyboard aliases, or screen layouts do not make the underlying controls absent. [Transport][d-transport], [player methods][d-track-selection]. |
| Muxed server audio/subtitles and verified cached fonts | Implemented on both. Desktop negotiates capabilities, honors confirmed fallback, and prepares fonts before player loading. [Desktop startup][d-session-start], [Android current architecture][a-readme]. |
| Local fallback | Desktop can switch an active uplink session to the original file at its current position, preserving pause intent. C2 describes the narrower gap in availability after failure. [Fallback][d-fallback]. |
| Fullscreen with auto-hidden controls, system theme adoption, keeping the display awake | Desktop already has fullscreen overlays, palette/theme handling, and Linux idle inhibition. [Fullscreen][d-fullscreen], [theme][d-theme], [idle inhibition][d-idle]. Android's adaptive phone layouts are platform presentation, not evidence these desktop capabilities are absent. |
| Direct SMB browsing/credential storage, shared-mount mapping, pairing, TLS | These remain deferred in the Android roadmap as well; they are not Android-to-desktop parity gaps. OS-mounted shares already work through desktop file access. [Android remaining work][a-roadmap], [protocol security status](PROTOCOL.md#7-security-status). |
| FFV1 | Desktop supports it. Android deliberately omits it and supports the HEVC tiers. [Android README][a-readme]. |

## Interpretation and follow-up

Remaining application work is C2–C4 (recovery and watchdogs), L2/L6/L7
(recents, backups, and browse detail), P5 (fullscreen settings access), and
D1/D4 (actionable performance warnings and immersive progress). The platform
features O1–O4 still need desktop-specific product decisions. There is no new
protocol version or automatic network-recovery policy in this implementation.

Useful existing regression specifications include Android's
[reconnect-policy tests][a-reconnect-tests], [seek-progress tests][a-seek-tests],
[history-state tests][a-history-tests], [backup tests][a-backup-tests],
[track-remapping tests][a-player-tests], and
[paused seek/settings restart device test][a-restart-test]. Desktop's
[library/player GUI tests][d-gui-tests] document much of the shared baseline.
Those Android regression specifications were inspected, not executed. Desktop
verification is recorded in the delivered-items section above.

[d-window]: ../desktop_client/main_window.py
[d-connect]: ../desktop_client/main_window.py
[d-mdns]: ../relay_server/mdns.py
[d-startup]: ../desktop_client/main_window.py
[d-teardown]: ../desktop_client/main_window.py
[d-control]: ../relay_client_core/client.py
[d-receiver]: ../relay_client_core/client.py
[d-seek]: ../relay_client_core/client.py
[d-consumer]: ../desktop_client/mpv_view.py
[d-stats]: ../desktop_client/mpv_view.py
[d-open]: ../desktop_client/main_window.py
[d-position]: ../desktop_client/main_window.py
[d-settings]: ../desktop_client/settings.py
[d-browser]: ../desktop_client/main_window.py
[d-library-api]: ../relay_client_core/client.py
[d-sort-plan]: LIBRARY_SORT_PLAN.md
[d-library-refresh]: ../desktop_client/main_window.py
[d-server-tab]: ../desktop_client/main_window.py
[d-player-start]: ../desktop_client/mpv_view.py
[d-track-selection]: ../desktop_client/mpv_view.py
[d-mpv-config]: ../desktop_client/mpv_view.py
[d-settings-restart]: ../desktop_client/main_window.py
[d-session-start]: ../desktop_client/main_window.py
[d-settings-panel]: ../desktop_client/main_window.py
[d-fullscreen]: ../desktop_client/main_window.py
[d-transport]: ../desktop_client/main_window.py
[d-slider]: ../desktop_client/main_window.py
[d-app]: ../desktop_client/app.py
[d-progress]: ../desktop_client/main_window.py
[d-keys]: ../desktop_client/mpv_view.py
[d-idle]: ../desktop_client/idle_inhibit.py
[d-hidden-rendering]: ../desktop_client/mpv_view.py
[d-fallback]: ../desktop_client/main_window.py
[d-theme]: ../desktop_client/theme.py
[d-gui-tests]: ../tests/test_server_library_gui.py
[a-discovery]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/ServerDiscovery.kt#L14
[a-connection-ui]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L1000
[a-recovery]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L1674
[a-reconnect-policy]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/relay-client/src/main/kotlin/org/upscalerelay/client/ReconnectPolicy.kt
[a-recovery-ui]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L2256
[a-control]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/relay-client/src/main/kotlin/org/upscalerelay/client/ControlChannel.kt#L54
[a-seek-watchdog]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/relay-client/src/main/kotlin/org/upscalerelay/client/RelaySessionController.kt#L487
[a-watchdogs]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L2483
[a-progress]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L2286
[a-watched]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L584
[a-history-ui]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L578
[a-prefs]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/AppPreferences.kt#L18
[a-recents]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L958
[a-local-ui]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L410
[a-autoplay]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L2046
[a-player-settings]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L1086
[a-sort]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L716
[a-sort-ui]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L751
[a-browse-restore]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L810
[a-scroll]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L279
[a-backup-ui]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L1149
[a-backup-codec]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/BackupCodec.kt
[a-library-detail]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L915
[a-subtitles]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L1370
[a-restart]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L1894
[a-preserve-tracks]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L2650
[a-track-mapping]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/player-mpv/src/main/kotlin/org/upscalerelay/player/mpv/MpvPlayerEngine.kt#L779
[a-player-tests]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/player-mpv/src/test/kotlin/org/upscalerelay/player/mpv/MpvPlayerEngineTest.kt#L43
[a-video-sync-ui]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L1196
[a-video-sync]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/player-mpv/src/main/kotlin/org/upscalerelay/player/mpv/MpvPlayerEngine.kt#L187
[a-player-chrome]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L1613
[a-seek-ui]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L1829
[a-player-screen]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L1281
[a-logger]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/FileLogger.kt
[a-metrics]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L2391
[a-loading-ui]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L2201
[a-system-media]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L2172
[a-service]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/PlaybackService.kt
[a-activity]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/MainActivity.kt
[a-background]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayViewModel.kt#L180
[a-gestures]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/main/kotlin/org/upscalerelay/android/RelayApp.kt#L1506
[a-readme]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/README.md
[a-roadmap]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/docs/ANDROID_CLIENT.md#phase-6--smb-endurance-security-and-release
[a-reconnect-tests]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/relay-client/src/test/kotlin/org/upscalerelay/client/ReconnectPolicyTest.kt
[a-seek-tests]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/relay-client/src/test/kotlin/org/upscalerelay/client/SeekProgressTrackerTest.kt
[a-history-tests]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/test/kotlin/org/upscalerelay/android/PhaseThreeStateTest.kt
[a-backup-tests]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/test/kotlin/org/upscalerelay/android/BackupCodecTest.kt
[a-restart-test]: https://github.com/brpjerry/upscale-relay-android/blob/8bf834ebaef539e4787ee68fafe86d954de0c7cb/app/src/androidTest/kotlin/org/upscalerelay/android/AuditPlaybackDeviceTest.kt#L83
