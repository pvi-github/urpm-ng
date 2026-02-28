"""Filter panel widget for rpmdrake-ng."""

from typing import TYPE_CHECKING

from ..compat import (
    Qt,
    Signal,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QGroupBox,
    QTreeWidget,
    QTreeWidgetItem,
    QFrame,
    QPushButton,
)

if TYPE_CHECKING:
    from ...common.controller import Controller

from ...common.models import PackageState

__all__ = ["FilterPanel", "CollapsibleGroup"]


class CollapsibleGroup(QWidget):
    """A collapsible group widget with a clickable header."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._expanded = True

        # Header button
        self._header = QPushButton(f"▼ {title}")
        self._header.setFlat(True)
        self._header.setStyleSheet("""
            QPushButton {
                text-align: left;
                font-weight: bold;
                padding: 4px 8px;
                border: none;
                background: palette(mid);
            }
            QPushButton:hover {
                background: palette(dark);
            }
        """)
        self._header.clicked.connect(self._toggle)
        self._title = title

        # Content widget
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(4)

        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._header)
        layout.addWidget(self._content)

    def addWidget(self, widget: QWidget) -> None:
        """Add widget to the content area."""
        self._content_layout.addWidget(widget)

    def addLayout(self, layout) -> None:
        """Add layout to the content area."""
        self._content_layout.addLayout(layout)

    def content_layout(self) -> QVBoxLayout:
        """Return the content layout for direct manipulation."""
        return self._content_layout

    def _toggle(self) -> None:
        """Toggle expanded/collapsed state."""
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        arrow = "▼" if self._expanded else "▶"
        self._header.setText(f"{arrow} {self._title}")

    def setExpanded(self, expanded: bool) -> None:
        """Set expanded state."""
        if self._expanded != expanded:
            self._toggle()


class FilterPanel(QWidget):
    """Filter panel with state and display filters.

    Layout:
    ┌──────────────────┐
    │ État             │
    │ ☑ Mises à jour   │
    │ ☐ Installés      │
    │    ☑ Explicites  │
    │    ☑ Dépendances │
    │    ☐ Orphelins   │
    │ ☐ Disponibles    │
    ├──────────────────┤
    │ Afficher aussi   │
    │ ☐ Bibliothèques  │
    │ ☐ Devel (-devel) │
    │ ☐ Debug (-debug) │
    │ ☐ 32-bit (i586)  │
    └──────────────────┘
    """

    filter_changed = Signal()

    def __init__(self, controller: 'Controller', parent=None):
        super().__init__(parent)
        self.controller = controller

        self._create_widgets()
        self._create_layout()
        self._connect_signals()
        self._update_from_state()

    def _create_widgets(self) -> None:
        """Create filter widgets."""
        # Category tree (collapsible)
        self.category_tree = QTreeWidget()
        self.category_tree.setHeaderHidden(True)
        self.category_tree.setIndentation(12)
        self.category_tree.setRootIsDecorated(True)
        self.category_tree.setAnimated(True)
        self.category_tree.setFrameShape(QFrame.Shape.NoFrame)
        self.category_tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.category_tree.setStyleSheet("""
            QTreeWidget {
                background: transparent;
                border: none;
            }
            QTreeWidget::item {
                padding: 3px 0;
            }
            QTreeWidget::item:selected {
                background: palette(highlight);
                color: palette(highlighted-text);
            }
        """)

        # State filters
        self.chk_upgrades = QCheckBox("Mises à jour")
        self.chk_installed = QCheckBox("Installés")
        self.chk_available = QCheckBox("Disponibles")
        self.chk_available.setToolTip("Sélectionnez une catégorie ou entrez un terme de recherche")

        # Install reason sub-filters (under Installés)
        self.chk_explicit = QCheckBox("Explicites")
        self.chk_dependencies = QCheckBox("Dépendances")
        self.chk_orphans = QCheckBox("Orphelins")

        # Special filters
        self.chk_tasks = QCheckBox("Méta-paquets (task-*)")
        self.chk_tasks.setToolTip("Afficher uniquement les méta-paquets task-*")

        # Display filters
        self.chk_libs = QCheckBox("Bibliothèques")
        self.chk_devel = QCheckBox("Devel (-devel)")
        self.chk_debug = QCheckBox("Debug (-debug)")
        self.chk_i586 = QCheckBox("32-bit (i586)")

    def _create_layout(self) -> None:
        """Create layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # State group (collapsible)
        self.state_group = CollapsibleGroup("État")
        self.state_group.addWidget(self.chk_upgrades)
        self.state_group.addWidget(self.chk_installed)

        # Sub-filters for Installés (indented)
        sub_widget = QWidget()
        sub_layout = QVBoxLayout(sub_widget)
        sub_layout.setContentsMargins(20, 0, 0, 0)  # Indent
        sub_layout.setSpacing(2)
        sub_layout.addWidget(self.chk_explicit)
        sub_layout.addWidget(self.chk_dependencies)
        sub_layout.addWidget(self.chk_orphans)
        self.state_group.addWidget(sub_widget)

        self.state_group.addWidget(self.chk_available)
        self.state_group.addWidget(self.chk_tasks)
        layout.addWidget(self.state_group)

        # Category group (collapsible)
        self.category_group = CollapsibleGroup("Catégories")
        self.category_group.addWidget(self.category_tree)
        layout.addWidget(self.category_group, 1)  # stretch factor 1

        # Display group (collapsible)
        self.display_group = CollapsibleGroup("Afficher aussi")
        self.display_group.addWidget(self.chk_libs)
        self.display_group.addWidget(self.chk_devel)
        self.display_group.addWidget(self.chk_debug)
        self.display_group.addWidget(self.chk_i586)
        layout.addWidget(self.display_group)

    def _connect_signals(self) -> None:
        """Connect checkbox signals."""
        # State filters
        self.chk_upgrades.toggled.connect(
            lambda checked: self._on_state_toggled(PackageState.UPGRADES, checked)
        )
        self.chk_installed.toggled.connect(
            lambda checked: self._on_state_toggled(PackageState.INSTALLED, checked)
        )
        self.chk_available.toggled.connect(
            lambda checked: self._on_state_toggled(PackageState.AVAILABLE, checked)
        )

        # Install reason sub-filters
        self.chk_explicit.toggled.connect(
            lambda checked: self._on_display_toggled('explicit', checked)
        )
        self.chk_dependencies.toggled.connect(
            lambda checked: self._on_display_toggled('dependencies', checked)
        )
        self.chk_orphans.toggled.connect(
            lambda checked: self._on_display_toggled('orphans', checked)
        )

        # Display filters
        self.chk_libs.toggled.connect(
            lambda checked: self._on_display_toggled('libs', checked)
        )
        self.chk_devel.toggled.connect(
            lambda checked: self._on_display_toggled('devel', checked)
        )
        self.chk_debug.toggled.connect(
            lambda checked: self._on_display_toggled('debug', checked)
        )
        self.chk_i586.toggled.connect(
            lambda checked: self._on_display_toggled('i586', checked)
        )

        # Special filters
        self.chk_tasks.toggled.connect(
            lambda checked: self._on_display_toggled('tasks', checked)
        )

        # Category tree (both click and keyboard)
        self.category_tree.currentItemChanged.connect(self._on_category_changed)

    def _on_state_toggled(self, state: PackageState, checked: bool) -> None:
        """Handle state filter toggle."""
        fs = self.controller.filter_state

        if checked:
            fs.states.add(state)
        else:
            fs.states.discard(state)

        # Update sub-filter visibility
        self._update_sub_filter_state()

        self.controller._invalidate_cache()
        self.controller._refresh_packages()
        self.filter_changed.emit()

    def _on_display_toggled(self, filter_name: str, checked: bool) -> None:
        """Handle display filter toggle."""
        self.controller.set_display_filter(filter_name, checked)
        self.filter_changed.emit()

    def _update_sub_filter_state(self) -> None:
        """Enable/disable sub-filters based on Installés state."""
        installed_checked = PackageState.INSTALLED in self.controller.filter_state.states
        self.chk_explicit.setEnabled(installed_checked)
        self.chk_dependencies.setEnabled(installed_checked)
        self.chk_orphans.setEnabled(installed_checked)

    def _update_from_state(self) -> None:
        """Update checkboxes from controller state."""
        fs = self.controller.filter_state

        # Block signals to avoid recursion
        for chk in [self.chk_upgrades, self.chk_installed, self.chk_available,
                    self.chk_explicit, self.chk_dependencies, self.chk_orphans,
                    self.chk_libs, self.chk_devel, self.chk_debug, self.chk_i586,
                    self.chk_tasks]:
            chk.blockSignals(True)

        # State
        self.chk_upgrades.setChecked(PackageState.UPGRADES in fs.states)
        self.chk_installed.setChecked(PackageState.INSTALLED in fs.states)
        self.chk_available.setChecked(PackageState.AVAILABLE in fs.states)

        # Install reason sub-filters
        self.chk_explicit.setChecked(fs.show_explicit)
        self.chk_dependencies.setChecked(fs.show_dependencies)
        self.chk_orphans.setChecked(fs.show_orphans)

        # Display
        self.chk_libs.setChecked(fs.show_libs)
        self.chk_devel.setChecked(fs.show_devel)
        self.chk_debug.setChecked(fs.show_debug)
        self.chk_i586.setChecked(fs.show_i586)

        # Special
        self.chk_tasks.setChecked(fs.show_tasks)

        # Unblock
        for chk in [self.chk_upgrades, self.chk_installed, self.chk_available,
                    self.chk_explicit, self.chk_dependencies, self.chk_orphans,
                    self.chk_libs, self.chk_devel, self.chk_debug, self.chk_i586,
                    self.chk_tasks]:
            chk.blockSignals(False)

        # Update sub-filter enabled state
        self._update_sub_filter_state()

    def populate_categories(self) -> None:
        """Populate category tree with available groups.

        Called after controller.load_initial() to ensure groups are loaded.
        """
        self.category_tree.clear()
        groups = self.controller.get_available_groups()

        # Build hierarchical structure
        # Groups are like "Development/Python", "Multimedia/Audio", etc.
        tree_data = {}  # main_cat -> list of sub_cats

        for group in groups:
            if '/' in group:
                main_cat, sub_cat = group.split('/', 1)
            else:
                main_cat = group
                sub_cat = None

            if main_cat not in tree_data:
                tree_data[main_cat] = []
            if sub_cat:
                tree_data[main_cat].append((sub_cat, group))

        # Add "All categories" item
        all_item = QTreeWidgetItem(["◉ Toutes les catégories"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, None)
        self.category_tree.addTopLevelItem(all_item)

        # Category icons (simple unicode)
        cat_icons = {
            'Accessibility': '♿',
            'Archiving': '📦',
            'Communications': '📡',
            'Databases': '🗄',
            'Development': '⚙',
            'Documentation': '📖',
            'Education': '🎓',
            'Editors': '📝',
            'Emulators': '🎮',
            'File tools': '📁',
            'Games': '🎲',
            'Graphical desktop': '🖥',
            'Graphics': '🎨',
            'Monitoring': '📊',
            'Multimedia': '🎬',
            'Networking': '🌐',
            'Office': '💼',
            'Publishing': '📰',
            'Sciences': '🔬',
            'Security': '🔒',
            'Shells': '⌨',
            'Sound': '🔊',
            'System': '🖧',
            'Terminals': '🖳',
            'Text tools': '📄',
            'Toys': '🧸',
            'Video': '🎥',
        }

        # Add main categories with their sub-categories
        for main_cat in sorted(tree_data.keys()):
            icon = cat_icons.get(main_cat, '📂')
            sub_count = len(tree_data[main_cat])

            # Add count indicator if has children
            if sub_count > 0:
                label = f"{icon} {main_cat} ({sub_count})"
            else:
                label = f"{icon} {main_cat}"

            main_item = QTreeWidgetItem([label])
            main_item.setData(0, Qt.ItemDataRole.UserRole, main_cat)
            self.category_tree.addTopLevelItem(main_item)

            # Add sub-categories
            for sub_name, full_path in sorted(tree_data[main_cat]):
                sub_item = QTreeWidgetItem([sub_name])
                sub_item.setData(0, Qt.ItemDataRole.UserRole, full_path)
                main_item.addChild(sub_item)

        # Select "All" by default
        all_item.setSelected(True)
        self.category_tree.setCurrentItem(all_item)

    def _on_category_changed(self, current: QTreeWidgetItem, previous: QTreeWidgetItem) -> None:
        """Handle category tree selection change (mouse or keyboard)."""
        if current is None:
            return
        category = current.data(0, Qt.ItemDataRole.UserRole)
        self.controller.set_category_filter(category)
        self.filter_changed.emit()
