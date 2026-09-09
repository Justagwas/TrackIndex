from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from trackindex.core.config import APP_NAME, APP_VERSION, OFFICIAL_PAGE_URL
from trackindex.core.models import (
    AuditResult,
    LibraryRecord,
    OrderAuthority,
    StorageMode,
    TrackRecord,
)

from .interaction import (
    apply_media_crate_scrollbar,
    is_pointer_control,
    sync_pointer_cursor,
)
from .library_list import LibraryList
from .player_panel import PlayerPanel
from .settings_panel import SettingsPanel
from .setup_panel import LibrarySetupPanel
from .theme import ThemePalette, build_stylesheet
from .track_list import TrackTableView
from .windows_titlebar import apply_windows_titlebar_theme


class MainWindow(QMainWindow):
    openFolderRequested = Signal()
    createPlaylistRequested = Signal()
    librarySelected = Signal(str)
    libraryRescanRequested = Signal(str)
    libraryExplorerRequested = Signal(str)
    currentLibraryExplorerRequested = Signal()
    libraryRemoveRequested = Signal(str)
    libraryOrderChanged = Signal(object)
    authoritySelected = Signal(str)
    undoRequested = Signal()
    redoRequested = Signal()
    themeModeChanged = Signal(str)
    pinChanged = Signal(bool)
    uiScaleChanged = Signal(int)
    autoUpdatesChanged = Signal(bool)
    confirmTrackDeletionChanged = Signal(bool)
    checkUpdatesRequested = Signal()
    resetSettingsRequested = Signal()
    musicRootRequested = Signal()
    scanLibrariesRequested = Signal()
    setupSelectionChanged = Signal(str, str)
    setupPlaylistChanged = Signal(str)
    setupSubmitRequested = Signal()
    setupCancelRequested = Signal()
    orderingMethodRequested = Signal(str)
    prefixRemovalRequested = Signal()

    def __init__(self, theme: ThemePalette, *, theme_mode: str, ui_scale_percent: int,
                 auto_updates: bool, window_pinned: bool, music_root: str = "",
                 icon_path: Path | None = None,
                 confirm_track_deletion: bool = True) -> None:
        super().__init__()
        self.theme, self.theme_mode, self.ui_scale_percent = theme, theme_mode, ui_scale_percent
        self._settings_visible = False
        self._window_pinned = False
        self._base_settings_width = 325
        self._base_minimum_width = 1000
        self._current_library_id = ""
        self._current_storage_mode = StorageMode.BOTH
        self._has_filename_prefixes = False
        self._audit_blocked = False
        self.setWindowTitle(APP_NAME)
        self.resize(1000, 720)
        self.setMinimumSize(self._minimum_window_width(), 600)
        if icon_path and icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self._build(auto_updates, music_root, confirm_track_deletion)
        self._install_interaction_cursors()
        self.set_pinned(window_pinned)
        self.apply_appearance(theme, theme_mode, ui_scale_percent)

    def _build(
        self,
        auto_updates: bool,
        music_root: str,
        confirm_track_deletion: bool,
    ) -> None:
        root = QWidget(self)
        root.setObjectName("trackindexRoot")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(6, 8, 6, 6)
        outer.setSpacing(6)
        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(0)
        outer.addLayout(content_row, 1)

        sidebar = QFrame(root)
        sidebar.setObjectName("librarySidebar")
        self.sidebar = sidebar
        sidebar.setFixedWidth(250)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 12, 12, 12)
        side.setSpacing(8)
        self.open_folder_button = QPushButton("Open Folder", sidebar)
        self.open_folder_button.setObjectName("libraryActionButton")
        self.open_folder_button.clicked.connect(self.openFolderRequested.emit)
        self.create_playlist_button = QPushButton("Create Playlist", sidebar)
        self.create_playlist_button.setObjectName("libraryActionButton")
        self.create_playlist_button.clicked.connect(self.createPlaylistRequested.emit)
        side.addWidget(self.open_folder_button)
        side.addWidget(self.create_playlist_button)
        self.playlists_caption = QLabel("PLAYLISTS", sidebar)
        self.playlists_caption.setObjectName("playlistCaption")
        self.playlists_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(self.playlists_caption)
        self.library_list = LibraryList(self.theme, self.ui_scale_percent / 100, sidebar)
        self.library_list.libraryActivated.connect(self.librarySelected.emit)
        self.library_list.libraryMenuRequested.connect(self._show_library_menu)
        self.library_list.librariesReordered.connect(self.libraryOrderChanged.emit)
        side.addWidget(self.library_list, 1)
        content_row.addWidget(sidebar)

        main = QWidget(root)
        self.main_panel = main
        main.setObjectName("mainColumn")
        layout = QVBoxLayout(main)
        self.main_layout = layout
        layout.setContentsMargins(18, 15, 18, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        self.library_header_info = QWidget(main)
        title_box = QVBoxLayout(self.library_header_info)
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(1)
        self.library_title = QLabel("", self.library_header_info)
        self.library_title.setObjectName("title")
        self.library_subtitle = QLabel("", self.library_header_info)
        self.library_subtitle.setObjectName("muted")
        title_box.addWidget(self.library_title)
        title_box.addWidget(self.library_subtitle)
        self.overflow_button = QPushButton("", main)
        self.overflow_button.setObjectName("footerIcon")
        self.overflow_button.setFlat(True)
        self.overflow_button.setIconSize(QSize(20, 20))
        self.overflow_button.setToolTip("Playlist actions")
        self.overflow_button.setAccessibleName("Playlist actions")
        self.overflow_button.clicked.connect(self._show_overflow)
        header.addWidget(self.overflow_button, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.library_header_info, 1)


        self.save_status = QLabel("", main)
        self.save_status.setObjectName("muted")
        self.save_status.hide()
        layout.addLayout(header)

        self.authority_banner = QFrame(main)
        self.authority_banner.setObjectName("authorityBanner")
        authority = QHBoxLayout(self.authority_banner)
        authority.setContentsMargins(10, 7, 10, 7)
        self.audit_banner_message = QLabel("", self.authority_banner)
        self.audit_banner_message.setObjectName("warning")
        self.audit_banner_message.setWordWrap(True)
        authority.addWidget(self.audit_banner_message, 1)
        self.filename_order_button = QPushButton("Filename order", self.authority_banner)
        self.playlist_order_button = QPushButton("Playlist order", self.authority_banner)
        self.filename_order_button.clicked.connect(lambda: self.authoritySelected.emit(OrderAuthority.FILENAMES.value))
        self.playlist_order_button.clicked.connect(lambda: self.authoritySelected.emit(OrderAuthority.PLAYLIST.value))
        authority.addWidget(self.filename_order_button)
        authority.addWidget(self.playlist_order_button)
        self.authority_banner.hide()
        layout.addWidget(self.authority_banner)

        self.track_stack = QStackedWidget(main)
        self.track_stack.setObjectName("trackStack")
        self.library_state_page = QWidget(self.track_stack)
        self.library_state_page.setObjectName("libraryStatePage")
        state_layout = QVBoxLayout(self.library_state_page)
        state_layout.setContentsMargins(24, 24, 24, 24)
        state_layout.addStretch(1)
        self.empty_label = QLabel("No playlist selected", self.library_state_page)
        self.empty_label.setObjectName("title")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_detail = QLabel(
            "Select a playlist from the sidebar or open a folder.",
            self.library_state_page,
        )
        self.empty_detail.setObjectName("muted")
        self.empty_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_detail.setWordWrap(True)
        state_layout.addWidget(self.empty_label)
        state_layout.addWidget(self.empty_detail)
        state_layout.addStretch(1)
        list_page = QWidget(self.track_stack)
        list_page.setObjectName("trackListPage")
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        self.track_table = TrackTableView(self.theme, self.ui_scale_percent / 100, list_page)
        self.track_table.setEnabled(False)
        list_layout.addWidget(self.track_table)
        self.setup_panel = LibrarySetupPanel(self.track_stack)
        self.setup_panel.selectionChanged.connect(self.setupSelectionChanged.emit)
        self.setup_panel.playlistChanged.connect(self.setupPlaylistChanged.emit)
        self.setup_panel.submitRequested.connect(self.setupSubmitRequested.emit)
        self.setup_panel.cancelRequested.connect(self.setupCancelRequested.emit)
        self.track_stack.addWidget(self.library_state_page)
        self.track_stack.addWidget(list_page)
        self.track_stack.addWidget(self.setup_panel)
        self.track_stack.setCurrentWidget(self.library_state_page)
        self._track_list_page = list_page
        layout.addWidget(self.track_stack, 1)
        self.player_panel = PlayerPanel(self.theme, self.ui_scale_percent / 100, main)
        layout.addWidget(self.player_panel)

        self.toast = QFrame(main)
        self.toast.setObjectName("toast")
        toast_layout = QHBoxLayout(self.toast)
        toast_layout.setContentsMargins(12, 7, 8, 7)
        toast_layout.setSpacing(12)
        self.toast_text = QLabel("", self.toast)
        self.toast_undo = QPushButton("Undo", self.toast)
        self.toast_undo.setObjectName("toastAction")
        self.toast_undo.clicked.connect(self._toast_action_requested)
        toast_layout.addWidget(self.toast_text, 0, Qt.AlignmentFlag.AlignVCenter)
        toast_layout.addWidget(self.toast_undo, 0, Qt.AlignmentFlag.AlignVCenter)
        self.toast.hide()
        self._toast_action: str | None = None
        self._toast_hiding = False
        self._toast_animation = QPropertyAnimation(self.toast, b"pos", self)
        self._toast_animation.finished.connect(self._toast_animation_finished)
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._hide_toast)
        content_row.addWidget(main, 1)

        self.settings_container = QFrame(root)
        self.settings_container.setObjectName("settingsPanel")
        settings_layout = QVBoxLayout(self.settings_container)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        self.settings_scroll = QScrollArea(self.settings_container)
        self.settings_scroll.setObjectName("settingsScroll")
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.settings_scroll.setFixedWidth(self._settings_target_width())
        apply_media_crate_scrollbar(self.settings_scroll.verticalScrollBar())
        self.settings_panel = SettingsPanel(
            self.theme,
            self.ui_scale_percent,
            auto_updates,
            music_root,
            self.settings_scroll,
            confirm_track_deletion,
        )
        self.settings_scroll.setWidget(self.settings_panel)
        settings_layout.addWidget(self.settings_scroll)
        self.settings_container.setMaximumWidth(0)
        self.settings_container.setMinimumWidth(0)
        content_row.addWidget(self.settings_container)
        self.settings_animation = QPropertyAnimation(self.settings_container, b"maximumWidth", self)
        self.settings_animation.setDuration(160)
        self.settings_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.settings_animation.valueChanged.connect(self._sync_settings_drawer_width)
        self.settings_panel.uiScaleChanged.connect(self.uiScaleChanged.emit)
        self.settings_panel.autoUpdatesChanged.connect(self.autoUpdatesChanged.emit)
        self.settings_panel.confirmTrackDeletionChanged.connect(
            self.confirmTrackDeletionChanged.emit
        )
        self.settings_panel.checkUpdatesRequested.connect(self.checkUpdatesRequested.emit)
        self.settings_panel.resetSettingsRequested.connect(self.resetSettingsRequested.emit)
        self.settings_panel.musicRootRequested.connect(self.musicRootRequested.emit)
        self.settings_panel.scanLibrariesRequested.connect(self.scanLibrariesRequested.emit)
        self._build_footer(root, outer)
        self._set_library_context_visible(False)
        QShortcut(QKeySequence.StandardKey.Undo, self, self.undoRequested.emit)
        QShortcut(QKeySequence.StandardKey.Redo, self, self.redoRequested.emit)

    def _build_footer(self, root: QWidget, outer: QVBoxLayout) -> None:
        footer = QFrame(root)
        footer.setObjectName("appFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(2, 0, 2, 0)
        footer_layout.setSpacing(8)
        self.theme_button = QPushButton("", footer)
        self.theme_button.setObjectName("footerIcon")
        self.theme_button.setFlat(True)
        self.theme_button.setIconSize(QSize(20, 20))
        self.theme_button.clicked.connect(self._toggle_theme)
        self.pin_button = QPushButton("", footer)
        self.pin_button.setObjectName("footerIcon")
        self.pin_button.setFlat(True)
        self.pin_button.setCheckable(True)
        self.pin_button.setIconSize(QSize(20, 20))
        self.pin_button.toggled.connect(self.set_pinned)
        self.settings_toggle_button = QPushButton("Show settings", footer)
        self.settings_toggle_button.setObjectName("footerLink")
        self.settings_toggle_button.setFlat(True)
        self.settings_toggle_button.clicked.connect(self.toggle_settings)
        self.open_playlist_folder_button = QPushButton("Open playlist folder", footer)
        self.open_playlist_folder_button.setObjectName("footerLink")
        self.open_playlist_folder_button.setFlat(True)
        self.open_playlist_folder_button.setEnabled(False)
        self.open_playlist_folder_button.clicked.connect(self.currentLibraryExplorerRequested.emit)
        self.official_button = QPushButton("Official website", footer)
        self.official_button.setObjectName("footerLink")
        self.official_button.setFlat(True)
        self.official_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(OFFICIAL_PAGE_URL)))
        self.version_label = QLabel(f"v{APP_VERSION}", footer)
        self.version_label.setObjectName("footerVersion")
        footer_layout.addWidget(self.theme_button, 0, Qt.AlignmentFlag.AlignLeft)
        footer_layout.addWidget(self.pin_button, 0, Qt.AlignmentFlag.AlignLeft)
        footer_layout.addWidget(self.settings_toggle_button, 0, Qt.AlignmentFlag.AlignLeft)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.open_playlist_folder_button, 0, Qt.AlignmentFlag.AlignRight)
        footer_layout.addWidget(self.official_button, 0, Qt.AlignmentFlag.AlignRight)
        footer_layout.addWidget(self.version_label, 0, Qt.AlignmentFlag.AlignRight)
        outer.addWidget(footer, 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_responsive_setup_layout()
        if (
            hasattr(self, "toast")
            and self.toast.isVisible()
            and self._toast_animation.state() != QAbstractAnimation.State.Running
        ):
            self.toast.move(self._toast_target_position())

    def _show_library_menu(self, library_id: str, point) -> None:
        menu = QMenu(self)
        menu.addAction("Rescan", lambda: self.libraryRescanRequested.emit(library_id))
        menu.addAction("Open in Explorer", lambda: self.libraryExplorerRequested.emit(library_id))
        menu.addAction("Change ordering method", lambda: self.orderingMethodRequested.emit(library_id))
        menu.addSeparator()
        menu.addAction("Remove from TrackIndex", lambda: self.libraryRemoveRequested.emit(library_id))
        menu.exec(point)

    def _show_overflow(self) -> None:
        menu = QMenu(self)
        undo = QAction("Undo", menu)
        undo.setShortcut(QKeySequence.StandardKey.Undo)
        undo.triggered.connect(self.undoRequested.emit)
        redo = QAction("Redo", menu)
        redo.setShortcut(QKeySequence.StandardKey.Redo)
        redo.triggered.connect(self.redoRequested.emit)
        menu.addActions((undo, redo))
        if self._current_library_id:
            menu.addSeparator()
            selected_tracks = self.track_table._selected_track_ids()
            if selected_tracks and self.track_table.isEnabled():
                label = (
                    "Remove selected track"
                    if len(selected_tracks) == 1
                    else f"Remove {len(selected_tracks)} selected tracks"
                )
                menu.addAction(
                    label,
                    lambda: self.track_table.deleteTracksRequested.emit(
                        selected_tracks
                    ),
                )
            menu.addAction(
                "Change ordering method",
                lambda: self.orderingMethodRequested.emit(self._current_library_id),
            )
            if self._current_storage_mode is StorageMode.M3U8 and self._has_filename_prefixes:
                menu.addAction("Remove filename prefixes", self.prefixRemovalRequested.emit)
        menu.exec(self.overflow_button.mapToGlobal(self.overflow_button.rect().bottomLeft()))

    def set_libraries(self, libraries: list[LibraryRecord], selected_id: str = "") -> None:
        self.library_list.set_libraries(libraries, selected_id)

    def set_audit(self, audit: AuditResult, record: LibraryRecord | None = None) -> None:
        by_id = audit.track_map()
        tracks = [by_id[item] for item in audit.display_order]
        self.track_table.set_tracks(tracks)
        self._update_audit_view(audit, tracks, record)

    def reconcile_audit(self, audit: AuditResult, record: LibraryRecord | None = None) -> bool:


        by_id = audit.track_map()
        tracks = [by_id[item] for item in audit.display_order]
        preserved = self.track_table.reconcile_tracks(tracks)
        self._update_audit_view(audit, tracks, record)
        return preserved

    def _update_audit_view(
        self,
        audit: AuditResult,
        tracks: list[TrackRecord],
        record: LibraryRecord | None,
    ) -> None:
        if record is not None:
            self._current_library_id = record.library_id
            self._current_storage_mode = StorageMode(record.storage_mode)
        self._has_filename_prefixes = any(track.filename_index is not None for track in tracks)
        ignored_codes = {"duplicate-index", "index-gap"} if self._current_storage_mode is StorageMode.M3U8 else set()
        visible_issues = [
            issue for issue in audit.issues
            if issue.code not in ignored_codes
            and (issue.code != "authority-required" or audit.requires_authority_choice)
        ]
        blockers = [issue.message for issue in visible_issues if issue.severity.value == "blocker"]
        warnings = [issue.message for issue in visible_issues if issue.severity.value == "warning"]
        self._audit_blocked = bool(blockers) or audit.requires_authority_choice
        self.track_table.setEnabled(not self._audit_blocked)
        messages = tuple(dict.fromkeys((*blockers, *warnings)))
        self.audit_banner_message.setText("\n".join(messages))
        self.filename_order_button.setVisible(audit.requires_authority_choice)
        self.playlist_order_button.setVisible(audit.requires_authority_choice)
        self.authority_banner.setVisible(bool(messages) or audit.requires_authority_choice)
        self.library_title.setText(audit.folder.name or str(audit.folder))
        parts = [f"{len(tracks)} tracks", str(audit.folder)]
        self.library_subtitle.setText("  |  ".join(parts))
        self.library_subtitle.setToolTip(str(audit.folder))
        self.open_playlist_folder_button.setEnabled(audit.folder.is_dir())
        sync_pointer_cursor(self.open_playlist_folder_button)
        self._set_library_context_visible(True)
        if tracks:
            self.track_stack.setCurrentWidget(self._track_list_page)
        else:
            self._show_library_state(
                "No tracks found",
                "This playlist folder has no supported audio files.",
            )
        self._sync_responsive_setup_layout()

    def show_library_setup(
        self,
        audit: AuditResult,
        record: LibraryRecord,
        mode: StorageMode,
        authority: OrderAuthority | None,
        playlist_name: str,
        reconfiguring: bool,
        plan=None,
    ) -> None:
        self._current_library_id = record.library_id
        self._current_storage_mode = mode
        self._has_filename_prefixes = any(track.filename_index is not None for track in audit.tracks)
        self.setup_panel.set_context(audit, record, mode, authority, playlist_name, reconfiguring)
        self.setup_panel.set_plan(plan, audit.requires_authority_choice and authority is None)
        self.authority_banner.hide()
        self._set_library_context_visible(True)
        self.track_stack.setCurrentWidget(self.setup_panel)
        self._sync_responsive_setup_layout()

    def update_setup_plan(self, plan, needs_source: bool = False) -> None:
        self.setup_panel.set_plan(plan, needs_source)

    def close_library_setup(self) -> None:
        self.track_stack.setCurrentWidget(self._track_list_page)
        self._sync_responsive_setup_layout()

    def _set_library_context_visible(self, visible: bool) -> None:
        self.library_header_info.setVisible(visible)
        self.save_status.hide()
        self.overflow_button.setVisible(visible)

    def _show_library_state(self, title: str, detail: str = "") -> None:
        self.empty_label.setText(title)
        self.empty_detail.setText(detail)
        self.empty_detail.setVisible(bool(detail))
        self.track_stack.setCurrentWidget(self.library_state_page)

    def clear_library(self, message: str = "No playlist selected") -> None:
        self.track_table.set_tracks([])
        self.track_table.setEnabled(False)
        self.library_title.clear()
        self.library_subtitle.clear()
        self.library_subtitle.setToolTip("")
        self.save_status.clear()
        self.open_playlist_folder_button.setEnabled(False)
        self._current_library_id = ""
        self.authority_banner.hide()
        self._set_library_context_visible(False)
        detail = (
            "Select a playlist from the sidebar or open a folder."
            if message == "No playlist selected"
            else ""
        )
        self._show_library_state(message, detail)
        self._sync_responsive_setup_layout()
        sync_pointer_cursor(self.open_playlist_folder_button)

    def set_library_loading(self, folder: Path) -> None:
        self.track_table.set_tracks([])
        self.track_table.setEnabled(False)
        self.library_title.setText(folder.name or str(folder))
        self.library_subtitle.setText(str(folder))
        self.authority_banner.hide()
        self._set_library_context_visible(True)
        self._show_library_state(
            "Loading playlist...",
            "Scanning tracks and preparing the playlist.",
        )
        self._sync_responsive_setup_layout()
        self.open_playlist_folder_button.setEnabled(folder.is_dir())
        sync_pointer_cursor(self.open_playlist_folder_button)

    def set_library_error(self, folder: Path, message: str) -> None:
        self.track_table.set_tracks([])
        self.track_table.setEnabled(False)
        self.library_title.setText(folder.name or str(folder))
        self.library_subtitle.setText(str(folder))
        self.authority_banner.hide()
        self._set_library_context_visible(True)
        self._show_library_state(message)
        self._sync_responsive_setup_layout()
        self.open_playlist_folder_button.setEnabled(folder.is_dir())
        sync_pointer_cursor(self.open_playlist_folder_button)

    def set_library_validating(self, validating: bool) -> None:
        self.track_table.setEnabled(not validating and not self._audit_blocked)
        self.setup_panel.setEnabled(not validating)
        self.save_status.setText("Checking..." if validating else "")

    def set_library_validation_error(self, message: str) -> None:
        self._audit_blocked = True
        self.track_table.setEnabled(False)
        self.setup_panel.setEnabled(False)
        self.save_status.setText("Refresh failed")
        self.audit_banner_message.setText(message)
        self.filename_order_button.hide()
        self.playlist_order_button.hide()
        self.authority_banner.show()

    def set_saving(self, saving: bool, message: str = "") -> None:
        self.track_table.setEnabled(not saving and not self._audit_blocked)
        self.setup_panel.setEnabled(not saving)
        self.save_status.setText("Saving..." if saving else message)

    def show_toast(self, text: str, action: str | bool | None = "undo") -> None:


        if isinstance(action, bool):
            action = "undo" if action else None
        if action not in {None, "undo", "redo"}:
            raise ValueError(f"Unsupported toast action: {action!r}")
        self._toast_timer.stop()
        self._toast_animation.stop()
        self.toast_text.setText(text)
        self._toast_action = action
        has_action = action is not None
        self.toast_undo.setText(action.title() if action else "")
        self.toast_undo.setVisible(has_action)
        self.toast.adjustSize()
        target = self._toast_target_position()
        start = QPoint(self.main_panel.width() + 8, target.y())
        self._toast_hiding = False
        self.toast.move(start)
        self.toast.show()
        self.toast.raise_()
        self._toast_animation.setDuration(190)
        self._toast_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._toast_animation.setStartValue(start)
        self._toast_animation.setEndValue(target)
        self._toast_animation.start()
        self._toast_timer.start(4500)

    def _toast_target_position(self) -> QPoint:
        margin = max(8, round(10 * self.ui_scale_percent / 100))
        return QPoint(
            max(margin, self.main_panel.width() - self.toast.width() - margin),
            margin,
        )

    def _hide_toast(self) -> None:
        if not self.toast.isVisible():
            return
        self._toast_timer.stop()
        self._toast_animation.stop()
        self._toast_hiding = True
        self._toast_animation.setDuration(160)
        self._toast_animation.setEasingCurve(QEasingCurve.Type.InCubic)
        self._toast_animation.setStartValue(self.toast.pos())
        self._toast_animation.setEndValue(
            QPoint(self.main_panel.width() + 8, self.toast.y())
        )
        self._toast_animation.start()

    def _toast_animation_finished(self) -> None:
        if self._toast_hiding:
            self.toast.hide()
            self._toast_hiding = False

    def _toast_action_requested(self) -> None:
        action = self._toast_action
        self._hide_toast()
        if action == "undo":
            self.undoRequested.emit()
        elif action == "redo":
            self.redoRequested.emit()

    def toggle_settings(self) -> None:
        self._settings_visible = not self._settings_visible
        self.settings_toggle_button.setText("Hide settings" if self._settings_visible else "Show settings")
        self.settings_animation.stop()
        self.settings_animation.setStartValue(self.settings_container.width())
        self.settings_animation.setEndValue(self._settings_target_width() if self._settings_visible else 0)
        self.settings_animation.start()

    def _settings_target_width(self) -> int:
        return round(self._base_settings_width * self.ui_scale_percent / 100)

    def _minimum_window_width(self) -> int:
        scaled_drawer_extra = max(0, self._settings_target_width() - self._base_settings_width)
        return self._base_minimum_width + scaled_drawer_extra

    def _sync_settings_drawer_width(self, value) -> None:
        width = max(0, round(float(value)))


        self.settings_container.setMinimumWidth(width)
        self.settings_container.setMaximumWidth(width)
        self._sync_responsive_setup_layout()

    def _sync_responsive_setup_layout(self) -> None:

        if not hasattr(self, "setup_panel") or not hasattr(self, "sidebar"):
            return
        setup_visible = self.track_stack.currentWidget() is self.setup_panel
        drawer_width = self.settings_container.width()
        full_width_needed = 12 + 250 + 36 + 36 + 640 + drawer_width
        compact = setup_visible and drawer_width > 0 and self.width() < full_width_needed
        self.sidebar.setVisible(not compact)
        if compact:
            self.main_layout.setContentsMargins(5, 15, 5, 14)
        else:
            self.main_layout.setContentsMargins(18, 15, 18, 14)
        self.setup_panel.set_compact_viewport(compact)

    def _toggle_theme(self) -> None:
        self.themeModeChanged.emit("light" if self.theme_mode == "dark" else "dark")

    def set_pinned(self, pinned: bool) -> None:
        self._window_pinned = bool(pinned)
        self.pin_button.blockSignals(True)
        self.pin_button.setChecked(pinned)
        self.pin_button.blockSignals(False)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, pinned)
        if self.isVisible():
            self.show()
        self._refresh_pin_toggle_icon()
        self.pinChanged.emit(pinned)

    def apply_appearance(self, theme: ThemePalette, theme_mode: str, ui_scale_percent: int) -> None:
        self.theme, self.theme_mode, self.ui_scale_percent = theme, theme_mode, ui_scale_percent
        self.setMinimumSize(self._minimum_window_width(), 600)
        self.setStyleSheet(build_stylesheet(theme, ui_scale_percent / 100))
        icon_px = max(14, round(20 * ui_scale_percent / 100))
        self.theme_button.setIconSize(QSize(icon_px, icon_px))
        self.pin_button.setIconSize(QSize(icon_px, icon_px))
        self.overflow_button.setIconSize(QSize(icon_px, icon_px))
        self._refresh_theme_toggle_icon()
        self._refresh_pin_toggle_icon()
        self._refresh_playlist_actions_icon()
        self.track_table.set_appearance(theme, ui_scale_percent / 100)
        self.player_panel.set_appearance(theme, ui_scale_percent / 100)
        self.library_list.set_appearance(theme, ui_scale_percent / 100)
        self.setup_panel.set_scale(ui_scale_percent / 100)
        self.settings_panel.set_theme(theme)
        self.settings_panel.set_scale(ui_scale_percent / 100)
        self.settings_scroll.setFixedWidth(self._settings_target_width())
        apply_media_crate_scrollbar(self.track_table.verticalScrollBar())
        apply_media_crate_scrollbar(self.library_list.verticalScrollBar())
        apply_media_crate_scrollbar(self.settings_scroll.verticalScrollBar())
        if self._settings_visible and self.settings_animation.state() != QAbstractAnimation.State.Running:
            self._sync_settings_drawer_width(self._settings_target_width())
        self.refresh_cursor_state()
        apply_windows_titlebar_theme(self, theme_mode == "dark")

    def _build_theme_icon(self, mode: str) -> QIcon:
        size = max(14, int(self.theme_button.iconSize().width()))
        screen = QGuiApplication.primaryScreen()
        dpr = float(screen.devicePixelRatio()) if screen is not None else 1.0
        px = round(size * dpr)
        icon = QPixmap(px, px)
        icon.setDevicePixelRatio(dpr)
        icon.fill(Qt.GlobalColor.transparent)
        icon_color = QColor(self.theme.text_primary)
        painter = QPainter(icon)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(icon_color, max(1.1, size * 0.1), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        center = QPointF(size * 0.5, size * 0.5)
        if mode == "sun":
            orbit_radius = size * 0.22
            inner_ray = size * 0.34
            outer_ray = size * 0.46
            painter.drawEllipse(center, orbit_radius, orbit_radius)
            directions = (
                QPointF(1.0, 0.0), QPointF(-1.0, 0.0), QPointF(0.0, 1.0), QPointF(0.0, -1.0),
                QPointF(0.707, 0.707), QPointF(-0.707, -0.707), QPointF(0.707, -0.707), QPointF(-0.707, 0.707),
            )
            for direction in directions:
                start = QPointF(center.x() + direction.x() * inner_ray, center.y() + direction.y() * inner_ray)
                end = QPointF(center.x() + direction.x() * outer_ray, center.y() + direction.y() * outer_ray)
                painter.drawLine(start, end)
        else:
            moon_radius = size * 0.38
            painter.setBrush(icon_color)
            painter.drawEllipse(center, moon_radius, moon_radius)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(Qt.GlobalColor.transparent)
            painter.drawEllipse(QPointF(center.x() + size * 0.17, center.y() - size * 0.1), size * 0.34, size * 0.34)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.end()
        return QIcon(icon)

    def _build_pin_icon(self, pinned: bool) -> QIcon:
        size = max(14, int(self.pin_button.iconSize().width()))
        screen = QGuiApplication.primaryScreen()
        dpr = float(screen.devicePixelRatio()) if screen is not None else 1.0
        px = round(size * dpr)
        icon = QPixmap(px, px)
        icon.setDevicePixelRatio(dpr)
        icon.fill(Qt.GlobalColor.transparent)
        color = QColor(self.theme.accent if pinned else self.theme.text_primary)
        painter = QPainter(icon)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(color, max(1.1, size * 0.10), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        center = QPointF(size * 0.5, size * 0.56)
        if not pinned:
            painter.translate(center)
            painter.rotate(-28)
            painter.translate(-center)
        head_radius = size * 0.21
        head_center = QPointF(center.x(), center.y() - size * 0.30)
        painter.setBrush(color)
        painter.drawEllipse(head_center, head_radius, head_radius)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        stem_top = QPointF(center.x(), head_center.y() + head_radius * 0.95)
        stem_mid = QPointF(center.x(), center.y() + size * 0.12)
        painter.drawLine(stem_top, stem_mid)
        cross_y = head_center.y() + head_radius * 0.45
        cross_half = size * 0.14
        painter.drawLine(QPointF(center.x() - cross_half, cross_y), QPointF(center.x() + cross_half, cross_y))
        painter.drawLine(stem_mid, QPointF(center.x(), center.y() + size * 0.40))
        painter.end()
        return QIcon(icon)

    def _build_playlist_actions_icon(self) -> QIcon:


        size = max(14, int(self.overflow_button.iconSize().width()))
        screen = QGuiApplication.primaryScreen()
        dpr = float(screen.devicePixelRatio()) if screen is not None else 1.0
        px = round(size * dpr)
        icon = QPixmap(px, px)
        icon.setDevicePixelRatio(dpr)
        icon.fill(Qt.GlobalColor.transparent)
        rail_color = QColor(self.theme.text_secondary)
        control_color = QColor(self.theme.text_primary)
        painter = QPainter(icon)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        line_width = max(1.0, size * 0.075)
        left, right = size * 0.18, size * 0.82
        rows = (
            (size * 0.22, size * 0.66),
            (size * 0.50, size * 0.36),
            (size * 0.78, size * 0.58),
        )
        knob_radius = max(1.45, size * 0.095)
        rail_gap = knob_radius + line_width * 0.8
        for y, knob_x in rows:
            painter.setPen(
                QPen(
                    rail_color,
                    line_width,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.drawLine(QPointF(left, y), QPointF(knob_x - rail_gap, y))
            painter.drawLine(QPointF(knob_x + rail_gap, y), QPointF(right, y))
            painter.setPen(
                QPen(
                    control_color,
                    max(1.0, size * 0.08),
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(knob_x, y), knob_radius, knob_radius)
        painter.end()
        return QIcon(icon)

    def _refresh_theme_toggle_icon(self) -> None:
        if self.theme_mode == "dark":
            self.theme_button.setIcon(self._build_theme_icon("moon"))
            self.theme_button.setToolTip("Switch to light mode")
        else:
            self.theme_button.setIcon(self._build_theme_icon("sun"))
            self.theme_button.setToolTip("Switch to dark mode")

    def _refresh_pin_toggle_icon(self) -> None:
        self.pin_button.setIcon(self._build_pin_icon(self._window_pinned))
        self.pin_button.setToolTip("Disable always on top" if self._window_pinned else "Keep window on top")

    def _refresh_playlist_actions_icon(self) -> None:
        self.overflow_button.setIcon(self._build_playlist_actions_icon())

    def _install_interaction_cursors(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.refresh_cursor_state()

    def refresh_cursor_state(self) -> None:
        self.setCursor(Qt.CursorShape.ArrowCursor)
        root = self.centralWidget()
        if root is not None:
            root.setCursor(Qt.CursorShape.ArrowCursor)
        for widget in self.findChildren(QWidget):
            if is_pointer_control(widget):
                sync_pointer_cursor(widget)
        self.settings_panel.refresh_cursor_state()

    def eventFilter(self, watched, event):
        if (
            event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and isinstance(watched, QWidget)
            and watched.window() is self
        ):
            self._deselect_on_background_press(watched, event)
        if event.type() in {
            QEvent.Type.EnabledChange, QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Enter,
            QEvent.Type.HoverEnter, QEvent.Type.HoverMove, QEvent.Type.StyleChange, QEvent.Type.Polish,
        } and isinstance(watched, QWidget) and is_pointer_control(watched):
            sync_pointer_cursor(watched)
        return super().eventFilter(watched, event)

    def _deselect_on_background_press(self, watched: QWidget, event) -> None:
        table = self.track_table
        if table._drag is not None or self.library_list._drag is not None:
            return
        backgrounds = (
            self.centralWidget(), self.main_panel, self.sidebar,
            self.track_stack, self._track_list_page, self.library_state_page,
            self.player_panel, self.player_panel._left, self.player_panel._middle, self.player_panel._center,
            self.player_panel._mini_center, self.player_panel._right,
        )
        blank_tracks = (
            watched is table.viewport()
            and not table.indexAt(event.position().toPoint()).isValid()
        )
        if watched not in backgrounds and not blank_tracks:
            return
        table.selectionModel().clear()
        focus = QApplication.focusWidget()
        if focus is not None and focus.window() is self:
            focus.clearFocus()

    def restore_cursor_state_after_modal(self) -> None:
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        self.refresh_cursor_state()
        QTimer.singleShot(0, self.refresh_cursor_state)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        apply_windows_titlebar_theme(self, self.theme_mode == "dark")
