"""Main window for rpmdrake-ng Qt frontend."""

import sys
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

from .compat import (
    Qt,
    QApplication,
    QMainWindow,
    QMessageBox,
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
    QColor,
    QPalette,
    QTimer,
)

from urpm.core.database import PackageDatabase

from ..common.controller import Controller, ControllerConfig
from .palette import button_stylesheet, get_secondary_colors
from .view import QtView
from .widgets.search_bar import SearchBar
from .widgets.package_list import PackageList, PackageTableModel
from .widgets.filter_zone import FilterZone
from .widgets.category_panel import CategoryPanel
from .widgets.detail_panel import PackageDetailPanel
from .widgets.download_progress import CollapsibleProgressWidget, SlotInfo

__all__ = ["MainWindow", "main"]


from PySide6.QtCore import Signal


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
        │ [📥 Installer][🗑 Enlever][⬆ Mettre à jour (N)][🔄]  N pkgs │
        └───────────────────────────────────────────────────────────────┘
    """

    # Signal emitted from background thread to finish loading on main thread
    _load_finished = Signal()

    MIN_FONT_SIZE = 8
    MAX_FONT_SIZE = 24
    DEFAULT_FONT_SIZE = 11

    def __init__(self, db: Optional[PackageDatabase] = None):
        super().__init__()

        self.setWindowTitle("rpmdrake-ng")
        self.resize(1100, 768)

        # Center on screen
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = (geo.width() - 1100) // 2 + geo.x()
            y = (geo.height() - 768) // 2 + geo.y()
            self.move(x, y)

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

        # Signal for thread-safe load completion
        self._load_finished.connect(self._finish_load)

        # Async detail loading: debounce rapid arrow-key navigation so we
        # don't spawn 4 rpm subprocesses per keystroke.
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(150)  # ms
        self._detail_timer.timeout.connect(self._fetch_detail_now)
        self._detail_pending_name: Optional[str] = None
        # Single-worker executor shared by detail fetches and refresh.
        # max_workers=1 ensures they never overlap, which avoids data races
        # on the controller's cache dicts (get_package_details reads them,
        # _load_installed_cache writes them).
        self._detail_executor = ThreadPoolExecutor(max_workers=1)
        self._detail_future: Optional[Future] = None

        # Package loading is deferred to _deferred_load() so the window
        # is visible immediately.  See main() which calls singleShot(0).

    def _deferred_load(self) -> None:
        """Load packages and categories after the window is visible.

        Called via QTimer.singleShot(0) from main() so the event loop
        has a chance to paint the empty window first.  The heavy loading
        runs in a background thread so the UI stays responsive.
        """
        self.set_loading(True)
        QApplication.processEvents()

        import threading

        def _bg_load():
            try:
                self.controller._load_installed_cache()
            except Exception:
                pass
            # Signal the main thread (thread-safe via Qt signal)
            self._load_finished.emit()

        threading.Thread(target=_bg_load, daemon=True).start()

    def _finish_load(self) -> None:
        """Complete initialization on the main thread after background loading."""
        self.controller._finish_load_initial()
        self.category_panel.populate_categories()
        self._update_button_states()

    # ------------------------------------------------------------------
    # Widget creation
    # ------------------------------------------------------------------

    def _create_widgets(self) -> None:
        """Instantiate all UI widgets."""
        # --- Top bar ---
        sc = get_secondary_colors()
        self.search_bar = SearchBar()

        toggle_border = sc['border']
        self.btn_filter_toggle = QPushButton("⊟")
        self.btn_filter_toggle.setToolTip("Afficher/masquer les filtres")
        self.btn_filter_toggle.setFixedWidth(32)
        self.btn_filter_toggle.setCheckable(True)
        self.btn_filter_toggle.setChecked(True)     # Filters visible by default
        self.btn_filter_toggle.setStyleSheet(
            f"QPushButton {{ border: 1px solid {toggle_border}; border-radius: 4px; padding: 4px; }}"
            "QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); }"
        )

        self.btn_cat_toggle = QPushButton("≡")
        self.btn_cat_toggle.setToolTip("Afficher/masquer les catégories")
        self.btn_cat_toggle.setFixedWidth(32)
        self.btn_cat_toggle.setCheckable(True)
        self.btn_cat_toggle.setChecked(True)    # Categories visible by default
        self.btn_cat_toggle.setStyleSheet(
            f"QPushButton {{ border: 1px solid {toggle_border}; border-radius: 4px; padding: 4px; }}"
            "QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); }"
        )

        # --- Filter zone (collapsible, visible by default) ---
        self.filter_zone = FilterZone(self.controller)

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

        self.btn_erase = QPushButton("🗑 Enlever")
        self.btn_erase.setEnabled(False)
        self.btn_erase.setStyleSheet(
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
        self.lbl_count.setStyleSheet(f"color: {sc['text_muted']};")

        # --- Status frame (when progress widget is hidden) ---
        self.status_frame = QFrame()
        self.status_frame.setFrameStyle(QFrame.Shape.StyledPanel)
        status_layout = QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(8, 4, 8, 4)
        self.status_label = QLabel("Prêt")
        status_layout.addWidget(self.status_label)

        # --- Focus policies ---
        # Only the primary workflow widgets participate in Tab navigation:
        # SearchBar → PackageList → Install → Erase → Upgrade → Refresh
        # Everything else is NoFocus (mouse only) or ClickFocus.
        self.btn_filter_toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_cat_toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.right_stack.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.category_panel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.detail_panel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.progress_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.progress_widget.cancel_btn.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.lbl_count.setFocusPolicy(Qt.FocusPolicy.NoFocus)

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
        action_bar.addWidget(self.btn_erase)
        action_bar.addWidget(self.btn_upgrade)
        action_bar.addStretch()
        action_bar.addWidget(self.lbl_count)
        action_bar.addSpacing(8)
        action_bar.addWidget(self.btn_refresh)
        main_layout.addLayout(action_bar)

        # Explicit Tab order: primary workflow chain only
        self.setTabOrder(self.search_bar, self.package_list)
        self.setTabOrder(self.package_list, self.btn_install)
        self.setTabOrder(self.btn_install, self.btn_erase)
        self.setTabOrder(self.btn_erase, self.btn_upgrade)
        self.setTabOrder(self.btn_upgrade, self.btn_refresh)
        self.setTabOrder(self.btn_refresh, self.search_bar)

    def _create_shortcuts(self) -> None:
        """Register keyboard shortcuts.

        Primary workflow:
            Ctrl+F          Focus search bar
            ↓ (in search)   Jump to package list
            Space (in list) Toggle package selection
            Ctrl+Enter      Contextual action (install/erase/upgrade)
            Ctrl+I          Install selected
            Ctrl+E          Erase selected
            Ctrl+U          Upgrade selected
            Ctrl+R          Refresh package list
            Shift+Ctrl+F    Toggle filter zone with keyboard focus
            Ctrl+G          Toggle category tree focus
            → (in list)     Jump to category tree
            → (in tree)     Expand node
            Ctrl+← (tree)  Collapse node
            ← / Esc (tree)  Return to package list
            Enter (tree)    Apply category and return to list

        Selection:
            Ctrl+A          Select all visible packages
            Escape          Clear selection

        Zoom:
            Ctrl++/=        Zoom in
            Ctrl+-          Zoom out
            Ctrl+0          Reset zoom
        """
        # Search & navigation
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            self.search_bar.focus_search
        )
        # Selection
        QShortcut(QKeySequence("Ctrl+A"), self).activated.connect(
            self.controller.select_all
        )
        QShortcut(QKeySequence("Escape"), self).activated.connect(
            self._on_clear_selection
        )

        # Actions
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(
            self._contextual_action
        )
        QShortcut(QKeySequence("Ctrl+I"), self).activated.connect(
            self.controller.install_selection
        )
        QShortcut(QKeySequence("Ctrl+E"), self).activated.connect(
            self.controller.erase_selection
        )
        QShortcut(QKeySequence("Ctrl+U"), self).activated.connect(
            self.controller.upgrade_selection
        )
        QShortcut(QKeySequence("Ctrl+R"), self).activated.connect(self._on_refresh)
        QShortcut(QKeySequence("F5"), self).activated.connect(self._on_refresh)

        # Filter zone toggle
        QShortcut(QKeySequence("Ctrl+Shift+F"), self).activated.connect(
            self._toggle_filter_zone_shortcut
        )

        # Category tree
        QShortcut(QKeySequence("Ctrl+G"), self).activated.connect(
            self._toggle_category_tree
        )

        # Help
        QShortcut(QKeySequence("Ctrl+H"), self).activated.connect(
            self._show_keyboard_help
        )

        # Zoom
        QShortcut(QKeySequence("Ctrl++"), self).activated.connect(self.zoom_in)
        QShortcut(QKeySequence("Ctrl+="), self).activated.connect(self.zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(self.zoom_out)
        QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(self.zoom_reset)

    def _connect_signals(self) -> None:
        """Wire widget signals to controller and slots."""
        self.search_bar.search_changed.connect(self.controller.set_search_term)
        self.search_bar.focus_list_requested.connect(self._focus_package_list)
        self.package_list.focus_search_requested.connect(self.search_bar.focus_search)
        self.package_list.focus_categories_requested.connect(self._focus_category_tree)
        self.category_panel.focus_list_requested.connect(
            lambda: self.package_list.setFocus()
        )

        # Checkbox-column selection change
        self.package_list.selection_changed.connect(self._on_checkbox_changed)
        self.package_list.section_check_toggled.connect(self._on_section_check_toggled)

        # Row selection → detail panel (keyboard and mouse)
        self.package_list.selectionModel().currentChanged.connect(
            self._on_current_row_changed
        )
        self.package_list.package_activated.connect(self._on_package_activated)

        # Action buttons
        self.btn_install.clicked.connect(self.controller.install_selection)
        self.btn_erase.clicked.connect(self.controller.erase_selection)
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

    def _on_checkbox_changed(self, nevra: str, selected: bool) -> None:
        """Handle checkbox selection change for a package."""
        if selected:
            # Warn if selecting an older (non-latest) version
            pkg = self._find_package_by_nevra(nevra)
            if pkg and not pkg.is_latest:
                result = QMessageBox.question(
                    self, "Ancienne version",
                    f"{pkg.name} {pkg.version}-{pkg.release} n'est pas la dernière "
                    f"version disponible.\n\nInstaller quand même cette ancienne version ?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if result != QMessageBox.StandardButton.Yes:
                    # Uncheck the checkbox visually
                    self.controller.unselect_package(nevra)
                    return
            self.controller.select_package(nevra)
        else:
            self.controller.unselect_package(nevra)
        self._update_button_states()

    def _on_section_check_toggled(self, title: str, checked: bool) -> None:
        """Handle section header checkbox toggle (select/deselect all in section)."""
        # Currently only the "Mises à jour" section is checkable
        if "Mises à jour" in title:
            if checked:
                self.controller.select_all_updates()
            else:
                self.controller.deselect_all_updates()
            self._update_button_states()

    def _find_package_by_nevra(self, nevra: str):
        """Find a PackageDisplayInfo by its NEVRA in the current package list.

        Uses the controller's NEVRA index for O(1) lookup.
        """
        return self.controller._nevra_index.get(nevra)

    def _on_current_row_changed(self, current, previous) -> None:
        """Schedule async detail fetch for the newly selected row.

        Uses a 150ms debounce timer so rapid arrow-key navigation doesn't
        spawn subprocesses for every intermediate row.
        """
        if not current.isValid():
            return
        if current.column() == PackageTableModel.COL_CHECKBOX:
            return
        pkg = self.package_list._model.get_package(current.row())
        if pkg is None:
            return  # Section header row — skip
        self._detail_pending_name = pkg.name
        self._detail_timer.start()  # (re)start the debounce timer

    def _fetch_detail_now(self) -> None:
        """Called when the debounce timer fires — launch background fetch."""
        name = self._detail_pending_name
        if not name:
            return
        # Cancel any in-flight fetch (result will be ignored)
        if self._detail_future and not self._detail_future.done():
            self._detail_future.cancel()
        self._detail_future = self._detail_executor.submit(
            self.controller.get_package_details, name
        )
        self._detail_future.add_done_callback(
            lambda f: self._on_detail_ready(f, name)
        )

    def _on_detail_ready(self, future: Future, name: str) -> None:
        """Receive detail result from background thread and display it.

        Runs on the executor thread.  Marshals the result to the main
        thread via QTimer.singleShot(0); the stale-check is done there
        (in _apply_detail) where it is safe to read _detail_pending_name.
        """
        if future.cancelled():
            return
        try:
            details = future.result()
        except Exception:
            return
        QTimer.singleShot(0, lambda: self._apply_detail(details, name))

    def _apply_detail(self, details: dict, name: str) -> None:
        """Apply fetched details to the detail panel (main thread)."""
        if self._detail_pending_name != name:
            return  # User navigated away while callback was queued
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

    def _focus_package_list(self) -> None:
        """Move focus to the package list and select first row if needed."""
        self.package_list.setFocus()
        if not self.package_list.currentIndex().isValid():
            self.package_list.selectRow(0)

    def _contextual_action(self) -> None:
        """Execute the appropriate action based on selected packages.

        Install if all selected are available, erase if all installed,
        upgrade if all upgradable.  Do nothing on mixed selections.
        """
        model = self.package_list._model
        categories: set[str] = set()

        for row in range(model.rowCount()):
            pkg = model.get_package(row)
            if pkg is None or not pkg.selected:
                continue
            if pkg.has_update:
                categories.add('upgrade')
            elif pkg.installed:
                categories.add('erase')
            else:
                categories.add('install')

        if len(categories) != 1:
            return

        action = categories.pop()
        if action == 'install':
            self.controller.install_selection()
        elif action == 'erase':
            self.controller.erase_selection()
        elif action == 'upgrade':
            self.controller.upgrade_selection()

    def _focus_category_tree(self) -> None:
        """Focus the category tree, ensuring the right panel is visible."""
        if not self.right_stack.isVisible():
            self.btn_cat_toggle.setChecked(True)
        if self.right_stack.currentWidget() is not self.category_panel:
            self.right_stack.setCurrentWidget(self.category_panel)
        self.category_panel.focus_tree()

    def _toggle_category_tree(self) -> None:
        """Toggle focus between category tree and package list (Ctrl+G)."""
        if self.category_panel._tree.hasFocus():
            self.package_list.setFocus()
        else:
            self._focus_category_tree()

    def _show_keyboard_help(self) -> None:
        """Show a dialog listing all keyboard shortcuts."""
        from .compat import QDialog, QVBoxLayout, QTextBrowser, QPushButton
        from .palette import button_stylesheet

        dialog = QDialog(self)
        dialog.setWindowTitle("Raccourcis clavier")
        dialog.setMinimumSize(480, 420)

        layout = QVBoxLayout(dialog)
        text = QTextBrowser()
        text.setOpenExternalLinks(False)
        text.setHtml("""
        <style>
            table { border-collapse: collapse; width: 100%; }
            th { text-align: left; padding: 6px 8px; border-bottom: 2px solid gray; }
            td { padding: 4px 8px; }
            kbd { background: #e0e0e0; border: 1px solid #999; border-radius: 3px;
                  padding: 1px 5px; font-family: monospace; font-size: 10pt; }
        </style>
        <h3>Navigation</h3>
        <table>
        <tr><td><kbd>Ctrl+F</kbd></td><td>Recherche</td></tr>
        <tr><td><kbd>↓</kbd> (recherche)</td><td>Aller à la liste</td></tr>
        <tr><td><kbd>↑</kbd> (1ère ligne)</td><td>Retour à la recherche</td></tr>
        <tr><td><kbd>→</kbd> (liste)</td><td>Aller aux catégories</td></tr>
        <tr><td><kbd>←</kbd> / <kbd>Esc</kbd> (arbre)</td><td>Retour à la liste</td></tr>
        <tr><td><kbd>Ctrl+G</kbd></td><td>Basculer catégories / liste</td></tr>
        <tr><td><kbd>Ctrl+Shift+F</kbd></td><td>Ouvrir/fermer les filtres</td></tr>
        <tr><td><kbd>Tab</kbd> / <kbd>Shift+Tab</kbd></td>
            <td>Recherche → Liste → Installer → Enlever → Upgrade → Refresh</td></tr>
        </table>

        <h3>Arbre des catégories</h3>
        <table>
        <tr><td><kbd>→</kbd></td><td>Ouvrir un nœud</td></tr>
        <tr><td><kbd>Ctrl+←</kbd></td><td>Fermer un nœud</td></tr>
        <tr><td><kbd>Entrée</kbd></td><td>Appliquer la catégorie et retour</td></tr>
        </table>

        <h3>Sélection</h3>
        <table>
        <tr><td><kbd>Espace</kbd></td><td>Sélectionner / désélectionner le paquet</td></tr>
        <tr><td><kbd>Ctrl+A</kbd></td><td>Tout sélectionner</td></tr>
        <tr><td><kbd>Esc</kbd></td><td>Tout désélectionner</td></tr>
        </table>

        <h3>Actions</h3>
        <table>
        <tr><td><kbd>Ctrl+Entrée</kbd></td><td>Action contextuelle (installer/enlever/upgrade)</td></tr>
        <tr><td><kbd>Ctrl+I</kbd></td><td>Installer</td></tr>
        <tr><td><kbd>Ctrl+E</kbd></td><td>Enlever</td></tr>
        <tr><td><kbd>Ctrl+U</kbd></td><td>Mettre à jour</td></tr>
        <tr><td><kbd>Ctrl+R</kbd> / <kbd>F5</kbd></td><td>Rafraîchir</td></tr>
        </table>

        <h3>Affichage</h3>
        <table>
        <tr><td><kbd>Ctrl++</kbd></td><td>Zoom +</td></tr>
        <tr><td><kbd>Ctrl+-</kbd></td><td>Zoom −</td></tr>
        <tr><td><kbd>Ctrl+0</kbd></td><td>Zoom par défaut</td></tr>
        <tr><td><kbd>Ctrl+H</kbd></td><td>Cette aide</td></tr>
        </table>
        """)
        layout.addWidget(text)

        btn = QPushButton("Fermer")
        btn.setStyleSheet(button_stylesheet("#607d8b", "#546e7a", "#455a64"))
        btn.clicked.connect(dialog.accept)
        btn.setFocus()
        layout.addWidget(btn)

        dialog.exec()

    def _toggle_filter_zone_shortcut(self) -> None:
        """Toggle filter zone visibility with keyboard focus management.

        - If focus is on a filter checkbox: close zone, return to search.
        - Otherwise: open/show zone and focus first checkbox.
        """
        focused = QApplication.focusWidget()
        in_filters = focused in self.filter_zone.checkboxes()

        if in_filters:
            # Leave filter zone
            self.btn_filter_toggle.setChecked(False)
            for chk in self.filter_zone.checkboxes():
                chk.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            self.search_bar.setFocus()
        else:
            # Enter filter zone
            if not self.filter_zone.isVisible():
                self.btn_filter_toggle.setChecked(True)
            for chk in self.filter_zone.checkboxes():
                chk.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            self.filter_zone.checkboxes()[0].setFocus()

    def _on_refresh(self) -> None:
        """Refresh package list: reload caches in background, update UI on main thread."""
        self.set_loading(True)
        self.status_label.setText("Rafraîchissement...")

        def _reload_caches():
            self.controller._load_installed_cache()
            self.controller._invalidate_cache()

        self._refresh_future = self._detail_executor.submit(_reload_caches)
        self._refresh_future.add_done_callback(
            lambda f: QTimer.singleShot(0, lambda: self._finish_refresh(f))
        )

    def _finish_refresh(self, future: Future) -> None:
        """Finalize refresh on the main thread (view updates must be here)."""
        try:
            future.result()  # Propagate any exception from the background thread
        except Exception as e:
            self.set_loading(False)
            from .compat import QMessageBox
            QMessageBox.warning(self, "Erreur", f"Échec du rafraîchissement : {e}")
            return
        self.controller._refresh_packages()
        self.controller.clear_selection()
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
        self.btn_erase.setEnabled(can_remove)

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
        self._detail_timer.stop()
        self._detail_executor.shutdown(wait=False)
        self.controller.shutdown()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _apply_system_dark_mode(app: QApplication) -> None:
    """Detect GNOME/GTK dark mode and apply a matching Qt palette.

    Qt6 does not follow the GNOME color-scheme preference by default.
    This reads the org.freedesktop.appearance portal (works on GNOME,
    Cinnamon, MATE, and any XDG-compliant desktop) and applies a dark
    QPalette when the system requests dark mode.

    Has no effect if the desktop already provides a Qt platform theme
    (KDE Breeze, qt6ct, etc.) or if the preference is not set.
    """
    # Only intervene if Qt didn't already pick up a dark palette
    if app.palette().color(QPalette.ColorRole.Window).lightness() < 128:
        return  # Already dark — nothing to do

    try:
        import subprocess
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=2,
        )
        if "prefer-dark" not in result.stdout:
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return

    # Build a dark palette from scratch
    dark = QPalette()
    dark.setColor(QPalette.ColorRole.Window,          QColor(43, 43, 43))
    dark.setColor(QPalette.ColorRole.WindowText,       QColor(220, 220, 220))
    dark.setColor(QPalette.ColorRole.Base,             QColor(30, 30, 30))
    dark.setColor(QPalette.ColorRole.AlternateBase,    QColor(50, 50, 50))
    dark.setColor(QPalette.ColorRole.Text,             QColor(220, 220, 220))
    dark.setColor(QPalette.ColorRole.Button,           QColor(53, 53, 53))
    dark.setColor(QPalette.ColorRole.ButtonText,       QColor(220, 220, 220))
    dark.setColor(QPalette.ColorRole.BrightText,       QColor(255, 255, 255))
    dark.setColor(QPalette.ColorRole.Highlight,        QColor(42, 130, 218))
    dark.setColor(QPalette.ColorRole.HighlightedText,  QColor(255, 255, 255))
    dark.setColor(QPalette.ColorRole.ToolTipBase,      QColor(50, 50, 50))
    dark.setColor(QPalette.ColorRole.ToolTipText,      QColor(220, 220, 220))
    dark.setColor(QPalette.ColorRole.PlaceholderText,  QColor(128, 128, 128))
    dark.setColor(QPalette.ColorRole.Link,             QColor(86, 164, 255))

    # Disabled state: dimmed text
    dark.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,       QColor(128, 128, 128))
    dark.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(128, 128, 128))
    dark.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(128, 128, 128))

    app.setPalette(dark)


def main() -> int:
    """Entry point for rpmdrake-ng Qt frontend.

    Returns:
        Application exit code.
    """
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    app = QApplication(sys.argv)
    app.setApplicationName("rpmdrake-ng")
    app.setApplicationDisplayName("rpmdrake-ng")
    app.setOrganizationName("Mageia")

    _apply_system_dark_mode(app)

    # Global focus indicator for keyboard navigation
    app.setStyleSheet(
        "QPushButton:focus {"
        "  border: 4px solid palette(shadow);"
        "  border-radius: 4px;"
        "}"
        "\nQLineEdit:focus { border: 2px solid palette(highlight); }"
        "\nQListView:focus { border: 2px solid palette(highlight); }"
    )

    from rpmdrake.i18n import init_qt_translation
    init_qt_translation(app)

    window = MainWindow()
    window.show()
    window.search_bar.setFocus()

    # Defer heavy loading so the window appears immediately
    from .compat import QTimer
    QTimer.singleShot(0, window._deferred_load)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
