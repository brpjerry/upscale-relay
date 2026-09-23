"""Main window: file browser + server bar + player."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from pathlib import Path

from PySide6.QtCore import QDir, QEvent, QStandardPaths, Qt, QTimer
from PySide6.QtGui import (
    QCursor,
    QIcon,
    QPainter,
    QPalette,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QDockWidget,
    QFileSystemModel,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
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

from relay_client_core import RelayClient, SessionConfig, TeardownNotConfirmedError

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
_SERVER_TYPE_ROLE = Qt.UserRole + 1
_SERVER_LOADED_ROLE = Qt.UserRole + 2
_SERVER_CURSOR_ROLE = Qt.UserRole + 3
_SERVER_PAGE_SIZE = 100


def _format_time(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{int(seconds % 60):02d}"


def _server_address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _parse_server_address(address: str) -> tuple[str, int]:
    text = address.strip()
    if not text or any(char.isspace() for char in text) or any(c in text for c in "/?#"):
        raise ValueError("Enter a hostname or IP address, such as server.local:8590.")
    port_text = None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError("Close the IPv6 address with ], for example [::1]:8590.")
        host = text[1:end]
        try:
            ipaddress.IPv6Address(host)
        except ValueError as err:
            raise ValueError("Enter a valid IPv6 address inside the brackets.") from err
        suffix = text[end + 1:]
        if suffix and not suffix.startswith(":"):
            raise ValueError("Use [IPv6]:port, for example [::1]:8590.")
        port_text = suffix[1:] if suffix else None
    elif text.count(":") > 1:
        try:
            ipaddress.IPv6Address(text)
        except ValueError as err:
            raise ValueError("Use [IPv6]:port, for example [::1]:8590.") from err
        host = text  # an unbracketed IPv6 address uses the default port
    else:
        host, separator, port = text.partition(":")
        port_text = port if separator else None
        if not host or "[" in host or "]" in host:
            raise ValueError("Enter a server hostname or IP address before the port.")
    if port_text is not None and (not port_text.isascii() or not port_text.isdecimal()):
        raise ValueError("The control port must be a number from 1 to 65534.")
    port = int(port_text) if port_text is not None else 8590
    if not 1 <= port <= 65534:
        raise ValueError("The control port must be from 1 to 65534; media uses the next port.")
    return host, port


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
        self._server_caps: dict = {}

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
        self.host_edit = QLineEdit(_server_address(
            self.settings.server_host, self.settings.server_port,
        ))
        self.host_edit.setMinimumWidth(180)
        self.host_edit.setMaximumWidth(320)
        self.host_edit.setPlaceholderText("host:port")
        self.connect_btn = QPushButton("Connect")
        self.autoconnect_check = QCheckBox("Auto connect")
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
        self.deband_check = QCheckBox("Reduce color banding")
        self.deband_check.setToolTip(
            "Smooth visible color steps in gradients during playback."
        )
        self.deband_check.setChecked(self.settings.deband_enabled)
        self.conn_label = QLabel("disconnected")
        self.conn_label.setMinimumWidth(120)
        self.conn_label.setMaximumWidth(300)
        self.conn_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.playback_settings_toggle = QToolButton()
        self.playback_settings_toggle.setText("Playback settings")
        self.playback_settings_toggle.setCheckable(True)
        self.playback_settings_toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.playback_settings_toggle.setToolTip("Show playback settings")
        bar.addWidget(self.browser_toggle)
        bar.addWidget(QLabel(" Server "))
        bar.addWidget(self.host_edit)
        bar.addWidget(self.connect_btn)
        bar.addWidget(self.autoconnect_check)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)
        bar.addWidget(self.playback_settings_toggle)

        self.playback_settings = QDockWidget("Playback settings", self)
        self.playback_settings.setObjectName("playbackSettings")
        self.playback_settings.setFeatures(QDockWidget.DockWidgetClosable)
        settings_page = QWidget()
        settings_layout = QVBoxLayout(settings_page)
        explanation = QLabel(
            "Streaming settings apply at the current playback position. "
            "Changing them restarts an active stream."
        )
        explanation.setWordWrap(True)
        settings_layout.addWidget(explanation)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        for label, combo, help_text in (
            ("Upscale model", self.model_combo, "Choose a model available on the server."),
            ("Stream quality", self.tier_combo, "Higher quality can require more network bandwidth."),
            ("Framing", self.fit_combo, "Fit keeps the whole image; Crop fills the display."),
            ("Resize filter", self.resize_combo, "Choose how the upscaled image is fitted to the display."),
        ):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(24)
            combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            combo.setToolTip(help_text)
            form.addRow(label, combo)
        settings_layout.addLayout(form)
        settings_layout.addWidget(self.deband_check)
        settings_layout.addStretch(1)
        self.playback_settings.setWidget(settings_page)
        self.addDockWidget(Qt.RightDockWidgetArea, self.playback_settings)
        self.playback_settings.hide()
        self.playback_settings_toggle.toggled.connect(self.playback_settings.setVisible)
        self.playback_settings.visibilityChanged.connect(self.playback_settings_toggle.setChecked)
        self._settings_visible_before_fullscreen = False

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
        self.idle_hint = QLabel(self.player)
        self.idle_hint.setObjectName("idleGuidance")
        self.idle_hint.setTextFormat(Qt.PlainText)
        self.idle_hint.setAlignment(Qt.AlignCenter)
        self.idle_hint.setWordWrap(True)
        self.idle_hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.idle_hint.setStyleSheet("color: #dddddd; background: transparent;")
        hint_font = self.idle_hint.font()
        hint_font.setPointSizeF(max(12.0, hint_font.pointSizeF()))
        self.idle_hint.setFont(hint_font)
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
        self.chapter_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.chapter_combo.setMinimumContentsLength(12)
        self.chapter_combo.setMaximumWidth(240)
        self.chapter_combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
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
        # Live telemetry can be much wider than the player.  QLabel's default
        # minimum size hint includes the complete text, which used to make the
        # transport row resize the top-level window and squeeze the browser as
        # soon as the first stats sample arrived.  Give it remaining space,
        # but never let its contents establish the row's minimum width.
        self.player_status.setMinimumWidth(0)
        self.player_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

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
        self._bound_track_combo(self.sub_combo)
        self.sub_delay = QDoubleSpinBox()
        self.sub_delay.setRange(-30.0, 30.0)
        self.sub_delay.setSingleStep(0.1)
        self.sub_delay.setSuffix(" s")
        self.sub_delay.setEnabled(False)
        self.sub_delay.setToolTip(
            "Subtitle delay: positive values show subtitles later; negative values show them earlier."
        )
        self.sub_delay.setAccessibleName("Subtitle delay")

        self.audio_combo = QComboBox()
        self.audio_combo.addItem("no audio", None)
        self.audio_combo.setEnabled(False)
        self._bound_track_combo(self.audio_combo)
        self.audio_delay = QDoubleSpinBox()
        self.audio_delay.setRange(-30.0, 30.0)
        self.audio_delay.setSingleStep(0.1)
        self.audio_delay.setSuffix(" s")
        self.audio_delay.setEnabled(False)
        self.audio_delay.setToolTip(
            "Audio delay: positive values play audio later; negative values play it earlier."
        )
        self.audio_delay.setAccessibleName("Audio delay")

        volume, muted = (
            self.player.audio_output_state()
            if hasattr(self.player, "audio_output_state") else (100, False)
        )
        self.mute_btn = QToolButton()
        self.mute_btn.setCheckable(True)
        self._icon_volume = self._icon("audio-volume-high", QStyle.SP_MediaVolume)
        self._icon_muted = self._icon("audio-volume-muted", QStyle.SP_MediaVolumeMuted)
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, max(100, volume))
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.setAccessibleName("Volume")
        self._show_audio_output(volume, muted)

        transport = QHBoxLayout()
        transport.addWidget(self.play_btn)
        transport.addWidget(self.stop_btn)
        transport.addWidget(self.chapter_prev_btn)
        transport.addWidget(self.chapter_combo)
        transport.addWidget(self.chapter_next_btn)
        transport.addWidget(self.fullscreen_btn)
        transport.addWidget(self.fallback_btn)
        transport.addWidget(self.player_status, stretch=1)
        transport.addWidget(self.mute_btn)
        transport.addWidget(self.volume_slider)
        # Track selectors used to share the transport row with chapters and
        # telemetry.  Once a video populated all of them, that single row had
        # an ~900 px minimum and Qt enlarged the window (or stole width from
        # the browser) to satisfy it.  A dedicated row stays usable at the
        # player's 480 px minimum and makes metadata updates geometry-neutral.
        track_controls = QHBoxLayout()
        track_controls.addWidget(QLabel("audio"))
        track_controls.addWidget(self.audio_combo)
        track_controls.addWidget(self.audio_delay)
        track_controls.addSpacing(8)
        track_controls.addWidget(QLabel("subs"))
        track_controls.addWidget(self.sub_combo)
        track_controls.addWidget(self.sub_delay)
        track_controls.addStretch(1)
        # Controls live in one hideable panel so fullscreen is just the video.
        self.controls_panel = QWidget()
        cv = QVBoxLayout(self.controls_panel)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addLayout(seek_row)
        cv.addLayout(transport)
        cv.addLayout(track_controls)
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
        # Resizing a QOpenGLWidget on every handle mouse-move forces mpv and Qt
        # to rebuild/render its backing FBO continuously. Use Qt's rubber-band
        # preview and perform the expensive video resize once on release.
        self.split.setOpaqueResize(False)
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
        self.statusBar().addPermanentWidget(self.conn_label)
        self.statusBar().showMessage("Connect to a server to start streaming.")

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
        # activated fires only for a user choice, avoiding a restart while
        # capability refreshes repopulate these menus.
        self.model_combo.activated.connect(self.on_model_changed)
        self.tier_combo.activated.connect(self.on_quality_tier_changed)
        self.deband_check.toggled.connect(self.on_deband_changed)
        self.fullscreen_btn.clicked.connect(self.toggle_fullscreen)
        if hasattr(self.player, "fullscreen_toggled"):
            self.player.fullscreen_toggled.connect(self.toggle_fullscreen)
        self.play_btn.clicked.connect(self.on_play_pause)
        if hasattr(self.player, "pause_requested"):
            self.player.pause_requested.connect(self.on_play_pause)
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
        self.audio_combo.currentIndexChanged.connect(self.on_audio_selected)
        self.audio_delay.valueChanged.connect(lambda v: self.player.set_audio_delay(v))
        self.volume_slider.valueChanged.connect(self._on_volume_requested)
        self.mute_btn.toggled.connect(self._on_mute_requested)
        if hasattr(self.player, "volume_changed"):
            self.player.volume_changed.connect(self._show_audio_output)
        self.player.stats_changed.connect(self.player_status.setText)
        self.player.position_changed.connect(self._on_position)
        self.player.track_list_changed.connect(self._on_tracks)
        if hasattr(self.player, "audio_track_list_changed"):
            self.player.audio_track_list_changed.connect(self._on_audio_tracks)
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
        self._update_idle_guidance()
        if self.settings.auto_connect:
            # Fire once the qasync loop starts (on_connect is a coroutine slot).
            QTimer.singleShot(0, self.on_connect)

    # -- helpers ------------------------------------------------------------------

    def _icon(self, theme_name: str, fallback: QStyle.StandardPixmap) -> QIcon:
        source = QIcon.fromTheme(theme_name)
        if source.isNull():
            source = self.style().standardIcon(fallback)
        # Many icon themes ship a fixed black or white SVG variant selected by
        # the desktop theme name. That becomes unreadable when a bundled Qt
        # adopts a different palette (or the system switches at runtime).
        # Preserve the source alpha/shape and tint action icons from QPalette.
        result = QIcon()
        modes = (
            (QIcon.Normal, QPalette.Active),
            (QIcon.Active, QPalette.Active),
            (QIcon.Selected, QPalette.Active),
            (QIcon.Disabled, QPalette.Disabled),
        )
        for size in (16, 22, 32, 48):
            base = source.pixmap(size, size)
            if base.isNull():
                continue
            for mode, group in modes:
                tinted = base.copy()
                painter = QPainter(tinted)
                painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
                painter.fillRect(
                    tinted.rect(), self.palette().color(group, QPalette.ButtonText))
                painter.end()
                result.addPixmap(tinted, mode)
        return result if not result.isNull() else source

    def _refresh_action_icons(self) -> None:
        """Rebuild palette-tinted icons after a light/dark or style change."""
        definitions = (
            ("browser_toggle", "folder", QStyle.SP_DirIcon),
            ("up_btn", "go-up", QStyle.SP_FileDialogToParent),
            ("home_btn", "go-home", QStyle.SP_DirHomeIcon),
            ("stop_btn", "media-playback-stop", QStyle.SP_MediaStop),
            ("chapter_prev_btn", "media-skip-backward", QStyle.SP_MediaSkipBackward),
            ("chapter_next_btn", "media-skip-forward", QStyle.SP_MediaSkipForward),
            ("fullscreen_btn", "view-fullscreen", QStyle.SP_TitleBarMaxButton),
            ("server_refresh_btn", "view-refresh", QStyle.SP_BrowserReload),
        )
        for name, theme_name, fallback in definitions:
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setIcon(self._icon(theme_name, fallback))
        self._icon_play = self._icon("media-playback-start", QStyle.SP_MediaPlay)
        self._icon_pause = self._icon("media-playback-pause", QStyle.SP_MediaPause)
        if hasattr(self, "play_btn"):
            self.play_btn.setIcon(
                self._icon_play if getattr(self, "_paused", False) else self._icon_pause)
        if hasattr(self, "volume_slider"):
            self._icon_volume = self._icon("audio-volume-high", QStyle.SP_MediaVolume)
            self._icon_muted = self._icon("audio-volume-muted", QStyle.SP_MediaVolumeMuted)
            self._show_audio_output(self.volume_slider.value(), self.mute_btn.isChecked())

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (
            QEvent.PaletteChange,
            QEvent.ApplicationPaletteChange,
            QEvent.StyleChange,
        ):
            self._refresh_action_icons()
            if getattr(self, "_controls_overlay", False):
                self._apply_overlay_palette()

    @staticmethod
    def _bound_track_combo(combo: QComboBox) -> None:
        """Keep metadata labels from changing the transport's minimum width."""
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(10)
        combo.setMaximumWidth(220)
        combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

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
            self._settings_visible_before_fullscreen = self.playback_settings.isVisible()
            self.playback_settings.hide()
            self._apply_browser_visible(False)
            self._enter_overlay_controls()
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
            self.player.setFocus()  # keys (Space/F/arrows) go to the video
        else:
            self.playback_settings.setVisible(self._settings_visible_before_fullscreen)
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
        self._apply_overlay_palette()
        self.controls_panel.setStyleSheet(
            "#overlayControls { background-color: palette(window); "
            "border-top: 1px solid palette(mid); }"
        )
        self.controls_panel.layout().setContentsMargins(16, 8, 16, 12)
        self.controls_panel.hide()
        self._position_overlay()
        self.controls_panel.raise_()

    def _apply_overlay_palette(self) -> None:
        # Keep foreground, disabled text, control surfaces and focus colors in
        # the same palette. A fixed dark background made a light theme's black
        # labels disappear in fullscreen.
        palette = QPalette(self.palette())
        backdrop = palette.color(QPalette.Window)
        backdrop.setAlpha(240)
        palette.setColor(QPalette.Window, backdrop)
        self.controls_panel.setPalette(palette)

    def _exit_overlay_controls(self) -> None:
        if not self._controls_overlay:
            return
        self._controls_overlay = False
        self._controls_timer.stop()
        self.controls_panel.setStyleSheet("")
        self.controls_panel.setPalette(QPalette())
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
        if obj is self.player and event.type() == QEvent.Resize:
            self._position_idle_guidance()
            if self._controls_overlay:
                self._position_overlay()
        return super().eventFilter(obj, event)

    def _position_idle_guidance(self) -> None:
        height = min(180, max(0, self.player.height() - 48))
        self.idle_hint.setGeometry(
            24, (self.player.height() - height) // 2,
            max(0, self.player.width() - 48), height,
        )

    def _update_idle_guidance(self) -> None:
        if self._session_source is not None:
            self.idle_hint.hide()
            return
        if self.client is None:
            text = "Connect to your server\n\nEnter its address above, then choose a video from the file browser."
        else:
            text = "Choose a video\n\nDouble-click a file in Local or Server to start streaming."
        self.idle_hint.setText(text)
        self._position_idle_guidance()
        self.idle_hint.show()

    def _show_audio_output(self, volume: int, muted: bool) -> None:
        self.volume_slider.blockSignals(True)
        self.volume_slider.setMaximum(max(self.volume_slider.maximum(), volume))
        self.volume_slider.setValue(volume)
        self.volume_slider.blockSignals(False)
        self.volume_slider.setToolTip(f"Volume: {volume}%")
        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(muted)
        self.mute_btn.blockSignals(False)
        self.mute_btn.setIcon(self._icon_muted if muted else self._icon_volume)
        self.mute_btn.setToolTip("Unmute (M)" if muted else "Mute (M)")
        self.mute_btn.setAccessibleName("Unmute" if muted else "Mute")

    def _on_volume_requested(self, volume: int) -> None:
        self.player.set_volume(volume)
        self._show_audio_output(volume, self.mute_btn.isChecked())

    def _on_mute_requested(self, muted: bool) -> None:
        self.player.set_muted(muted)
        self._show_audio_output(self.volume_slider.value(), muted)

    def _host_port(self) -> tuple[str, int]:
        return _parse_server_address(self.host_edit.text())

    def _set_opening(self, active: bool, text: str = "") -> None:
        """Show/hide the indeterminate busy bar while a session opens."""
        self.open_progress.setVisible(active)
        if active and text:
            self.statusBar().showMessage(text)
            self.idle_hint.setText(f"Preparing playback…\n\n{text}")
            self.idle_hint.show()

    def _on_open_progress(self, msg: dict) -> None:
        """session_progress from the server (e.g. TensorRT engine build)."""
        message = msg.get("message") or "preparing session…"
        elapsed = msg.get("elapsed_s")
        if isinstance(elapsed, (int, float)):
            message = f"{message} ({elapsed:.0f} s)"
        self._set_opening(True, message)

    def _on_seek_progress(self, msg: dict) -> None:
        """seek_progress from the server: the new epoch has produced nothing
        yet. Keep the status bar honest instead of letting the "seeking to X"
        message time out into silence while the server decodes."""
        elapsed = msg.get("elapsed_s")
        discarded = msg.get("frames_discarded")
        text = "seeking…"
        if isinstance(discarded, int) and discarded:
            text = f"seeking… (server decoded past {discarded} frames)"
        if isinstance(elapsed, (int, float)):
            text = f"{text} {elapsed:.1f} s"
        self.statusBar().showMessage(text, 3000)

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
        self._server_caps = dict(caps)
        client.on_progress = self._on_open_progress
        client.on_seek_progress = self._on_seek_progress
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
        self._update_idle_guidance()
        if self._session_source is None:
            self.statusBar().showMessage("Choose a video from the file browser.")
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
        tree.expanded.connect(self.on_server_directory_expanded)
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
        item.setData(node.get("type"), _SERVER_TYPE_ROLE)
        parent.appendRow(item)
        if is_dir:
            item.setData(False, _SERVER_LOADED_ROLE)
            placeholder = QStandardItem("Loading…")
            placeholder.setEditable(False)
            placeholder.setData("placeholder", _SERVER_TYPE_ROLE)
            item.appendRow(placeholder)

    def _append_server_more(
        self, parent: QStandardItem, path: str, cursor: str,
    ) -> None:
        item = QStandardItem("Load more…")
        item.setEditable(False)
        item.setData(path, Qt.UserRole)
        item.setData("more", _SERVER_TYPE_ROLE)
        item.setData(cursor, _SERVER_CURSOR_ROLE)
        parent.appendRow(item)

    async def _load_server_page(
        self, parent: QStandardItem, path: str, *, cursor: str | None = None,
        reset: bool = False,
    ) -> None:
        client = self.client
        if client is None or self.server_model is None:
            return
        if reset:
            parent.removeRows(0, parent.rowCount())
        page = await client.fetch_library_page(
            path, cursor=cursor, limit=_SERVER_PAGE_SIZE,
        )
        if client is not self.client or self.server_model is None:
            return
        root = page["tree"]
        for node in root.get("children", []):
            self._append_server_node(parent, node)
        if page["next_cursor"] is not None:
            self._append_server_more(parent, path, page["next_cursor"])
        parent.setData(True, _SERVER_LOADED_ROLE)

    @asyncSlot("QModelIndex")
    async def on_server_directory_expanded(self, index) -> None:
        if index.data(_SERVER_TYPE_ROLE) != "directory":
            return
        item = self.server_model.itemFromIndex(index) if self.server_model is not None else None
        if item is None or item.data(_SERVER_LOADED_ROLE):
            return
        item.setData(True, _SERVER_LOADED_ROLE)
        try:
            await self._load_server_page(item, item.data(Qt.UserRole), reset=True)
        except Exception as err:
            item.removeRows(0, item.rowCount())
            error = QStandardItem(f"Could not load folder: {err}")
            error.setEditable(False)
            error.setData("error", _SERVER_TYPE_ROLE)
            item.appendRow(error)

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
            page = await client.fetch_library_page(limit=_SERVER_PAGE_SIZE)
        except Exception as err:
            if client is self.client and self.server_placeholder is not None:
                self.server_placeholder.setText(f"Could not load server library:\n{err}")
            return
        if client is not self.client or self.server_model is None:
            return
        self.server_model.clear()
        invisible = self.server_model.invisibleRootItem()
        root = page["tree"]
        for node in root.get("children", []):
            self._append_server_node(invisible, node)
        if page["next_cursor"] is not None:
            self._append_server_more(invisible, "", page["next_cursor"])
        if self.server_model.rowCount() == 0:
            self.server_placeholder.setText("Server library is empty.")
            return
        self.server_placeholder.setVisible(False)
        self.server_tree.setVisible(True)

    # -- slots -----------------------------------------------------------------------

    @asyncSlot()
    async def on_connect(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
            self._remove_server_tab()
            self.conn_label.setText("disconnected")
            self.connect_btn.setText("Connect")
            self._update_idle_guidance()
            return
        try:
            host, port = self._host_port()
        except ValueError as err:
            self.host_edit.setFocus()
            self.host_edit.selectAll()
            self._error("Invalid server address", str(err))
            return
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
        node_type = index.data(_SERVER_TYPE_ROLE)
        if node_type == "more":
            if self.server_model is None:
                return
            item = self.server_model.itemFromIndex(index)
            parent = item.parent() or self.server_model.invisibleRootItem()
            path = item.data(Qt.UserRole)
            cursor = item.data(_SERVER_CURSOR_ROLE)
            parent.removeRow(item.row())
            try:
                await self._load_server_page(parent, path, cursor=cursor)
            except Exception as err:
                error = QStandardItem(f"Could not load more: {err}")
                error.setEditable(False)
                error.setData("error", _SERVER_TYPE_ROLE)
                parent.appendRow(error)
            return
        if node_type != "file":
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
            aux_tracks=(
                "muxed"
                if source == "server_file" and self._server_caps.get("muxed_aux_tracks")
                else "external"
            ),
            aux_attachments=(
                "cached"
                if (
                    source == "server_file"
                    and self._server_caps.get("muxed_aux_tracks")
                    and self._server_caps.get("attachment_cache", 0) >= 1
                )
                else "embedded"
            ),
        )
        self.settings.model = cfg.model
        self.settings.quality_tier = cfg.quality_tier
        self._set_opening(True, f"opening session for {Path(path).name}…")
        try:
            session = await self.client.open_session(cfg)
            attachment_root = (
                Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation))
                / "attachments"
            )
            font_dir = (
                await self.client.prepare_attachments(attachment_root)
                if hasattr(self.client, "prepare_attachments") else None
            )
            if hasattr(self.player, "set_subtitle_fonts_dir"):
                self.player.set_subtitle_fonts_dir(font_dir)
            await self.client.attach_media()
            await self.client.start_uplink()
        except Exception as err:
            self._error("Session failed", str(err))
            # open_session may already have allocated a GPU pipeline before
            # fonts, media sockets, or the source pump fail. Retire that owner
            # through the same confirmed teardown barrier as an ordinary stop.
            await self._teardown_session()
            return
        except asyncio.CancelledError:
            await self._teardown_session()
            raise
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
        original_media = (
            None
            if getattr(session, "aux_tracks", "external") == "muxed"
            else (path if source == "uplink" else self.client.media_url(path))
        )
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
        self.idle_hint.hide()
        # Match Android's ordering: give mpv its per-load loopback first, then
        # release the server pipeline. Previously the server could produce into
        # the bridge while the player had not even begun opening its socket.
        await asyncio.sleep(0)
        try:
            await self.client.play()
        except Exception as err:
            self._error("Session failed", str(err))
            await self._teardown_session()
            return
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
        self.audio_combo.setEnabled(True)
        self.audio_delay.setEnabled(True)
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
        target_s = max(0.0, target_s)
        if self._duration_s:
            target_s = min(target_s, max(0.0, self._duration_s - 1.0))
        if self._session_source == "local":
            try:
                self.player.seek_local(target_s)
                self._arm_pending_seek(target_s)
            except Exception as err:
                self._error("Seek failed", str(err))
            return
        if self.client is None or self.client.session is None:
            return
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

    async def _restart_for_playback_setting(self) -> None:
        """Apply a session-fixed setting without losing the playback place."""
        if (self.client is not None and self.client.session is not None
                and self._session_path is not None and self._session_source is not None):
            await self._start_session(
                self._session_path, source=self._session_source, resume_s=self._position_s
            )

    @asyncSlot(int)
    async def on_model_changed(self, _index: int) -> None:
        self.settings.model = self.model_combo.currentText()
        await self._restart_for_playback_setting()

    @asyncSlot(int)
    async def on_quality_tier_changed(self, _index: int) -> None:
        self.settings.quality_tier = self._quality_tier()
        await self._restart_for_playback_setting()

    @asyncSlot()
    async def on_fit_mode_changed(self) -> None:
        self.settings.fit_mode = self._fit_mode()
        self._apply_panscan()
        # The requested resolution changes with the mode (fit vs cover), and
        # that is fixed at open_session — so a live session must be re-opened.
        # Restart at the current position; nothing to do if idle.
        await self._restart_for_playback_setting()

    @asyncSlot()
    async def on_resize_algorithm_changed(self) -> None:
        self.settings.resize_algorithm = self._resize_algorithm() or ""
        await self._restart_for_playback_setting()

    def on_deband_changed(self, enabled: bool) -> None:
        self.settings.deband_enabled = enabled
        if hasattr(self.player, "set_deband"):
            self.player.set_deband(enabled)

    @asyncSlot()
    async def on_play_pause(self) -> None:
        local = self._session_source == "local"
        if self.client is None and not local:
            return
        self._paused = not self._paused
        self.player.set_paused(self._paused)
        if self._paused:
            if not local:
                await self.client.pause()
            self.play_btn.setIcon(self._icon_play)
            self.play_btn.setToolTip("Play (Space)")
        else:
            if not local:
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
        path = self._session_path
        if path is None:
            return
        # The control teardown closes the old downlink. Stop its consumer first
        # so that normal closure cannot enqueue a stale playback failure while
        # the local file is being loaded.
        self.player.stop()
        if self.client is not None:
            try:
                await self.client.teardown()
            except TeardownNotConfirmedError as err:
                # Local playback allocates no replacement GPU session. It can
                # proceed while the failed server cleanup remains visible.
                self._error("Server cleanup not confirmed", str(err))
            self.client = None
            self._remove_server_tab()
            self.conn_label.setText("disconnected")
            self.connect_btn.setText("Connect")
        self._session_source = "local"
        self._session_time_base = None
        self._pending_seek_s = None
        self.fallback_btn.setEnabled(False)
        self.player.client = None
        try:
            await self.player.play_local(path, pos, paused=self._paused)
        except Exception as err:
            self._error("Local playback failed", str(err))
            await self._teardown_session()
            return
        if self._session_source != "local":
            return
        self.statusBar().showMessage(f"playing locally from {pos:.1f}s (upscaler off)")

    def on_sub_selected(self, index: int) -> None:
        self.player.select_subtitle(self.sub_combo.itemData(index))

    def on_audio_selected(self, index: int) -> None:
        aid = self.audio_combo.itemData(index)
        if aid is not None:
            self.player.select_audio(aid)

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

    def _on_audio_tracks(self, audio: list, selected_aid=None) -> None:
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        if not audio:
            self.audio_combo.addItem("no audio", None)
        else:
            for aid, title in audio:
                self.audio_combo.addItem(title, aid)
        idx = self.audio_combo.findData(selected_aid)
        self.audio_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.audio_combo.blockSignals(False)

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
            self.statusBar().showMessage(
                "Buffering local playback…" if self._session_source == "local"
                else "Buffering… waiting for the stream."
            )
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
        self.audio_combo.setEnabled(False)
        self.audio_delay.setEnabled(False)
        self._session_source = None
        self._session_path = None
        self._session_time_base = None
        self._duration_s = None
        self._position_s = 0.0
        self.pos_label.setText("--:-- / --:--")
        self.player_status.clear()
        self._update_idle_guidance()
        if self.client is not None and (
            self.client.session is not None
            or getattr(self.client, "has_server_session", False)
        ):
            # Close the whole client (server tears the session down with the
            # WS) and reconnect fresh: one session per connection in v1.
            host, port = self.client.host, self.client.port
            try:
                await self.client.teardown()
            except TeardownNotConfirmedError as err:
                # The local sockets are already closed, but reconnecting could
                # overlap a wedged NVENC owner. Make the risk visible and stop.
                self.client = None
                self._remove_server_tab()
                self.conn_label.setText("server teardown unconfirmed")
                self.connect_btn.setText("Connect")
                self._error("Server cleanup not confirmed", str(err))
                return
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
