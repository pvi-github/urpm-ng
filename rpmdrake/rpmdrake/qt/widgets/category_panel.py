"""Category panel for rpmdrake-ng.

Right-hand panel (QStackedWidget page 0) showing the category tree
and a permanently visible colour legend at the bottom.
"""

from typing import TYPE_CHECKING

from ..compat import (
    Qt,
    Signal,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QTreeWidget,
    QTreeWidgetItem,
)
from ..palette import get_state_colors

if TYPE_CHECKING:
    from ...common.controller import Controller

__all__ = ["CategoryPanel"]


# Mapping of top-level RPM Group prefix → unicode icon.
#
# These entries are derived from the actual Mageia RPM group taxonomy,
# obtained by running: rpm -qa --qf '%{group}\n' | sort -u
# The hierarchy uses '/' as a separator (e.g. "Networking/WWW").
# Only the first path component (before the first '/') is used here.
_GROUP_ICONS: dict[str, str] = {
    'Accessibility':      '♿',
    'Archiving':          '📦',
    'Communications':     '📡',
    'Databases':          '🗄',
    'Development':        '⚙',
    'Documentation':      '📖',
    'Editors':            '📝',
    'Emulators':          '🎮',
    'File tools':         '📁',
    'Games':              '🎲',
    'Geography':          '🗺',
    'Graphical desktop':  '🖥',
    'Graphics':           '🎨',
    'Monitoring':         '📊',
    'Networking':         '🌐',
    'Office':             '💼',
    'Publishing':         '📰',
    'Sciences':           '🔬',
    'Security':           '🔒',
    'Shells':             '⌨',
    'Sound':              '🔊',
    'System':             '🖧',
    'Terminals':          '🖳',
    'Text tools':         '📄',
    'Toys':               '🧸',
    'Video':              '🎥',
}

# Legend entries: (state_key, badge_letter, description)
# Letters match StateBadgeDelegate exactly.
_LEGEND = [
    ('installed', 'I', 'Installé'),
    ('dep',       'D', 'Dépendance'),
    ('orphan',    'O', 'Orphelin'),
    ('update',    'U', 'Mise à jour'),
    ('conflict',  'C', 'Conflit'),
]


class CategoryPanel(QWidget):
    """Package category tree with a permanently visible colour legend.

    The legend always reflects the active theme (light/dark) because it
    calls :func:`~rpmdrake.qt.palette.get_state_colors` at paint time
    via :meth:`_refresh_legend`.

    Signals
    -------
    category_changed : emitted with the selected category string (or None
        for "All categories").
    """

    category_changed = Signal(object)   # str | None

    def __init__(self, controller: 'Controller', parent=None):
        super().__init__(parent)
        self.controller = controller
        self._setup_ui()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def populate_categories(self) -> None:
        """Rebuild the category tree from the controller's package database.

        Uses the RPM ``Group`` tag hierarchy directly (separator: ``/``),
        exactly as the legacy rpmdrake did.  The first path component becomes
        a top-level tree item; the remainder (if any) becomes a child item.

        Counts shown next to each item reflect the number of packages in
        that group, not the number of sub-categories.

        Call after :meth:`~rpmdrake.common.controller.Controller.load_initial`
        to ensure group metadata has been loaded.
        """
        self._tree.clear()
        groups = self.controller.get_available_groups()
        counts = self.controller.get_group_package_counts()

        # Build a two-level hierarchy from "Main/Sub" RPM group strings.
        # Keys are top-level prefixes; values are (sub_label, full_path) pairs.
        tree_data: dict[str, list[tuple[str, str]]] = {}
        for group in groups:
            main, _, _ = group.partition('/')
            if main not in tree_data:
                tree_data[main] = []
            if '/' in group:
                tree_data[main].append((group.split('/', 1)[1], group))

        # Pre-compute top-level package counts (sum of all sub-groups).
        top_counts: dict[str, int] = {}
        for group, count in counts.items():
            main = group.split('/')[0]
            top_counts[main] = top_counts.get(main, 0) + count

        total = sum(counts.values())

        # "All categories" top item
        all_item = QTreeWidgetItem([f"◉ Toutes les catégories ({total})"])
        all_item.setData(0, Qt.ItemDataRole.UserRole, None)
        self._tree.addTopLevelItem(all_item)

        for main_cat in sorted(tree_data.keys()):
            icon = _GROUP_ICONS.get(main_cat, '📂')
            n = top_counts.get(main_cat, 0)
            count_str = f" ({n})" if n else ""
            main_item = QTreeWidgetItem([f"{icon} {main_cat}{count_str}"])
            main_item.setData(0, Qt.ItemDataRole.UserRole, main_cat)
            self._tree.addTopLevelItem(main_item)

            for sub_label, full_path in sorted(tree_data[main_cat]):
                sub_n = counts.get(full_path, 0)
                sub_count_str = f" ({sub_n})" if sub_n else ""
                sub_item = QTreeWidgetItem([f"{sub_label}{sub_count_str}"])
                sub_item.setData(0, Qt.ItemDataRole.UserRole, full_path)
                main_item.addChild(sub_item)

        all_item.setSelected(True)
        self._tree.setCurrentItem(all_item)

        # Refresh legend colours after potential theme change
        self._refresh_legend()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Category tree (takes all available vertical space)
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(12)
        self._tree.setRootIsDecorated(True)
        self._tree.setAnimated(True)
        self._tree.setFrameShape(QFrame.Shape.NoFrame)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self._tree.setStyleSheet("""
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
        self._tree.currentItemChanged.connect(self._on_category_changed)
        layout.addWidget(self._tree, stretch=1)

        # Separator above legend
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        # Colour legend (always visible)
        self._legend_widget = QWidget()
        self._legend_layout = QVBoxLayout(self._legend_widget)
        self._legend_layout.setContentsMargins(6, 6, 6, 6)
        self._legend_layout.setSpacing(6)

        lbl_title = QLabel("Légende :")
        lbl_title.setStyleSheet("font-weight: bold;")
        self._legend_layout.addWidget(lbl_title)

        # Each legend row: [badge QLabel] [text QLabel]
        # Badge style mirrors StateBadgeDelegate: filled rounded square,
        # white bold letter, coloured background updated in _refresh_legend().
        self._legend_badges: list[QLabel] = []
        for _state_key, letter, label in _LEGEND:
            row_layout = QHBoxLayout()
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            badge = QLabel(letter)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedSize(20, 20)
            self._legend_badges.append(badge)

            text_lbl = QLabel(label)
            text_lbl.setMinimumHeight(20)

            row_layout.addWidget(badge)
            row_layout.addWidget(text_lbl)
            row_layout.addStretch()
            self._legend_layout.addLayout(row_layout)

        layout.addWidget(self._legend_widget)

        # Initial colour pass (may be called again after theme change)
        self._refresh_legend()

    def _refresh_legend(self) -> None:
        """Update legend badge colours from the active palette.

        Called after theme switches to keep the badges in sync with
        :func:`~rpmdrake.qt.palette.get_state_colors`.
        """
        colors = get_state_colors()
        for badge, (state_key, _letter, _label) in zip(self._legend_badges, _LEGEND):
            color = colors.get(state_key)
            hex_color = color.name() if color else '#9e9e9e'
            badge.setStyleSheet(
                f"background-color: {hex_color};"
                " color: white;"
                " font-weight: bold;"
                " border-radius: 3px;"
            )

    def _on_category_changed(
        self, current: QTreeWidgetItem, previous: QTreeWidgetItem
    ) -> None:
        if current is None:
            return
        category = current.data(0, Qt.ItemDataRole.UserRole)
        self.controller.set_category_filter(category)
        self.category_changed.emit(category)
