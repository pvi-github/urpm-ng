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
    QFrame,
    QStackedWidget,
)

from urpm.core.database import PackageDatabase

from ..common.controller import Controller, ControllerConfig
from .palette import button_stylesheet
from .view import QtView
from .widgets.search_bar import SearchBar
from .widgets.package_list import PackageList, PackageTableModel
from .widgets.filter_zone import FilterZone
from .widgets.category_panel import CategoryPanel
from .widgets.detail_panel import PackageDetailPanel
from .widgets.download_progress import CollapsibleProgressWidget, SlotInfo

__all__ = ["MainWindow", "main"]


class MainWindow(QMainWindow):
    """Main window for rpmdrake-ng.

    Layout::

        ┌──[🔍 Rechercher...][⊟][≡]────────────────────────────────────┐
        │ [FilterZone — caché par défaut]                               │
        ├────────────────────────────────────┬──────────────────────────┤
        │ PackageList (stretch)              │ QStackedWidget           │
        │   ══ Mises à jour (N) ══           │   page 0: CategoryPanel  │
        │   firefox  124→125  mga10  x86  ⬆ │   page 1: DetailPanel    │
        │   ══ Installés (N) ══              │                          │
        │   vim       9.1     mga10  x86  Ⓘ │                          │
        ├────────────────────────────────────┴──────────────────────────┤
        │ [CollapsibleProgressWidget]                                   │
        │ [📥 Installer][🗑 Supprimer][⬆ Mettre à jour (N)][🔄] N pkgs│
        └───────────────────────────────────────────────────────────────┘
    """

    MIN_FONT_SIZE = 8
    MAX_FONT_SIZE = 24
    DEFAULT_FONT_SIZE = 11

    def __init__(self, db: Optional[PackageDatabase] = None):
        super().__init__()

        self.setWindowTitle("rpmdrake-ng")
        self.resize(1100, 768)

        self._font_size = self.DEFAULT_FONT_SIZE
        self._apply_font_size()

        self.db = db or PackageDatabase()

        self.qt_view = QtView(self)
        self.controller = Controller(self.db, self.qt_view)

        self._create_widgets()
        self._create_layout()
        self._create_shortcuts()
        self._connect_signals()

        # Connect progress cancel
        self.progress_widget.cancel_requested.connect(self.controller.cancel_transaction)

        # Apply initial font size now that all widgets exist
        self._apply_font_size()

        # Load packages and categories
        self.controller.load_initial()
        self.category_panel.populate_categories()

        self._update_upgrade_button()

    # ------------------------------------------------------------------
    # Widget creation
    # ------------------------------------------------------------------

    def _create_widgets(self) -> None:
        """Instantiate all UI widgets."""
        # --- Top bar ---
        self.search_bar = SearchBar()

        self.btn_filter_toggle = QPushButton("⊟")
        self.btn_filter_toggle.setToolTip("Afficher/masquer les filtres")
        self.btn_filter_toggle.setFixedWidth(32)
        self.btn_filter_toggle.setCheckable(True)
        self.btn_filter_toggle.setStyleSheet(
            "QPushButton { border: 1px solid palette(mid); border-radius: 4px; padding: 4px; }"
            "QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); }"
        )

        self.btn_cat_toggle = QPushButton("≡")
        self.btn_cat_toggle.setToolTip("Afficher/masquer les catégories")
        self.btn_cat_toggle.setFixedWidth(32)
        self.btn_cat_toggle.setCheckable(True)
        self.btn_cat_toggle.setChecked(True)    # Categories visible by default
        self.btn_cat_toggle.setStyleSheet(
            "QPushButton { border: 1px solid palette(mid); border-radius: 4px; padding: 4px; }"
            "QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); }"
        )

        # --- Filter zone (collapsible, hidden by default) ---
        self.filter_zone = FilterZone(self.controller)
        self.filter_zone.hide()

        # --- Package list ---
        self.package_list = PackageList()

        # --- Right panel: QStackedWidget ---
        self.right_stack = QStackedWidget()
        self.right_stack.setMinimumWidth(280)

        self.category_panel = CategoryPanel(self.controller)
        self.detail_panel = PackageDetailPanel()

        self.right_stack.addWidget(self.category_panel)   # page 0
        self.right_stack.addWidget(self.detail_panel)     # page 1

        # --- Progress widget ---
        self.progress_widget = CollapsibleProgressWidget(num_slots=4)

        # --- Action buttons ---
        self.btn_install = QPushButton("📥 Installer")
        self.btn_install.setEnabled(False)
        self.btn_install.setStyleSheet(
            button_stylesheet("#4caf50", "#45a049", "#3d8b40", disabled="#9e9e9e")
        )

        self.btn_remove = QPushButton("🗑 Supprimer")
        self.btn_remove.setEnabled(False)
        self.btn_remove.setStyleSheet(
            button_stylesheet("#f44336", "#da190b", "#c1170a", disabled="#9e9e9e")
        )

        self.btn_upgrade = QPushButton("⬆ Mettre à jour")
        self.btn_upgrade.setEnabled(False)
        self.btn_upgrade.setStyleSheet(
            button_stylesheet("#9e9e9e", "#757575", "#616161", disabled="#bdbdbd")
        )

        self.btn_refresh = QPushButton("🔄")
        self.btn_refresh.setToolTip("Rafraîchir la liste")
        self.btn_refresh.setFixedWidth(40)
        self.btn_refresh.setStyleSheet(
            button_stylesheet("#607d8b", "#546e7a", "#455a64")
        )

        # --- Status / count label ---
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet("color: palette(mid);")

        # --- Status frame (when progress widget is hidden) ---
        self.status_frame = QFrame()
        self.status_frame.setFrameStyle(QFrame.Shape.StyledPanel)
        status_layout = QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(8, 4, 8, 4)
        self.status_label = QLabel("Prêt")
        status_layout.addWidget(self.status_label)

    def _create_layout(self) -> None:
        """Assemble the layout."""
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(6)

        # Top bar: search + filter toggle + cat toggle
        top_bar = QHBoxLayout()
        top_bar.addWidget(self.search_bar, stretch=1)
        top_bar.addSpacing(4)
        top_bar.addWidget(self.btn_filter_toggle)
        top_bar.addWidget(self.btn_cat_toggle)
        main_layout.addLayout(top_bar)

        # Filter zone (collapsible, below search bar)
        main_layout.addWidget(self.filter_zone)

        # Content splitter
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self.package_list)
        self._splitter.addWidget(self.right_stack)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setSizes([660, 300])
        main_layout.addWidget(self._splitter, stretch=1)

        # Independent preferred widths for each right-panel page.
        # Updated on splitterMoved; restored on page switch.
        self._cat_panel_width    = 300
        self._detail_panel_width = 400

        # Progress widget (shown during transactions)
        main_layout.addWidget(self.progress_widget)

        # Status frame
        main_layout.addWidget(self.status_frame)

        # Action bar
        action_bar = QHBoxLayout()
        action_bar.addWidget(self.btn_install)
        action_bar.addWidget(self.btn_remove)
        action_bar.addWidget(self.btn_upgrade)
        action_bar.addStretch()
        action_bar.addWidget(self.lbl_count)
        action_bar.addSpacing(8)
        action_bar.addWidget(self.btn_refresh)
        main_layout.addLayout(action_bar)

    def _create_shortcuts(self) -> None:
        """Register keyboard shortcuts."""
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            self.search_bar.focus_search
        )
        QShortcut(QKeySequence("Ctrl+A"), self).activated.connect(
            self.controller.select_all
        )
        QShortcut(QKeySequence("Escape"), self).activated.connect(
            self._on_clear_selection
        )
        QShortcut(QKeySequence("Ctrl++"), self).activated.connect(self.zoom_in)
        QShortcut(QKeySequence("Ctrl+="), self).activated.connect(self.zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(self.zoom_out)
        QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(self.zoom_reset)
        QShortcut(QKeySequence("F5"), self).activated.connect(self._on_refresh)

    def _connect_signals(self) -> None:
        """Wire widget signals to controller and slots."""
        self.search_bar.search_changed.connect(self.controller.set_search_term)

        # Checkbox-column selection change
        self.package_list.selection_changed.connect(self._on_checkbox_changed)

        # Row selection → detail panel (keyboard and mouse)
        self.package_list.selectionModel().currentChanged.connect(
            self._on_current_row_changed
        )
        self.package_list.package_activated.connect(self._on_package_activated)

        # Action buttons
        self.btn_install.clicked.connect(self.controller.install_selection)
        self.btn_remove.clicked.connect(self.controller.erase_selection)
        self.btn_upgrade.clicked.connect(self.controller.upgrade_selection)
        self.btn_refresh.clicked.connect(self._on_refresh)

        # Toggle buttons
        self.btn_filter_toggle.toggled.connect(self._on_filter_toggle)
        self.btn_cat_toggle.toggled.connect(self._on_cat_toggle)

        # Detail panel "← Catégories" button
        self.detail_panel.back_clicked.connect(self._show_category_panel)

        # Forward Ctrl+wheel from description text to app zoom
        self.detail_panel.zoom_requested.connect(
            lambda direction: self.zoom_in() if direction > 0 else self.zoom_out()
        )

        # Track splitter moves to remember each panel's preferred width
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_checkbox_changed(self, name: str, selected: bool) -> None:
        """Handle checkbox selection change for a package."""
        if selected:
            self.controller.select_package(name)
        else:
            self.controller.unselect_package(name)
        self._update_button_states()
        self._update_upgrade_button()

    def _on_current_row_changed(self, current, previous) -> None:
        """Load and display details for the newly selected row."""
        if not current.isValid():
            return
        # Checkbox clicks change currentIndex to COL_CHECKBOX — ignore them
        # so that ticking a checkbox doesn't unexpectedly open the detail panel.
        if current.column() == PackageTableModel.COL_CHECKBOX:
            return
        pkg = self.package_list._model.get_package(current.row())
        if pkg is None:
            return  # Section header row — skip
        details = self.controller.get_package_details(pkg.name)
        self.detail_panel.show_package(details)
        self._show_detail_panel()

    def _on_package_activated(self, name: str) -> None:
        """Handle double-click: ensure detail panel is visible."""
        self._show_detail_panel()

    def _show_detail_panel(self) -> None:
        """Switch right stack to the detail panel, restoring its preferred width."""
        self.right_stack.setCurrentIndex(1)
        sizes = self._splitter.sizes()
        total = sizes[0] + sizes[1]
        self._splitter.setSizes([total - self._detail_panel_width, self._detail_panel_width])

    def _show_category_panel(self) -> None:
        """Switch right stack to the category panel, restoring its preferred width."""
        self.right_stack.setCurrentIndex(0)
        sizes = self._splitter.sizes()
        total = sizes[0] + sizes[1]
        self._splitter.setSizes([total - self._cat_panel_width, self._cat_panel_width])

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        """Remember the current right-panel width for the active page."""
        width = self._splitter.sizes()[1]
        if self.right_stack.currentIndex() == 1:
            self._detail_panel_width = width
        else:
            self._cat_panel_width = width

    def _on_filter_toggle(self, checked: bool) -> None:
        self.filter_zone.setVisible(checked)

    def _on_cat_toggle(self, checked: bool) -> None:
        if not checked:
            self.right_stack.setVisible(False)
        else:
            self.right_stack.setVisible(True)
            self._show_category_panel()

    def _on_clear_selection(self) -> None:
        """Clear all checkbox selections and refresh action buttons."""
        self.controller.clear_selection()
        self._update_button_states()

    def _on_refresh(self) -> None:
        """Refresh package list."""
        self.set_loading(True)
        self.status_label.setText("Rafraîchissement...")
        QApplication.processEvents()
        self.controller.refresh_after_transaction()
        self.category_panel.populate_categories()
        self.set_loading(False)
        self.show_status_message("Liste rafraîchie", 2000)

    # ------------------------------------------------------------------
    # UI state helpers
    # ------------------------------------------------------------------

    def _update_button_states(self) -> None:
        """Enable/disable action buttons based on the state of selected packages."""
        can_install = False
        can_remove  = False
        can_upgrade = False
        n_upgrades  = 0

        model = self.package_list._model
        for row in range(model.rowCount()):
            pkg = model.get_package(row)
            if pkg is None or not pkg.selected:
                continue
            if pkg.has_update:
                can_upgrade = True
                can_remove  = True   # an update implies the package is installed
                n_upgrades += 1
            elif pkg.installed:
                can_remove = True
            else:
                can_install = True

        self.btn_install.setEnabled(can_install)
        self.btn_remove.setEnabled(can_remove)

        if can_upgrade:
            self.btn_upgrade.setText(f"⬆ Mettre à jour ({n_upgrades})")
            self.btn_upgrade.setEnabled(True)
            self.btn_upgrade.setStyleSheet(
                button_stylesheet("#fb8c00", "#ef6c00", "#e65100", disabled="#9e9e9e")
            )
        else:
            self.btn_upgrade.setText("⬆ Mettre à jour")
            self.btn_upgrade.setEnabled(False)
            self.btn_upgrade.setStyleSheet(
                button_stylesheet("#9e9e9e", "#757575", "#616161", disabled="#bdbdbd")
            )

    def _update_upgrade_button(self) -> None:
        """Kept for compatibility — delegates to _update_button_states."""
        self._update_button_states()

    def set_loading(self, loading: bool) -> None:
        """Show or hide loading indicator."""
        self.status_label.setText("Chargement..." if loading else "Prêt")

    def show_status_message(self, message: str, timeout: int = 0) -> None:
        """Display a status message, optionally auto-clearing after *timeout* ms."""
        self.status_label.setText(message)
        if timeout > 0:
            from .compat import QTimer
            QTimer.singleShot(timeout, lambda: self.status_label.setText("Prêt"))

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------

    def _apply_font_size(self) -> None:
        font = QApplication.instance().font()
        font.setPointSize(self._font_size)
        QApplication.instance().setFont(font)
        if hasattr(self, 'package_list'):
            self.package_list.update_row_height(self._font_size)
        if hasattr(self, 'detail_panel'):
            self.detail_panel.update_font_size(self._font_size)

    def zoom_in(self) -> None:
        if self._font_size < self.MAX_FONT_SIZE:
            self._font_size += 1
            self._apply_font_size()
            self.show_status_message(f"Zoom : {self._font_size} pt", 2000)

    def zoom_out(self) -> None:
        if self._font_size > self.MIN_FONT_SIZE:
            self._font_size -= 1
            self._apply_font_size()
            self.show_status_message(f"Zoom : {self._font_size} pt", 2000)

    def zoom_reset(self) -> None:
        self._font_size = self.DEFAULT_FONT_SIZE
        self._apply_font_size()
        self.show_status_message(f"Zoom : {self._font_size} pt (défaut)", 2000)

    def wheelEvent(self, event) -> None:
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    def closeEvent(self, event) -> None:
        self.controller.shutdown()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Entry point for rpmdrake-ng Qt frontend.

    Returns:
        Application exit code.
    """
    app = QApplication(sys.argv)
    app.setApplicationName("rpmdrake-ng")
    app.setApplicationDisplayName("rpmdrake-ng")
    app.setOrganizationName("Mageia")

    from rpmdrake.i18n import init_qt_translation
    init_qt_translation(app)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
