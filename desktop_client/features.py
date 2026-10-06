"""Desktop preferences and library lifecycle integration kept out of window layout."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path, PurePosixPath
import time

from PySide6.QtCore import QStandardPaths, Qt, QTimer
from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QLabel, QLineEdit, QMenu, QSpinBox

from .browser_state import BrowserStore
from .diagnostics import ClientLog
from .discovery import ServerDiscovery
from .history import HistoryStore, source_key
from .mpv_config import ConfigWatcher, DEFAULTS, MpvConfig
from .playback_state import PlaybackSnapshot, Transitions
from .settings import FAST_FORWARD_MAX_S
from .theme import style_menu
from .widgets import AccentPicker, FlatSwitch, SegmentedSwitch


class DesktopFeatures:
    def _init_feature_state(self):
        self.history = HistoryStore(self.settings._qs, self.settings.history_limit)
        self.browser_state = BrowserStore(self.settings._qs)
        self.transitions = Transitions()
        self._listing_generation = 0
        self._browser_endpoint = None
        self._listing_endpoint = None
        self._restoring_browser = False
        self._transitioning = False
        self._restart_snapshot = None
        self._history_key = None
        self._last_history_save = self._last_log_sample = 0.0
        self._stable_position = False
        self._closing = False
        self._close_ready = False
        self._next_defaults = dict(DEFAULTS)
        self._pending_defaults = {}
        log_root = self.options.log_root or Path(QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation))
        self.client_log = ClientLog(log_root)
        self.client_log.enable(self.settings.file_logging)
        self.client_log.record("application_start")
        self.discovery = ServerDiscovery(self._nearby_changed,
            lambda message: self.client_log.record("discovery_failure", message=message))

    def _init_feature_controls(self, layout):
        def group(title):
            box = QGroupBox(title)
            form = QFormLayout(box)
            form.setRowWrapPolicy(QFormLayout.WrapAllRows)
            layout.addWidget(box)
            return form
        # Video timing: how mpv paces frames. The switch is ours; the mode is
        # mpv's own video-sync option and lives in mpv.conf with the defaults.
        timing_form = group("Video timing")
        self.display_sync_switch = SegmentedSwitch([("Off", "off"), ("Windowed", "windowed"), ("Always", "always")])
        self.display_sync_switch.set_current(self.settings.display_sync)
        self.display_sync_switch.setToolTip(
            "When to tell mpv the display's refresh rate so a display synchronization mode can take effect. "
            "Windowed: while the window is not fullscreen and while entering or leaving fullscreen.")
        self.display_sync_switch.selected.connect(self._set_display_sync)
        timing_form.addRow("Pace video by the display", self.display_sync_switch)
        self.display_sync_hint = QLabel()
        self.display_sync_hint.setWordWrap(True)
        self.display_sync_hint.setProperty("role", "faint")
        form = group("mpv defaults")
        self.mpv_defaults_notice = QLabel("These settings modify mpv.conf and also affect standalone mpv.")
        self.mpv_defaults_notice.setWordWrap(True)
        self.mpv_defaults_notice.setProperty("role", "faint")
        form.addRow(self.mpv_defaults_notice)
        self.mpv_config_label = QLabel()
        self.mpv_config_label.setWordWrap(True)
        self.mpv_config_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(self.mpv_config_label)
        self.mpv_profile_label = QLabel()
        self.mpv_profile_label.setWordWrap(True)
        form.addRow(self.mpv_profile_label)
        self.mpv_controls = {}
        choices = {
            "sid": [("Auto", "auto"), ("Off", "no")],
            "video-sync": [
                ("Audio (default)", "audio"),
                ("Display, resample audio to match (display-resample)", "display-resample"),
                ("Display, repeat or drop video frames (display-vdrop)", "display-vdrop"),
                *((v, v) for v in ("display-resample-vdrop", "display-resample-desync", "display-tempo",
                                   "display-adrop", "display-desync", "desync")),
            ],
            "interpolation": [("Off", "no"), ("On", "yes")],
            "tscale": [(v, v) for v in ("oversample", "linear", "catmull_rom", "mitchell")],
        }
        titles = {"sid": "Default subtitle selection", "slang": "Preferred subtitle languages (in order)",
                  "video-sync": "Video synchronization", "interpolation": "Motion interpolation", "tscale": "Interpolation scaler"}
        for name in DEFAULTS:
            if name == "slang":
                widget = QLineEdit()
                widget.setPlaceholderText("e.g. en,ja")
                widget.editingFinished.connect(lambda: self._edit_mpv_default("slang", self.mpv_controls["slang"].text()))
            else:
                widget = QComboBox()
                for label, value in choices[name]:
                    widget.addItem(label, value)
                widget.activated.connect(lambda _index, key=name: self._edit_mpv_default(key, self.mpv_controls[key].currentData()))
            self.mpv_controls[name] = widget
            (timing_form if name == "video-sync" else form).addRow(titles[name], widget)
        timing_form.addRow(self.display_sync_hint)
        self.mpv_config = None
        try:
            native = getattr(self.player, "mpv", None)
            path = self.options.mpv_config_path
            if path is None and native is not None:
                path = Path(native.command("expand-path", "~~home/mpv.conf"))
            if path is None:
                raise RuntimeError("The mpv configuration path is unavailable without libmpv.")
            self.mpv_config = MpvConfig(path, expand=(lambda p: native.command("expand-path", p)) if native else None)
            self.mpv_config_label.setText(str(path))
            self._refresh_mpv_controls(self.mpv_config.read())
        except Exception as err:
            self.mpv_config_label.setText(str(err))
            for control in self.mpv_controls.values():
                control.setEnabled(False)
        self._show_display_sync_hint()
        if self.mpv_config is not None:
            self.config_watcher = ConfigWatcher(self.mpv_config, self)
            self.config_watcher.changed.connect(self._refresh_mpv_controls)
            self.config_watcher.failed.connect(lambda message: self._error("mpv configuration", message))
        form = group("Controls")
        self.fast_forward_spin = QSpinBox()
        self.fast_forward_spin.setRange(1, FAST_FORWARD_MAX_S)
        self.fast_forward_spin.setSuffix(" s")
        self.fast_forward_spin.setValue(self.settings.fast_forward_s)
        self.fast_forward_spin.setToolTip("How far the skip buttons move forward and back.")
        self.fast_forward_spin.valueChanged.connect(self._set_fast_forward)
        form.addRow("Skip amount", self.fast_forward_spin)
        form = group("Library")
        self.autoplay_check = FlatSwitch("Play the next video automatically")
        self.autoplay_check.setChecked(self.settings.autoplay)
        self.autoplay_check.toggled.connect(lambda value: setattr(self.settings, "autoplay", value))
        form.addRow(self.autoplay_check)
        self.history_limit_spin = QSpinBox()
        self.history_limit_spin.setRange(1, 1000)
        self.history_limit_spin.setValue(self.settings.history_limit)
        self.history_limit_spin.valueChanged.connect(self._set_history_limit)
        form.addRow("History entries", self.history_limit_spin)
        form = group("Appearance")
        self.theme_switch = SegmentedSwitch([("Auto", "auto"), ("Dark", "dark"), ("Light", "light")])
        self.theme_switch.set_current(self.settings.theme_mode)
        self.theme_switch.selected.connect(self._set_theme_mode)
        form.addRow("Theme", self.theme_switch)
        # Accent colour: follow the video, a preset, or any hue.
        self.accent_picker = AccentPicker()
        self.accent_picker.set_value(self.settings.accent)
        self.accent_picker.picked.connect(self._set_accent)
        self.accent_hint = QLabel()
        self.accent_hint.setProperty("role", "faint")
        form.addRow("Accent colour", self.accent_picker)
        form.addRow(self.accent_hint)
        self._show_accent_hint()
        form = group("Diagnostics")
        self.diagnostics_check = FlatSwitch("Show playback diagnostics")
        self.diagnostics_check.setChecked(self.settings.diagnostics)
        self.diagnostics_check.toggled.connect(self._set_diagnostics)
        self.player_status.setVisible(self.settings.diagnostics)
        form.addRow(self.diagnostics_check)
        self.logging_check = FlatSwitch("Write client diagnostic logs")
        self.logging_check.setChecked(self.settings.file_logging)
        self.logging_check.toggled.connect(self._set_logging)
        form.addRow(self.logging_check)
        self.log_path_label = QLabel(str(self.client_log.root))
        self.log_path_label.setWordWrap(True)
        self.log_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(self.log_path_label)
        layout.addStretch(1)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._local_history_menu)
        self.browser_panel.currentChanged.connect(self._browser_tab_changed)
        self.seek_slider.sliderMoved.connect(self._scrub_preview)
        self.seek_slider.cancelled.connect(self._cancel_scrub)
        self._browser_save_timer = QTimer(self)
        self._browser_save_timer.setSingleShot(True)
        self._browser_save_timer.setInterval(250)
        self._browser_save_timer.timeout.connect(self._capture_browser)
        self._feature_timer = QTimer(self)
        self._feature_timer.setInterval(1000)
        self._feature_timer.timeout.connect(self._feature_tick)
        self._feature_timer.start()
        if hasattr(self.player, "telemetry_changed"):
            self.player.telemetry_changed.connect(self._on_telemetry)
        if hasattr(self.player, "log_message"):
            self.player.log_message.connect(lambda level, prefix, message: self.client_log.record("mpv", level=level, prefix=prefix, message=message))
        QTimer.singleShot(0, self._start_feature_services)

    def _start_feature_services(self):
        if self._closing:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.client_log.install_hooks(loop)
        if self.options.discovery:
            self._discovery_task = asyncio.create_task(self.discovery.start())

    def _nearby_changed(self, servers):
        self.nearby_menu.clear()
        if not servers:
            action = self.nearby_menu.addAction("No nearby servers found")
            action.setEnabled(False)
        for server in sorted(servers.values(), key=lambda s: s.name.casefold()):
            action = self.nearby_menu.addAction(f"{server.name} — {server.address}")
            action.triggered.connect(lambda checked=False, address=server.address: self.host_edit.setText(address))

    def _refresh_mpv_controls(self, values):
        self._next_defaults = dict(values)
        self._pending_defaults = {key: value for key, value in self._pending_defaults.items()
                                  if values.get(key) == value}
        if self.mpv_config:
            self.mpv_config_label.setText(str(self.mpv_config.path))
        for name, value in values.items():
            control = self.mpv_controls[name]
            control.setEnabled(True)
            control.blockSignals(True)
            if isinstance(control, QLineEdit):
                control.setText(value)
            else:
                for i in reversed(range(control.count())):
                    if control.itemText(i).startswith("Configured: "):
                        control.removeItem(i)
                index = control.findData(value)
                if index < 0:
                    control.addItem(f"Configured: {value}", value)
                    index = control.count() - 1
                control.setCurrentIndex(index)
            control.blockSignals(False)
        self._show_display_sync_hint()
        if self.mpv_config:
            overrides = [f"{key}={value}" for key, value in self.mpv_config.effective_values.items()
                         if value != values[key]]
            message = ("Global defaults shown. Named or active profiles can override them; profile settings are preserved."
                       if self.mpv_config.profile_options else "External edits apply to the next opened playback session.")
            if overrides:
                message += " Applied profile values: " + ", ".join(overrides)
            self.mpv_profile_label.setText(message)

    def _edit_mpv_default(self, name, value):
        if not self.mpv_config or value == self._next_defaults.get(name):
            return
        try:
            values = self.mpv_config.write(name, str(value))
        except Exception as err:
            self._error("Could not save mpv.conf", str(err))
            if hasattr(self, "config_watcher"):
                self.config_watcher.refresh()
            self._refresh_mpv_controls(self._next_defaults)
            return
        self._refresh_mpv_controls(values)
        self._pending_defaults[name] = str(value)
        self._apply_pending_defaults()

    def _apply_pending_defaults(self):
        if self._transitioning or self._pending_seek_s is not None or getattr(self.player, "_reloading", False):
            return
        if self._session_source and not getattr(self.player, "_epoch_released", True):
            return
        if hasattr(self.player, "apply_defaults") and self._pending_defaults:
            try:
                self.player.apply_defaults(self._pending_defaults)
                self._pending_defaults.clear()
            except Exception as err:
                self._pending_defaults.clear()
                self._error("Could not apply mpv defaults", str(err))

    def _set_history_limit(self, value):
        self.settings.history_limit = value
        self.history.limit = value
        self.history.flush()
        self._refresh_history_labels()

    def _set_fast_forward(self, value):
        self.settings.fast_forward_s = value
        self._show_skip_amount(self.settings.fast_forward_s)

    def _set_theme_mode(self, mode):
        self.settings.theme_mode = mode
        self._apply_theme_mode(mode)

    def _set_display_sync(self, value):
        self.settings.display_sync = value
        self._apply_display_pacing()
        self._show_display_sync_hint()

    def _show_display_sync_hint(self):
        """Say what the switch and the mode add up to; either alone does nothing."""
        mode = self._next_defaults.get("video-sync", "audio")
        display_mode = mode.startswith("display-")
        choice = self.settings.display_sync
        if choice == "windowed" and display_mode:
            text = ("The interface stays fluid while the window is not fullscreen and while entering or "
                    "leaving fullscreen; settled in fullscreen, mpv keeps its own frame timing (less power).")
        elif choice == "always" and display_mode:
            text = ("Video is paced by the display and the interface stays fluid during playback, "
                    "fullscreen included. This draws more power (about 3 W in fullscreen on a laptop).")
        elif choice != "off":
            text = ("No effect yet: Video synchronization is set to audio. "
                    "Choose a Display mode to pace video by the display.")
        elif display_mode:
            text = (f"{mode} is set but inactive: mpv cannot see the display from inside this player. "
                    "Choose Windowed or Always to let it take effect.")
        else:
            text = ("Video is paced by audio. During playback the interface updates once per video frame. "
                    "The mode is saved to mpv.conf.")
        self.display_sync_hint.setText(text)

    def _set_accent(self, value):
        self.settings.accent = value
        self._show_accent_hint()
        self._accent_setting_changed()

    def _show_accent_hint(self):
        value = self.settings.accent
        self.accent_hint.setText(
            "Follows the video that is playing" if value == "auto"
            else "Custom" if self.accent_picker.custom else "")

    def _set_diagnostics(self, value):
        self.settings.diagnostics = value
        self.player_status.setVisible(value)

    def _set_logging(self, value):
        self.settings.file_logging = value
        self.client_log.enable(value)

    def _feature_tick(self):
        if time.monotonic() - self._last_history_save >= 5:
            self._save_history()
        self._apply_pending_defaults()
        path = self.client_log.path or self.client_log.root
        self.log_path_label.setText(f"{path}" + (f"\nLogging stopped: {self.client_log.error}" if self.client_log.error else ""))

    def _on_telemetry(self, sample):
        self._stable_position = sample.stable
        if sample.stable and self._awaiting_first_frame:
            # A session opened paused reports no position change to wait for.
            self._awaiting_first_frame = False
            self._update_loading()
        if self._duration_s is None and sample.duration:
            self._duration_s = sample.duration
        if time.monotonic() - self._last_log_sample >= 10:
            self._last_log_sample = time.monotonic()
            self.client_log.record("telemetry", **asdict(sample))

    def _key_for(self, source, path):
        host = self.client.host if self.client else self.settings.server_host
        port = self.client.port if self.client else self.settings.server_port
        return source_key(source, path, host, port)

    def _save_history(self, *, completed=False):
        if not self._history_key or (not completed and (
            not self._stable_position or self._pending_seek_s is not None or self._slider_down
            or self._transitioning or getattr(self.player, "_reloading", False)
        )):
            return
        self.history.save(self._history_key, self._position_s, self._duration_s, completed=completed)
        self._last_history_save = time.monotonic()
        self._refresh_history_labels()

    def _snapshot(self):
        if not self._session_path or not self._session_source:
            return self._restart_snapshot
        tracks = self.player.track_choices() if hasattr(self.player, "track_choices") else {}
        audio_delay, sub_delay = (self.player.delay_state() if hasattr(self.player, "delay_state")
                                  else (self.audio_delay.value(), self.sub_delay.value()))
        return PlaybackSnapshot(self._session_source, self._session_path, self._position_s,
                                self._duration_s, self._paused, tracks, audio_delay, sub_delay)

    def _refresh_history_labels(self):
        self.local_proxy.invalidate()
        if self.server_model is None:
            return
        def visit(parent):
            for i in range(parent.rowCount()):
                item = parent.child(i)
                path = item.data(Qt.UserRole)
                if path and item.data(Qt.UserRole + 1) == "file":
                    entry = self.history.entries.get(self._key_for("server_file", path))
                    item.setText(PurePosixPath(path).name + (f"  —  {entry.description}" if entry else ""))
                visit(item)
        visit(self.server_model.invisibleRootItem())

    def _history_menu(self, tree, point, source, path):
        if not path:
            return
        menu = QMenu(tree)
        style_menu(menu)
        key = self._key_for(source, path)
        for label, watched in (("Mark watched", True), ("Mark unwatched", False)):
            action = menu.addAction(label)
            action.triggered.connect(lambda checked=False, value=watched: self._mark_history(key, value))
        menu.setAttribute(Qt.WA_DeleteOnClose)
        menu.popup(tree.viewport().mapToGlobal(point))

    def _mark_history(self, key, watched):
        self.history.mark(key, watched)
        self._refresh_history_labels()

    def _local_history_menu(self, point):
        index = self.local_proxy.mapToSource(self.tree.indexAt(point))
        if index.isValid() and not self.fs_model.isDir(index):
            self._history_menu(self.tree, point, "uplink", self.fs_model.filePath(index))

    def _server_history_menu(self, point):
        index = self.server_tree.indexAt(point)
        if index.data(Qt.UserRole + 1) == "file":
            self._history_menu(self.server_tree, point, "server_file", index.data(Qt.UserRole))

    def _browser_tab_changed(self, _index):
        if not self._restoring_browser:
            self.browser_state.tab = self.browser_panel.tabText(self.browser_panel.currentIndex())
            self.browser_state.save()

    def _capture_browser(self):
        if not self._restoring_browser:
            self.browser_state.capture(self._listing_endpoint, self.server_tree, self.server_model)

    def _scrub_preview(self, value):
        if self._duration_s is not None:
            self._slider_down = True
            target = value / 1000 * self._duration_s
            minutes, seconds = divmod(int(target), 60)
            self.pos_label.setText(f"{minutes:02d}:{seconds:02d} ({target - self._position_s:+.1f}s)")

    def _cancel_scrub(self):
        self._slider_down = False
        self._show_position(self._position_s)

    async def _next_sibling(self, snapshot, valid):
        name = PurePosixPath(snapshot.path).name
        current = (name.casefold(), name)
        if snapshot.source == "uplink":
            from .main_window import VIDEO_EXTENSIONS
            allowed = {p[1:] for p in VIDEO_EXTENSIONS}
            def siblings():
                return sorted((p for p in Path(snapshot.path).parent.iterdir()
                               if p.is_file() and p.suffix.casefold() in allowed), key=lambda p: (p.name.casefold(), p.name))
            for path in await asyncio.to_thread(siblings):
                if not valid():
                    return None
                if (path.name.casefold(), path.name) > current:
                    entry = self.history.entries.get(self._key_for("uplink", str(path)))
                    if not entry or not entry.watched:
                        return str(path)
            return None
        if snapshot.source != "server_file" or self.client is None:
            return None
        parent = str(PurePosixPath(snapshot.path).parent)
        parent = "" if parent == "." else parent
        cursor = None
        seen = set()
        while valid():
            kwargs = {"sort": "name"} if "name" in self._server_caps.get("library_sort", []) else {}
            page = await self.client.fetch_library_page(parent, cursor=cursor, limit=100, **kwargs)
            if not valid():
                return None
            for node in page["tree"].get("children", []):
                if node.get("type") == "file" and (node["name"].casefold(), node["name"]) > current:
                    entry = self.history.entries.get(self._key_for("server_file", node["path"]))
                    if not entry or not entry.watched:
                        return node["path"]
            cursor = page.get("next_cursor")
            if cursor is None or cursor in seen:
                return None
            seen.add(cursor)
        return None
