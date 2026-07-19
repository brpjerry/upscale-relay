"""Main window: file browser + server bar + player."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from PySide6.QtCore import QDir, QEvent, Qt, QTimer
from PySide6.QtGui import QCursor, QIcon, QPainter, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileSystemModel,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSplitter,
    QStatusBar,
    QStyle,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)
from qasync import asyncSlot

from relay_client_core import RelayClient, SessionConfig

from .chapters import (
    Chapter,
    chapter_index,
    normalize_chapters,
    slider_fractions,
    step_target,
)
from .settings import AppSettings
from .options import DesktopOptions

try:
    from .mpv_view import MpvPlayerView as PlayerView
    PLAYER_BACKEND = "mpv"
except (ImportError, OSError):
    from .player_view import VideoPreviewView as PlayerView
    PLAYER_BACKEND = "preview (video-only; libmpv not found)"

VIDEO_EXTENSIONS = ["*.mkv", "*.mp4", "*.m4v", "*.avi", "*.mov", "*.ts", "*.webm"]


def _format_time(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


class SeekSlider(QSlider):
    """QSlider whose groove clicks jump straight to the clicked position.

    Stock QSlider treats a groove click as one page-step. Moving the handle
    under the cursor before the default press handling means the click both
    jumps to the timestamp and starts a drag from there.

    Chapter starts (as 0..1 fractions) are painted as tick marks over the
    groove so chapter boundaries are visible while scrubbing.
    """

    def __init__(self, *args) -> None:
        super().__init__(*args)
        self._chapter_fractions: list[float] = []

    def set_chapter_marks(self, fractions: list[float]) -> None:
        self._chapter_fractions = fractions
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(),
                round(event.position().x()), self.width(),
            )
            self.setSliderPosition(value)
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._chapter_fractions:
            return
        painter = QPainter(self)
        color = self.palette().windowText().color()
        color.setAlpha(150)
        span = self.maximum() - self.minimum()
        mid_y = self.height() // 2
        for fraction in self._chapter_fractions:
            # Mirror mousePressEvent's mapping so ticks line up with where a
            # click on that timestamp would land the handle.
            x = QStyle.sliderPositionFromValue(
                self.minimum(), self.maximum(),
                round(self.minimum() + fraction * span), self.width(),
            )
            painter.fillRect(x - 1, mid_y - 4, 2, 8, color)
        painter.end()


class MainWindow(QMainWindow):
    def __init__(self, options: DesktopOptions | None = None):
        super().__init__()
        self.options = options or DesktopOptions()
        self.setWindowTitle("Upscale Relay")
        self.resize(1200, 700)
        self.settings = AppSettings(self.options.settings_scope)
        self.client: RelayClient | None = None

        # -- toolbar: server + session config --------------------------------
        bar = QToolBar("server")
        bar.setMovable(False)
        self.addToolBar(bar)
        self._toolbar = bar  # hidden in fullscreen
        self.browser_toggle = QToolButton()
        self.browser_toggle.setCheckable(True)
        self.browser_toggle.setChecked(self.settings.browser_visible)
        self.browser_toggle.setIcon(self._icon("folder", QStyle.SP_DirIcon))
        self.browser_toggle.setToolTip("Show/hide the file browser")
        self.host_edit = QLineEdit(f"{self.settings.server_host}:{self.settings.server_port}")
        self.host_edit.setFixedWidth(160)
        self.host_edit.setPlaceholderText("host:port")
        self.connect_btn = QPushButton("Connect")
        self.autoconnect_check = QCheckBox("auto")
        self.autoconnect_check.setToolTip("Connect to this server automatically on launch")
        self.autoconnect_check.setChecked(self.settings.auto_connect)
        self.model_combo = QComboBox()
        self.model_combo.addItem(self.settings.model)
        self.tier_combo = QComboBox()
        self.tier_combo.addItem(self.settings.quality_tier, self.settings.quality_tier)
        # "Fit to screen" letterboxes inside the display; "Crop" requests a
        # frame sized to cover the display and pans/scans off the overflow, so
        # a fullscreen fill shows native (server-upscaled) pixels edge to edge.
        self.fit_combo = QComboBox()
        self.fit_combo.addItem("Fit to screen", "fit")
        self.fit_combo.addItem("Crop", "cover")
        fit_idx = self.fit_combo.findData(self.settings.fit_mode)
        self.fit_combo.setCurrentIndex(fit_idx if fit_idx >= 0 else 0)
        self.resize_combo = QComboBox()
        self.resize_combo.addItem("Server default", None)
        if self.settings.resize_algorithm:
            self.resize_combo.addItem(self.settings.resize_algorithm, self.settings.resize_algorithm)
            self.resize_combo.setCurrentIndex(1)
        self.deband_check = QCheckBox("deband")
        self.deband_check.setToolTip(
            "Apply mpv's GPU debanding after decode (does not alter the ONNX input)"
        )
        self.deband_check.setChecked(self.settings.deband_enabled)
        self.conn_label = QLabel("disconnected")
        bar.addWidget(self.browser_toggle)
        bar.addWidget(QLabel(" server "))
        bar.addWidget(self.host_edit)
        bar.addWidget(self.connect_btn)
        bar.addWidget(self.autoconnect_check)
        bar.addWidget(QLabel("  model "))
        bar.addWidget(self.model_combo)
        bar.addWidget(QLabel("  quality "))
        bar.addWidget(self.tier_combo)
        bar.addWidget(QLabel("  display "))
        bar.addWidget(self.fit_combo)
        bar.addWidget(QLabel("  resize "))
        bar.addWidget(self.resize_combo)
        bar.addWidget(QLabel("  "))
        bar.addWidget(self.deband_check)
        bar.addWidget(QLabel("  "))
        bar.addWidget(self.conn_label)

        # -- file browser ------------------------------------------------------
        self.fs_model = QFileSystemModel()
        self.fs_model.setRootPath(QDir.rootPath())
        self.fs_model.setNameFilters(VIDEO_EXTENSIONS)
        self.fs_model.setNameFilterDisables(False)
        self.tree = QTreeView()
        self.tree.setModel(self.fs_model)
        start_dir = self.settings.browse_dir or QDir.homePath()
        self.tree.setRootIndex(self.fs_model.index(start_dir))
        for col in range(1, 4):
            self.tree.hideColumn(col)
        self.tree.setHeaderHidden(True)

        self.up_btn = QToolButton()
        self.up_btn.setIcon(self._icon("go-up", QStyle.SP_FileDialogToParent))
        self.up_btn.setToolTip("Up one directory")
        self.home_btn = QToolButton()
        self.home_btn.setIcon(self._icon("go-home", QStyle.SP_DirHomeIcon))
        self.home_btn.setToolTip("Home directory")
        self.path_edit = QLineEdit(start_dir)
        self.path_edit.setPlaceholderText("directory path")
        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.addWidget(self.up_btn)
        nav.addWidget(self.home_btn)
        nav.addWidget(self.path_edit)
        self.local_browser_panel = QWidget()
        bv = QVBoxLayout(self.local_browser_panel)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.addLayout(nav)
        bv.addWidget(self.tree)

        # With only Local present the tab strip is hidden, preserving the
        # pre-library appearance. A Server tab is created only while connected
        # to a server advertising the library capability.
        self.browser_panel = QTabWidget()
        self.browser_panel.addTab(self.local_browser_panel, "Local")
        self.browser_panel.tabBar().setVisible(False)
        self.server_browser_panel: QWidget | None = None
        self.server_tree: QTreeView | None = None
        self.server_model: QStandardItemModel | None = None
        self.server_placeholder: QLabel | None = None
        self.server_refresh_btn: QToolButton | None = None

        # -- player -------------------------------------------------------------
        self.player = PlayerView(options=self.options)
        if hasattr(self.player, "set_deband"):
            self.player.set_deband(self.settings.deband_enabled)
        self._icon_play = self._icon("media-playback-start", QStyle.SP_MediaPlay)
        self._icon_pause = self._icon("media-playback-pause", QStyle.SP_MediaPause)
        self.play_btn = QToolButton()
        self.play_btn.setIcon(self._icon_pause)
        self.play_btn.setToolTip("Pause (Space)")
        self.play_btn.setEnabled(False)
        self.stop_btn = QToolButton()
        self.stop_btn.setIcon(self._icon("media-playback-stop", QStyle.SP_MediaStop))
        self.stop_btn.setToolTip("Stop")
        self.stop_btn.setEnabled(False)
        self.chapter_prev_btn = QToolButton()
        self.chapter_prev_btn.setIcon(
            self._icon("media-skip-backward", QStyle.SP_MediaSkipBackward))
        self.chapter_prev_btn.setToolTip("Previous chapter (PgDn)")
        self.chapter_next_btn = QToolButton()
        self.chapter_next_btn.setIcon(
            self._icon("media-skip-forward", QStyle.SP_MediaSkipForward))
        self.chapter_next_btn.setToolTip("Next chapter (PgUp)")
        self.chapter_combo = QComboBox()
        self.chapter_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.chapter_combo.setToolTip("Jump to a chapter")
        # Hidden until a session with chapters starts (_set_chapters).
        for w in (self.chapter_prev_btn, self.chapter_combo, self.chapter_next_btn):
            w.setVisible(False)
        self.fullscreen_btn = QToolButton()
        self.fullscreen_btn.setIcon(self._icon("view-fullscreen", QStyle.SP_TitleBarMaxButton))
        self.fullscreen_btn.setToolTip("Fullscreen — F or double-click the video; Esc exits")
        self.fallback_btn = QPushButton("Play locally")
        self.fallback_btn.setEnabled(False)
        self.fallback_btn.setToolTip("Drop the upscaler and play the original file directly")
        self.player_status = QLabel("")

        self.seek_slider = SeekSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setEnabled(False)
        self.pos_label = QLabel("--:-- / --:--")
        seek_row = QHBoxLayout()
        seek_row.addWidget(self.seek_slider, stretch=1)
        seek_row.addWidget(self.pos_label)

        self.sub_combo = QComboBox()
        self.sub_combo.addItem("no subs", None)
        self.sub_combo.setEnabled(False)
        self.sub_delay = QDoubleSpinBox()
        self.sub_delay.setRange(-30.0, 30.0)
        self.sub_delay.setSingleStep(0.1)
        self.sub_delay.setSuffix(" s")
        self.sub_delay.setEnabled(False)

        transport = QHBoxLayout()
        transport.addWidget(self.play_btn)
        transport.addWidget(self.stop_btn)
        transport.addWidget(self.chapter_prev_btn)
        transport.addWidget(self.chapter_combo)
        transport.addWidget(self.chapter_next_btn)
        transport.addWidget(self.fullscreen_btn)
        transport.addWidget(self.fallback_btn)
        transport.addWidget(QLabel(" subs "))
        transport.addWidget(self.sub_combo)
        transport.addWidget(self.sub_delay)
        transport.addWidget(self.player_status, stretch=1)
        # Controls live in one hideable panel so fullscreen is just the video.
        self.controls_panel = QWidget()
        cv = QVBoxLayout(self.controls_panel)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addLayout(seek_row)
        cv.addLayout(transport)
        player_page = QWidget()
        pv = QVBoxLayout(player_page)
        pv.setContentsMargins(0, 0, 0, 0)
        pv.addWidget(self.player, stretch=1)
        pv.addWidget(self.controls_panel)

        # In fullscreen the same panel is re-parented onto the video as a
        # bottom overlay, hidden until the pointer nears the bottom edge.
        self._controls_layout = pv
        self._controls_overlay = False
        self.controls_panel.setObjectName("overlayControls")
        self._controls_timer = QTimer(self)
        self._controls_timer.setSingleShot(True)
        self._controls_timer.setInterval(2200)
        self._controls_timer.timeout.connect(self._auto_hide_controls)
        self.player.installEventFilter(self)  # reposition overlay on resize

        self.split = QSplitter()
        self.split.addWidget(self.browser_panel)
        self.split.addWidget(player_page)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([300, 900])
        self._browser_sizes = [300, 900]  # restored when the browser is re-shown
        self.setCentralWidget(self.split)
        self.setStatusBar(QStatusBar())
        # Indeterminate busy bar for session open — visible while the server
        # prepares the pipeline (a first-use TensorRT engine build can run for
        # minutes; session_progress messages narrate it in the status bar).
        self.open_progress = QProgressBar()
        self.open_progress.setRange(0, 0)
        self.open_progress.setFixedWidth(140)
        self.open_progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.open_progress)
        self.statusBar().showMessage(f"player backend: {PLAYER_BACKEND}")

        # -- signals ---------------------------------------------------------------
        self.connect_btn.clicked.connect(self.on_connect)
        self.browser_toggle.toggled.connect(self._on_browser_toggled)
        self.autoconnect_check.toggled.connect(
            lambda v: setattr(self.settings, "auto_connect", v))
        self.tree.doubleClicked.connect(self.on_file_activated)
        self.up_btn.clicked.connect(self.on_up_dir)
        self.home_btn.clicked.connect(lambda: self._set_browse_root(QDir.homePath()))
        self.path_edit.returnPressed.connect(
            lambda: self._set_browse_root(self.path_edit.text())
        )
        self.fit_combo.currentIndexChanged.connect(self.on_fit_mode_changed)
        self.resize_combo.currentIndexChanged.connect(self.on_resize_algorithm_changed)
        self.deband_check.toggled.connect(self.on_deband_changed)
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        if hasattr(self.player, "fullscreen_toggled"):
            self.player.fullscreen_toggled.connect(self.toggle_fullscreen)
        self.play_btn.clicked.connect(self.on_play_pause)
        self.stop_btn.clicked.connect(self.on_stop)
        self.fallback_btn.clicked.connect(self.on_fallback)
        self.seek_slider.sliderReleased.connect(self.on_seek)
        self.chapter_prev_btn.clicked.connect(lambda: self.on_chapter_step(-1))
        self.chapter_next_btn.clicked.connect(lambda: self.on_chapter_step(1))
        # activated (not currentIndexChanged): fires only on user choice, so
        # the position-driven combo updates below never trigger seeks.
        self.chapter_combo.activated.connect(self.on_chapter_selected)
        if hasattr(self.player, "chapter_step_requested"):
            self.player.chapter_step_requested.connect(self.on_chapter_step)
        self.sub_combo.currentIndexChanged.connect(self.on_sub_selected)
        self.sub_delay.valueChanged.connect(lambda v: self.player.set_sub_delay(v))
        self.player.stats_changed.connect(self.player_status.setText)
        self.player.position_changed.connect(self._on_position)
        self.player.track_list_changed.connect(self._on_tracks)
        self.player.rebuffering.connect(self._on_rebuffering)
        if hasattr(self.player, "seek_requested"):
            self.player.seek_requested.connect(self.on_seek_relative)
        if hasattr(self.player, "mouse_moved"):
            self.player.mouse_moved.connect(self._on_player_mouse_moved)
        self.player.finished.connect(lambda: self._end_session("end of stream"))
        self.player.failed.connect(self._on_player_failed)

        self._paused = False
        self._was_maximized = False
        self._chapters: list[Chapter] = []
        self._duration_s: float | None = None
        self._position_s = 0.0
        self._slider_down = False
        self._pending_seek_s: float | None = None
        self._pending_seek_t = 0.0
        self._session_source: str | None = None
        self._session_path: str | None = None
        self._session_time_base = None
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_slider_down", True))

        self._apply_browser_visible(self.settings.browser_visible)
        if self.settings.auto_connect:
            # Fire once the qasync loop starts (on_connect is a coroutine slot).
            QTimer.singleShot(0, self.on_connect)

    # -- helpers ------------------------------------------------------------------

    def _icon(self, theme_name: str, fallback: QStyle.StandardPixmap) -> QIcon:
        icon = QIcon.fromTheme(theme_name)
        return icon if not icon.isNull() else self.style().standardIcon(fallback)

    def _set_browse_root(self, path: str) -> None:
        p = Path(path).expanduser()
        if not p.is_dir():
            self.statusBar().showMessage(f"not a directory: {p}", 5000)
            return
        self.tree.setRootIndex(self.fs_model.index(str(p)))
        self.path_edit.setText(str(p))
        self.settings.browse_dir = str(p)

    def on_up_dir(self) -> None:
        current = self.fs_model.filePath(self.tree.rootIndex()) or QDir.rootPath()
        self._set_browse_root(str(Path(current).parent))

    def _on_browser_toggled(self, visible: bool) -> None:
        self.settings.browser_visible = visible
        self._apply_browser_visible(visible)

    def _apply_browser_visible(self, visible: bool) -> None:
        """Collapse/expand the file browser, remembering its width so it comes
        back to the same size. Fullscreen hides it too, without disturbing the
        toggle's remembered state."""
        if visible:
            self.browser_panel.setVisible(True)
            self.split.setSizes(self._browser_sizes)
        else:
            sizes = self.split.sizes()
            if sizes and sizes[0] > 0:  # don't overwrite with an already-collapsed width
                self._browser_sizes = sizes
            self.browser_panel.setVisible(False)

    def toggle_fullscreen(self) -> None:
        entering = not self.isFullScreen()
        # The transport bar becomes a pointer-revealed overlay in fullscreen
        # rather than just vanishing; everything else hides.
        for w in (self._toolbar, self.statusBar()):
            w.setVisible(not entering)
        if entering:
            self._apply_browser_visible(False)
            self._enter_overlay_controls()
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
            self.player.setFocus()  # keys (Space/F/arrows) go to the video
        else:
            self._apply_browser_visible(self.browser_toggle.isChecked())
            self._exit_overlay_controls()
            if self._was_maximized:
                self.showMaximized()
            else:
                self.showNormal()

    def keyPressEvent(self, event) -> None:
        # Unhandled keys from the player view propagate up to here.
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.toggle_fullscreen()
            return
        super().keyPressEvent(event)

    # -- fullscreen control overlay ---------------------------------------------

    def _enter_overlay_controls(self) -> None:
        """Float the transport bar over the bottom of the video, hidden until
        the pointer nears the bottom edge (see _on_player_mouse_moved)."""
        if self._controls_overlay:
            return
        self._controls_overlay = True
        self._controls_layout.removeWidget(self.controls_panel)
        self.controls_panel.setParent(self.player)
        self.controls_panel.setAttribute(Qt.WA_StyledBackground, True)
        self.controls_panel.setStyleSheet(
            "#overlayControls { background-color: rgba(18, 18, 18, 210); "
            "border-top: 1px solid rgba(255, 255, 255, 28); }"
        )
        self.controls_panel.layout().setContentsMargins(16, 8, 16, 12)
        self.controls_panel.hide()
        self._position_overlay()
        self.controls_panel.raise_()

    def _exit_overlay_controls(self) -> None:
        if not self._controls_overlay:
            return
        self._controls_overlay = False
        self._controls_timer.stop()
        self.controls_panel.setStyleSheet("")
        self.controls_panel.setAttribute(Qt.WA_StyledBackground, False)
        self.controls_panel.layout().setContentsMargins(0, 0, 0, 0)
        self._controls_layout.addWidget(self.controls_panel)  # re-dock below the video
        self.controls_panel.show()

    def _position_overlay(self) -> None:
        h = self.controls_panel.sizeHint().height()
        self.controls_panel.setGeometry(
            0, self.player.height() - h, self.player.width(), h
        )

    def _on_player_mouse_moved(self, x: int, y: int) -> None:
        if not (self._controls_overlay and self.isFullScreen()):
            return
        reveal_zone = self.controls_panel.sizeHint().height() + 48
        if y >= self.player.height() - reveal_zone:
            self._reveal_controls()

    def _reveal_controls(self) -> None:
        if not self.controls_panel.isVisible():
            self._position_overlay()
            self.controls_panel.show()
            self.controls_panel.raise_()
        self._controls_timer.start()

    def _auto_hide_controls(self) -> None:
        if not self._controls_overlay:
            return
        # Keep the bar up while the pointer rests on it or is dragging the seek
        # slider — the player sees no motion there to keep the timer alive.
        local = self.controls_panel.mapFromGlobal(QCursor.pos())
        if self._slider_down or self.controls_panel.rect().contains(local):
            self._controls_timer.start()
            return
        self.controls_panel.hide()

    def eventFilter(self, obj, event) -> bool:
        if (obj is self.player and event.type() == QEvent.Resize
                and self._controls_overlay):
            self._position_overlay()
        return super().eventFilter(obj, event)

    def _host_port(self) -> tuple[str, int]:
        text = self.host_edit.text().strip()
        host, _, port = text.partition(":")
        return host or "127.0.0.1", int(port or 8590)

    def _set_opening(self, active: bool, text: str = "") -> None:
        """Show/hide the indeterminate busy bar while a session opens."""
        self.open_progress.setVisible(active)
        if active and text:
            self.statusBar().showMessage(text)

    def _on_open_progress(self, msg: dict) -> None:
        """session_progress from the server (e.g. TensorRT engine build)."""
        message = msg.get("message") or "preparing session…"
        elapsed = msg.get("elapsed_s")
        if isinstance(elapsed, (int, float)):
            message = f"{message} ({elapsed:.0f} s)"
        self._set_opening(True, message)

    def _error(self, title: str, message: str) -> None:
        self.statusBar().showMessage(message, 10_000)
        # Non-modal on purpose: a modal dialog spins the Qt event loop inside
        # whatever coroutine raised the error, and qasync then re-enters
        # asyncio tasks reentrantly ("Cannot enter into task ...").
        box = QMessageBox(QMessageBox.Warning, title, message, QMessageBox.Ok, self)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.setModal(False)
        box.show()

    async def _adopt_connected_client(self, client: RelayClient, caps: dict) -> None:
        """Install a connected control client and reflect its capabilities."""
        self.client = client
        client.on_progress = self._on_open_progress
        self.settings.server_host, self.settings.server_port = client.host, client.port
        current = self.model_combo.currentText()
        self.model_combo.clear()
        self.model_combo.addItems([m["name"] for m in caps["models"]])
        if current:
            self.model_combo.setCurrentText(current)
        selected_tier = self.settings.quality_tier
        quality_options = caps.get("quality_options") or [
            {"id": tier, "label": tier} for tier in caps.get("quality_tiers", [])
        ]
        self.tier_combo.blockSignals(True)
        self.tier_combo.clear()
        for option in quality_options:
            self.tier_combo.addItem(option.get("label", option["id"]), option["id"])
        tier_index = self.tier_combo.findData(selected_tier)
        if tier_index < 0 and self.tier_combo.count():
            tier_index = 0
        self.tier_combo.setCurrentIndex(tier_index)
        self.tier_combo.blockSignals(False)
        selected_resize = self.settings.resize_algorithm or None
        self.resize_combo.blockSignals(True)
        self.resize_combo.clear()
        default_resize = caps.get("default_resize_algorithm", "lanczos")
        self.resize_combo.addItem(f"Server default ({default_resize})", None)
        for algorithm in caps.get("resize_algorithms", ["lanczos"]):
            self.resize_combo.addItem(algorithm, algorithm)
        resize_index = self.resize_combo.findData(selected_resize)
        self.resize_combo.setCurrentIndex(resize_index if resize_index >= 0 else 0)
        self.resize_combo.blockSignals(False)
        self.conn_label.setText(f"connected: {caps['server_name']}")
        self.connect_btn.setText("Disconnect")
        if caps.get("library"):
            self._ensure_server_tab()
            await self.on_refresh_server_library()
        else:
            self._remove_server_tab()

    def _ensure_server_tab(self) -> None:
        if self.server_browser_panel is not None:
            return
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        refresh = QToolButton()
        refresh.setIcon(self._icon("view-refresh", QStyle.SP_BrowserReload))
        refresh.setText("Refresh")
        refresh.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        refresh.setToolTip("Refresh the server library")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(refresh)
        placeholder = QLabel("Loading server library…")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setWordWrap(True)
        tree = QTreeView()
        model = QStandardItemModel(tree)
        tree.setModel(model)
        tree.setHeaderHidden(True)
        tree.setVisible(False)
        layout.addLayout(row)
        layout.addWidget(placeholder)
        layout.addWidget(tree, stretch=1)

        self.server_browser_panel = panel
        self.server_tree = tree
        self.server_model = model
        self.server_placeholder = placeholder
        self.server_refresh_btn = refresh
        refresh.clicked.connect(self.on_refresh_server_library)
        tree.doubleClicked.connect(self.on_server_file_activated)
        self.browser_panel.addTab(panel, "Server")
        self.browser_panel.tabBar().setVisible(True)

    def _remove_server_tab(self) -> None:
        panel = self.server_browser_panel
        if panel is not None:
            index = self.browser_panel.indexOf(panel)
            if index >= 0:
                self.browser_panel.removeTab(index)
            panel.deleteLater()
        self.server_browser_panel = None
        self.server_tree = None
        self.server_model = None
        self.server_placeholder = None
        self.server_refresh_btn = None
        self.browser_panel.tabBar().setVisible(self.browser_panel.count() > 1)

    def _append_server_node(self, parent: QStandardItem, node: dict) -> None:
        is_dir = node.get("type") == "directory"
        icon = self._icon(
            "folder" if is_dir else "video-x-generic",
            QStyle.SP_DirIcon if is_dir else QStyle.SP_FileIcon,
        )
        item = QStandardItem(icon, node.get("name", ""))
        item.setEditable(False)
        item.setData(node.get("path", ""), Qt.UserRole)
        item.setData(node.get("type"), Qt.UserRole + 1)
        parent.appendRow(item)
        if is_dir:
            for child in node.get("children", []):
                self._append_server_node(item, child)

    @asyncSlot()
    async def on_refresh_server_library(self) -> None:
        client = self.client
        if client is None or self.server_browser_panel is None:
            return
        assert self.server_placeholder is not None
        assert self.server_tree is not None
        assert self.server_model is not None
        self.server_placeholder.setText("Loading server library…")
        self.server_placeholder.setVisible(True)
        self.server_tree.setVisible(False)
        try:
            root = await client.fetch_library()
        except Exception as err:
            if client is self.client and self.server_placeholder is not None:
                self.server_placeholder.setText(f"Could not load server library:\n{err}")
            return
        if client is not self.client or self.server_model is None:
            return
        self.server_model.clear()
        invisible = self.server_model.invisibleRootItem()
        for node in root.get("children", []):
            self._append_server_node(invisible, node)
        if self.server_model.rowCount() == 0:
            self.server_placeholder.setText("Server library is empty.")
            return
        self.server_placeholder.setVisible(False)
        self.server_tree.setVisible(True)
        for row in range(self.server_model.rowCount()):
            self.server_tree.setExpanded(self.server_model.index(row, 0), True)

    # -- slots -----------------------------------------------------------------------

    @asyncSlot()
    async def on_connect(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
            self._remove_server_tab()
            self.conn_label.setText("disconnected")
            self.connect_btn.setText("Connect")
            return
        host, port = self._host_port()
        client = RelayClient(host, port)
        try:
            caps = await client.connect()
        except Exception as err:
            await client.close()
            self._error("Connection failed", f"Could not reach {host}:{port}\n{err}")
            return
        await self._adopt_connected_client(client, caps)

    @asyncSlot("QModelIndex")
    async def on_file_activated(self, index) -> None:
        path = self.fs_model.filePath(index)
        if Path(path).is_dir():
            return
        if self.client is None:
            self._error("Not connected", "Connect to an upscale server first.")
            return
        self.settings.browse_dir = str(Path(path).parent)
        await self._start_session(path, source="uplink")

    @asyncSlot("QModelIndex")
    async def on_server_file_activated(self, index) -> None:
        if index.data(Qt.UserRole + 1) != "file":
            return
        path = index.data(Qt.UserRole)
        if not path:
            return
        if self.client is None:
            self._error("Not connected", "Connect to an upscale server first.")
            return
        await self._start_session(path, source="server_file")

    async def _start_session(self, path: str, source: str = "uplink",
                             resume_s: float | None = None) -> None:
        await self._teardown_session()
        if self.client is None:  # teardown lost the connection and couldn't reconnect
            self._error("Not connected", "Lost the connection to the upscale server.")
            return
        # Physical pixels: QScreen.size() is logical (a 4K panel at 150%
        # Windows scaling reports 2560x1440 and we'd negotiate a too-small
        # output).
        screen = self.screen()
        dpr = screen.devicePixelRatio()
        cfg = SessionConfig(
            path=path,
            model=self.model_combo.currentText(),
            quality_tier=self._quality_tier(),
            display_w=round(screen.size().width() * dpr),
            display_h=round(screen.size().height() * dpr),
            fit_mode=self._fit_mode(),
            source=source,
            resize_algorithm=self._resize_algorithm(),
        )
        self.settings.model = cfg.model
        self.settings.quality_tier = cfg.quality_tier
        self._set_opening(True, f"opening session for {Path(path).name}…")
        try:
            session = await self.client.open_session(cfg)
            await self.client.attach_media()
            await self.client.start_uplink()
            await self.client.play()
        except Exception as err:
            self._error("Session failed", str(err))
            return
        finally:
            self._set_opening(False)
        track = self.client.track
        time_base = track.time_base if track is not None else session.time_base
        avg_rate = track.average_rate if track is not None else session.avg_rate
        duration_s = track.duration_seconds() if track is not None else session.duration_s
        if time_base is None:
            self._error("Session failed", "Server did not provide the source time base.")
            await self._teardown_session()
            return
        original_media = path if source == "uplink" else self.client.media_url(path)
        self._session_source = source
        self._session_path = path
        self._session_time_base = time_base
        self.player.client = self.client
        self.player.start(
            session,
            self.client.downlink_queue(),
            time_base,
            source_path=original_media,
            avg_rate=avg_rate,
        )
        self._apply_panscan()
        self._duration_s = duration_s
        # session_opened.chapters is authoritative (server file or echo); an
        # older server without the echo still yields chapters for local files.
        raw_chapters = session.chapters
        if not raw_chapters and track is not None:
            raw_chapters = track.chapters()
        self._set_chapters(normalize_chapters(raw_chapters))
        self._pending_seek_s = None
        self.seek_slider.setEnabled(self._duration_s is not None)
        self.play_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.fallback_btn.setVisible(source == "uplink")
        self.fallback_btn.setEnabled(source == "uplink")
        self.sub_combo.setEnabled(True)
        self.sub_delay.setEnabled(True)
        self.play_btn.setIcon(self._icon_pause)
        self.play_btn.setToolTip("Pause (Space)")
        self._paused = False
        self.statusBar().showMessage(
            f"{Path(path).name} -> {session.downlink_codec} "
            f"{session.downlink_width}x{session.downlink_height}"
        )
        if resume_s:  # restart (e.g. mode change): pick up where we left off
            await self._resume_at(resume_s)

    async def _resume_at(self, target_s: float) -> None:
        if self._duration_s:
            target_s = min(target_s, max(0.0, self._duration_s - 1.0))
        if target_s <= 0:
            return
        await self._seek_to_seconds(target_s, announce=False)

    async def _seek_to_seconds(self, target_s: float, announce: bool = True) -> None:
        """Shared relay-protocol seek: slider, arrow keys, chapters, resume."""
        if self.client is None or self.client.session is None:
            return
        target_s = max(0.0, target_s)
        if self._duration_s:
            target_s = min(target_s, max(0.0, self._duration_s - 1.0))
        tb = self._session_time_base
        if tb is None:
            return
        if announce:
            self.statusBar().showMessage(f"seeking to {target_s:.1f}s", 3000)
        self._arm_pending_seek(target_s)
        if hasattr(self.player, "prepare_seek"):
            self.player.prepare_seek(target_s)
        try:
            await self.client.seek(int(target_s / float(tb)))
        except Exception as err:
            self._pending_seek_s = None
            self._error("Seek failed", str(err))

    def _fit_mode(self) -> str:
        return self.fit_combo.currentData() or "fit"

    def _resize_algorithm(self) -> str | None:
        return self.resize_combo.currentData()

    def _quality_tier(self) -> str:
        return self.tier_combo.currentData() or self.tier_combo.currentText()

    def _apply_panscan(self) -> None:
        # Cover is already cropped server-side. Reassert zero so a user's
        # mpv.conf cannot apply a second client-side crop.
        self.player.set_panscan(0.0)

    @asyncSlot()
    async def on_fit_mode_changed(self) -> None:
        self.settings.fit_mode = self._fit_mode()
        self._apply_panscan()
        # The requested resolution changes with the mode (fit vs cover), and
        # that is fixed at open_session — so a live session must be re-opened.
        # Restart at the current position; nothing to do if idle.
        if (self.client is not None and self.client.session is not None
                and self._session_path is not None and self._session_source is not None):
            await self._start_session(
                self._session_path, source=self._session_source, resume_s=self._position_s
            )

    @asyncSlot()
    async def on_resize_algorithm_changed(self) -> None:
        self.settings.resize_algorithm = self._resize_algorithm() or ""
        if (self.client is not None and self.client.session is not None
                and self._session_path is not None and self._session_source is not None):
            await self._start_session(
                self._session_path, source=self._session_source, resume_s=self._position_s
            )

    def on_deband_changed(self, enabled: bool) -> None:
        self.settings.deband_enabled = enabled
        if hasattr(self.player, "set_deband"):
            self.player.set_deband(enabled)

    @asyncSlot()
    async def on_play_pause(self) -> None:
        if self.client is None:
            return
        self._paused = not self._paused
        self.player.set_paused(self._paused)
        if self._paused:
            await self.client.pause()
            self.play_btn.setIcon(self._icon_play)
            self.play_btn.setToolTip("Play (Space)")
        else:
            await self.client.play()
            self.play_btn.setIcon(self._icon_pause)
            self.play_btn.setToolTip("Pause (Space)")

    @asyncSlot()
    async def on_stop(self) -> None:
        await self._teardown_session()

    @asyncSlot()
    async def on_seek(self) -> None:
        self._slider_down = False
        if self._duration_s is None:
            return
        await self._seek_to_seconds(self.seek_slider.value() / 1000 * self._duration_s)

    @asyncSlot(float)
    async def on_seek_relative(self, delta_s: float) -> None:
        """Arrow-key seek: relay-protocol seek relative to current position."""
        await self._seek_to_seconds(self._position_s + delta_s)

    @asyncSlot(int)
    async def on_chapter_selected(self, index: int) -> None:
        target = self.chapter_combo.itemData(index)
        if target is not None:
            await self._seek_to_seconds(float(target))

    @asyncSlot(int)
    async def on_chapter_step(self, delta: int) -> None:
        """Prev/next chapter (buttons and PgUp/PgDn); no-op without chapters."""
        target = step_target(self._chapters, self._position_s, delta)
        if target is not None:
            await self._seek_to_seconds(target)

    @asyncSlot()
    async def on_fallback(self) -> None:
        """Drop the relay session and play the original file directly."""
        if self._session_source != "uplink":
            return
        pos = self._position_s
        path = self.client.track.path if self.client and self.client.track else None
        if path is None:
            return
        self.player._source_path = path  # ensure set even if session died early
        if self.client is not None and self.client.session is not None:
            host, port = self.client.host, self.client.port
            await self.client.teardown()
            self.client = None
            self._remove_server_tab()
            self.conn_label.setText("disconnected")
            self.connect_btn.setText("Connect")
        self.player.play_local_fallback(pos)
        self.statusBar().showMessage(f"playing locally from {pos:.1f}s (upscaler off)")

    def on_sub_selected(self, index: int) -> None:
        self.player.select_subtitle(self.sub_combo.itemData(index))

    def _set_chapters(self, chapters: list[Chapter]) -> None:
        self._chapters = chapters
        visible = bool(chapters)
        for widget in (self.chapter_prev_btn, self.chapter_combo, self.chapter_next_btn):
            widget.setVisible(visible)
        self.chapter_combo.blockSignals(True)
        self.chapter_combo.clear()
        for index, chapter in enumerate(chapters):
            self.chapter_combo.addItem(
                f"{index + 1:02d}  {chapter.title}  ({_format_time(chapter.start_s)})",
                chapter.start_s,
            )
        self.chapter_combo.blockSignals(False)
        self.seek_slider.set_chapter_marks(slider_fractions(chapters, self._duration_s))

    def _sync_chapter_combo(self, pos_s: float) -> None:
        if not self._chapters:
            return
        index = chapter_index(self._chapters, pos_s)
        combo_index = -1 if index is None else index
        if self.chapter_combo.currentIndex() != combo_index:
            self.chapter_combo.blockSignals(True)
            self.chapter_combo.setCurrentIndex(combo_index)
            self.chapter_combo.blockSignals(False)

    def _on_tracks(self, subs: list, selected_sid=None) -> None:
        # selected_sid: the track the player auto-selected (subs default on);
        # the fallback preview view still emits just the list.
        self.sub_combo.blockSignals(True)
        self.sub_combo.clear()
        self.sub_combo.addItem("no subs", None)
        for sid, title in subs:
            self.sub_combo.addItem(title, sid)
        idx = self.sub_combo.findData(selected_sid)
        self.sub_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.sub_combo.blockSignals(False)

    def _on_position(self, pos_s: float) -> None:
        if self._pending_seek_s is not None:
            # While the server rebuilds the pipeline after a seek, the
            # reloading stream reports 0/stale positions — hold the bar at
            # the seek target until playback lands near it (with a timeout
            # so a failed seek doesn't freeze the bar forever).
            near_target = abs(pos_s - self._pending_seek_s) <= 5.0
            if not near_target and time.monotonic() - self._pending_seek_t < 15.0:
                return
            self._pending_seek_s = None
        self._position_s = pos_s
        self._show_position(pos_s)

    def _show_position(self, pos_s: float) -> None:
        if self._duration_s and not self._slider_down:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(int(pos_s / self._duration_s * 1000))
            self.seek_slider.blockSignals(False)
        total = _format_time(self._duration_s) if self._duration_s else "--:--"
        self.pos_label.setText(f"{_format_time(pos_s)} / {total}")
        self._sync_chapter_combo(pos_s)

    def _arm_pending_seek(self, target_s: float) -> None:
        """Snap the UI to the seek target and ignore stale positions."""
        self._pending_seek_s = target_s
        self._pending_seek_t = time.monotonic()
        self._position_s = target_s
        self._show_position(target_s)

    def _on_rebuffering(self, buffering: bool) -> None:
        if buffering:
            self.statusBar().showMessage("buffering… (server behind real-time)")
        else:
            self.statusBar().clearMessage()

    def _on_player_failed(self, message: str) -> None:
        self._error("Playback failed", message)
        asyncio.ensure_future(self._teardown_session())

    def _end_session(self, reason: str) -> None:
        self.statusBar().showMessage(reason, 5000)
        asyncio.ensure_future(self._teardown_session())

    async def _teardown_session(self) -> None:
        self.player.stop()
        self._pending_seek_s = None
        self._set_chapters([])
        self.play_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.fallback_btn.setEnabled(False)
        self.fallback_btn.setVisible(True)
        self.seek_slider.setEnabled(False)
        self.sub_combo.setEnabled(False)
        self.sub_delay.setEnabled(False)
        self._session_source = None
        self._session_path = None
        self._session_time_base = None
        if self.client is not None and self.client.session is not None:
            # Close the whole client (server tears the session down with the
            # WS) and reconnect fresh: one session per connection in v1.
            host, port = self.client.host, self.client.port
            await self.client.teardown()
            client = RelayClient(host, port)
            try:
                caps = await client.connect()
                await self._adopt_connected_client(client, caps)
            except Exception:
                await client.close()
                self.client = None
                self._remove_server_tab()
                self.conn_label.setText("disconnected")
                self.connect_btn.setText("Connect")

    def closeEvent(self, event) -> None:
        self.player.stop()
        if self.client is not None:
            asyncio.ensure_future(self.client.close())
        event.accept()
