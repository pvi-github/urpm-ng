"""Main window for rpmdrake-ng Qt frontend."""

import sys
from typing import Optional

from .compat import (
    Qt,
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QPushButton,
    QLabel,
    QKeySequence,
    QShortcut,
    QFont,
    QToolButton,
    QMenu,
    QFrame,
)

from urpm.core.database import PackageDatabase

from ..common.controller import Controller, ControllerConfig
from .view import QtView
from .widgets.search_bar import SearchBar
from .widgets.package_list import PackageList
from .widgets.filter_panel import FilterPanel
from .widgets.download_progress import CollapsibleProgressWidget, SlotInfo

__all__ = ["MainWindow", "main"]


class MainWindow(QMainWindow):
    """Main window for rpmdrake-ng.

    Layout:
    ┌─────────────────────────────────────────────────────────────────┐
    │ ┌───────────────────────┐  ┌──────────┬──────────┬─────┬───┐   │
    │ │ 🔍 Rechercher...      │  │ Installer│ Supprimer│ Màj │ ⬆ │   │
    │ └───────────────────────┘  └──────────┴──────────┴─────┴───┘   │
    │  ┌──────────────────────────────────────────┐│┌──────────────┐  │
    │  │                                          │││              │  │
    │  │           ZONE PRINCIPALE                │▐│   FILTRES    │  │
    │  │           (liste paquets)                │▐│              │  │
    │  │                                          │││              │  │
    │  └──────────────────────────────────────────┘│└──────────────┘  │
    │ ┌─────────────────────────────────────────────────────────────┐ │
    │ │ Status bar                                                  │ │
    │ └─────────────────────────────────────────────────────────────┘ │
    └─────────────────────────────────────────────────────────────────┘
    """

    MIN_FONT_SIZE = 8
    MAX_FONT_SIZE = 24
    DEFAULT_FONT_SIZE = 10

    def __init__(self, db: Optional[PackageDatabase] = None):
        super().__init__()

        self.setWindowTitle("rpmdrake-ng")
        self.resize(1024, 768)

        # Font size for zoom
        self._font_size = self.DEFAULT_FONT_SIZE
        self._apply_font_size()

        # Initialize database
        self.db = db or PackageDatabase()

        # Create view and controller
        self.qt_view = QtView(self)
        self.controller = Controller(self.db, self.qt_view)

        # Build UI
        self._create_widgets()
        self._create_layout()
        self._create_shortcuts()
        self._connect_signals()

        # Connect cancel button to controller
        self.progress_widget.cancel_requested.connect(self.controller.cancel_transaction)

        # Apply initial row height for package list
        self.package_list.update_row_height(self._font_size)

        # Load initial data
        self.controller.load_initial()

        # Populate categories after groups are loaded
        self.filter_panel.populate_categories()

    def _create_widgets(self) -> None:
        """Create UI widgets."""
        # Top bar
        self.search_bar = SearchBar()

        # Action buttons with modern styling
        self.btn_install = QPushButton("📥 Installer")
        self.btn_install.setStyleSheet("""
            QPushButton {
                background-color: #4caf50;
                color: white;
                font-weight: bold;
                padding: 6px 16px;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #45a049; }
            QPushButton:pressed { background-color: #3d8b40; }
            QPushButton:disabled { background-color: #a5d6a7; }
        """)

        self.btn_remove = QPushButton("🗑 Supprimer")
        self.btn_remove.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                font-weight: bold;
                padding: 6px 16px;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #da190b; }
            QPushButton:pressed { background-color: #c1170a; }
            QPushButton:disabled { background-color: #ef9a9a; }
        """)

        # Upgrade button with dropdown menu
        self.btn_upgrade = QToolButton()
        self.btn_upgrade.setText("⬆ Màj")
        self.btn_upgrade.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.btn_upgrade.setStyleSheet("""
            QToolButton {
                background-color: #2196f3;
                color: white;
                font-weight: bold;
                padding: 6px 12px;
                border: none;
                border-radius: 4px;
            }
            QToolButton:hover { background-color: #1976d2; }
            QToolButton:pressed { background-color: #1565c0; }
            QToolButton:disabled { background-color: #90caf9; }
            QToolButton::menu-button {
                background-color: #1976d2;
                border-left: 1px solid #1565c0;
                border-top-right-radius: 4px;
                border-bottom-right-radius: 4px;
                width: 20px;
            }
            QToolButton::menu-button:hover { background-color: #1565c0; }
            QToolButton::menu-arrow { image: none; }
        """)

        # Menu for upgrade all
        self.upgrade_menu = QMenu(self)
        self.upgrade_menu.setStyleSheet("""
            QMenu {
                background-color: #2196f3;
                color: white;
                border: none;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 16px;
                border-radius: 2px;
            }
            QMenu::item:selected { background-color: #1976d2; }
        """)
        self.action_upgrade_all = self.upgrade_menu.addAction("⏫ Tout mettre à jour")
        self.btn_upgrade.setMenu(self.upgrade_menu)

        self.btn_refresh = QPushButton("🔄")
        self.btn_refresh.setToolTip("Rafraîchir la liste")
        self.btn_refresh.setFixedWidth(40)
        self.btn_refresh.setStyleSheet("""
            QPushButton {
                background-color: #607d8b;
                color: white;
                font-weight: bold;
                padding: 6px 8px;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #546e7a; }
            QPushButton:pressed { background-color: #455a64; }
        """)

        # Main area
        self.package_list = PackageList()

        # Filter panel
        self.filter_panel = FilterPanel(self.controller)
        self.filter_panel.setMinimumWidth(200)
        self.filter_panel.setMaximumWidth(300)

        # Custom bottom bar (replaces QStatusBar for expandable progress)
        self.bottom_bar = QWidget()
        bottom_layout = QVBoxLayout(self.bottom_bar)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)

        # Progress widget (collapsible)
        self.progress_widget = CollapsibleProgressWidget(num_slots=4)
        bottom_layout.addWidget(self.progress_widget)

        # Status line
        self.status_frame = QFrame()
        self.status_frame.setFrameStyle(QFrame.Shape.StyledPanel)
        status_layout = QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(8, 4, 8, 4)
        self.status_label = QLabel("Prêt")
        status_layout.addWidget(self.status_label)
        bottom_layout.addWidget(self.status_frame)

    def _create_layout(self) -> None:
        """Create layout."""
        # Central widget
        central = QWidget()
        self.setCentralWidget(central)

        # Main layout
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        # Top bar
        top_bar = QHBoxLayout()
        top_bar.addWidget(self.search_bar, stretch=1)
        top_bar.addSpacing(16)
        top_bar.addWidget(self.btn_install)
        top_bar.addWidget(self.btn_remove)
        top_bar.addWidget(self.btn_upgrade)
        top_bar.addSpacing(8)
        top_bar.addWidget(self.btn_refresh)

        main_layout.addLayout(top_bar)

        # Splitter for package list and filters
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.package_list)
        splitter.addWidget(self.filter_panel)
        splitter.setStretchFactor(0, 1)  # Package list stretches
        splitter.setStretchFactor(1, 0)  # Filter panel fixed
        splitter.setSizes([700, 250])

        main_layout.addWidget(splitter, stretch=1)

        # Bottom bar (progress + status)
        main_layout.addWidget(self.bottom_bar)

    def _create_shortcuts(self) -> None:
        """Create keyboard shortcuts."""
        # Ctrl+F: Focus search
        shortcut_search = QShortcut(QKeySequence("Ctrl+F"), self)
        shortcut_search.activated.connect(self.search_bar.focus_search)

        # Ctrl+A: Select all
        shortcut_select_all = QShortcut(QKeySequence("Ctrl+A"), self)
        shortcut_select_all.activated.connect(self.controller.select_all)

        # Escape: Clear selection
        shortcut_escape = QShortcut(QKeySequence("Escape"), self)
        shortcut_escape.activated.connect(self.controller.clear_selection)

        # Zoom: Ctrl+Plus / Ctrl+Minus / Ctrl+0
        shortcut_zoom_in = QShortcut(QKeySequence("Ctrl++"), self)
        shortcut_zoom_in.activated.connect(self.zoom_in)
        shortcut_zoom_in2 = QShortcut(QKeySequence("Ctrl+="), self)  # For keyboards without numpad
        shortcut_zoom_in2.activated.connect(self.zoom_in)

        shortcut_zoom_out = QShortcut(QKeySequence("Ctrl+-"), self)
        shortcut_zoom_out.activated.connect(self.zoom_out)

        shortcut_zoom_reset = QShortcut(QKeySequence("Ctrl+0"), self)
        shortcut_zoom_reset.activated.connect(self.zoom_reset)

        # F5: Refresh
        shortcut_refresh = QShortcut(QKeySequence("F5"), self)
        shortcut_refresh.activated.connect(self._on_refresh)

    def _connect_signals(self) -> None:
        """Connect widget signals to controller."""
        # Search
        self.search_bar.search_changed.connect(self.controller.set_search_term)

        # Package list
        self.package_list.selection_changed.connect(self._on_selection_changed)
        self.package_list.package_activated.connect(self._on_package_activated)

        # Action buttons
        self.btn_install.clicked.connect(self.controller.install_selection)
        self.btn_remove.clicked.connect(self.controller.erase_selection)
        self.btn_upgrade.clicked.connect(self.controller.upgrade_selection)
        self.action_upgrade_all.triggered.connect(self.controller.upgrade_all)
        self.btn_refresh.clicked.connect(self._on_refresh)

    def _on_selection_changed(self, name: str, selected: bool) -> None:
        """Handle package selection change."""
        if selected:
            self.controller.select_package(name)
        else:
            self.controller.unselect_package(name)

        # Update button states
        has_selection = len(self.controller.selection) > 0
        self.btn_install.setEnabled(has_selection)
        self.btn_remove.setEnabled(has_selection)
        self.btn_upgrade.setEnabled(has_selection)

    def _on_package_activated(self, name: str) -> None:
        """Handle package double-click."""
        # TODO: Show package details
        self.status_label.setText(f"Paquet: {name}")

    def _on_refresh(self) -> None:
        """Handle refresh button click."""
        self.set_loading(True)
        self.status_label.setText("Rafraîchissement...")
        # Use processEvents to show the loading state before blocking
        QApplication.processEvents()
        self.controller.refresh_after_transaction()
        self.filter_panel.populate_categories()
        self.set_loading(False)
        self.show_status_message("Liste rafraîchie", 2000)

    def set_loading(self, loading: bool) -> None:
        """Show or hide loading indicator."""
        if loading:
            self.status_label.setText("Chargement...")
        else:
            self.status_label.setText("Prêt")

    def show_status_message(self, message: str, timeout: int = 0) -> None:
        """Show a message in the status bar.

        Args:
            message: Message to show.
            timeout: Auto-clear after this many ms (0 = permanent).
        """
        self.status_label.setText(message)
        if timeout > 0:
            from .compat import QTimer
            QTimer.singleShot(timeout, lambda: self.status_label.setText("Prêt"))

    def _apply_font_size(self) -> None:
        """Apply font size to entire application."""
        # Use stylesheet for global font - propagates to all widgets
        QApplication.instance().setStyleSheet(f"* {{ font-size: {self._font_size}pt; }}")

        # Update package list row height for 2-line descriptions
        if hasattr(self, 'package_list'):
            self.package_list.update_row_height(self._font_size)

    def zoom_in(self) -> None:
        """Increase font size."""
        if self._font_size < self.MAX_FONT_SIZE:
            self._font_size += 1
            self._apply_font_size()
            self.show_status_message(f"Zoom: {self._font_size} pt", 2000)

    def zoom_out(self) -> None:
        """Decrease font size."""
        if self._font_size > self.MIN_FONT_SIZE:
            self._font_size -= 1
            self._apply_font_size()
            self.show_status_message(f"Zoom: {self._font_size} pt", 2000)

    def zoom_reset(self) -> None:
        """Reset font size to default."""
        self._font_size = self.DEFAULT_FONT_SIZE
        self._apply_font_size()
        self.show_status_message(f"Zoom: {self._font_size} pt (défaut)", 2000)

    def wheelEvent(self, event) -> None:
        """Handle mouse wheel with Ctrl for zoom."""
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            elif delta < 0:
                self.zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    def closeEvent(self, event) -> None:
        """Handle window close."""
        self.controller.shutdown()
        super().closeEvent(event)


def main() -> int:
    """Main entry point for rpmdrake-ng Qt frontend.

    Returns:
        Exit code.
    """
    app = QApplication(sys.argv)
    app.setApplicationName("rpmdrake-ng")
    app.setApplicationDisplayName("rpmdrake-ng")
    app.setOrganizationName("Mageia")

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
