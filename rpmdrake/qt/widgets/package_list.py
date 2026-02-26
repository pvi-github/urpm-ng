"""Package list widget for rpmdrake-ng."""

from typing import List, Optional

from ..compat import (
    Qt,
    Signal,
    QTableView,
    QAbstractItemView,
    QHeaderView,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionButton,
    QApplication,
    QAbstractTableModel,
    QModelIndex,
    QRect,
    QMouseEvent,
    QColor,
    QBrush,
    QFont,
    QFontMetrics,
)

from ...common.models import PackageDisplayInfo, InstallReason

__all__ = ["PackageList", "PackageTableModel"]


class PackageTableModel(QAbstractTableModel):
    """Table model for package list.

    Supports virtual scrolling by only storing data, not widgets.
    """

    COLUMNS = ['', '#', 'État', 'Nom', 'Version', 'Description']
    COL_CHECKBOX = 0
    COL_NUMBER = 1
    COL_STATE = 2
    COL_NAME = 3
    COL_VERSION = 4
    COL_SUMMARY = 5

    # State indicators (circled letters - good font support)
    STATE_ICONS = {
        'installed': 'Ⓘ',    # U+24BE Circled I - Installed explicitly
        'dep': 'Ⓓ',          # U+24B9 Circled D - Dependency
        'orphan': 'Ⓞ',       # U+24C4 Circled O - Orphan
        'update': 'Ⓤ',       # U+24CA Circled U - Update available
        'available': '',     # Not installed
        'conflict': '✗',     # Conflict
    }

    # Row colors based on state
    STATE_COLORS = {
        'installed': QColor('#2196f3'),   # Bright blue for explicitly installed
        'dep': QColor('#0d47a1'),         # Dark blue for dependencies
        'orphan': QColor('#9966cc'),      # Soft purple for orphans
        'update': QColor('#e6a030'),      # Soft orange for updates
        'available': None,                # Default color
        'conflict': QColor('#cc4444'),    # Soft red for conflicts
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._packages: List[PackageDisplayInfo] = []
        self._sort_column = self.COL_NAME
        self._sort_order = Qt.SortOrder.AscendingOrder

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._packages)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()

        if row < 0 or row >= len(self._packages):
            return None

        pkg = self._packages[row]

        if role == Qt.ItemDataRole.DisplayRole:
            if col == self.COL_NUMBER:
                return str(pkg.row_number)
            elif col == self.COL_STATE:
                return self._get_state_icon(pkg)
            elif col == self.COL_NAME:
                return pkg.name
            elif col == self.COL_VERSION:
                return pkg.display_version
            elif col == self.COL_SUMMARY:
                return pkg.summary
            return None

        elif role == Qt.ItemDataRole.CheckStateRole:
            if col == self.COL_CHECKBOX:
                return Qt.CheckState.Checked if pkg.selected else Qt.CheckState.Unchecked
            return None

        elif role == Qt.ItemDataRole.ForegroundRole:
            # Row color based on package state
            color = self._get_state_color(pkg)
            if color:
                return QBrush(color)
            return None

        elif role == Qt.ItemDataRole.FontRole:
            # Bold for explicitly installed packages (not updates)
            if pkg.installed and not pkg.has_update:
                if pkg.install_reason != InstallReason.DEPENDENCY and pkg.install_reason != InstallReason.ORPHAN:
                    font = QFont()
                    font.setBold(True)
                    return font
            return None

        elif role == Qt.ItemDataRole.ToolTipRole:
            if col == self.COL_NAME:
                return pkg.nevra
            elif col == self.COL_STATE:
                return self._get_state_tooltip(pkg)
            elif col == self.COL_VERSION:
                if pkg.has_update:
                    return f"Mise à jour disponible: {pkg.installed_version} → {pkg.version}-{pkg.release}"
            return None

        elif role == Qt.ItemDataRole.UserRole:
            # Return the package object
            return pkg

        return None

    def _get_state_icon(self, pkg: PackageDisplayInfo) -> str:
        """Get state icon for package."""
        if pkg.has_conflict:
            return self.STATE_ICONS['conflict']

        # Determine base icon based on install state
        if pkg.installed:
            if pkg.install_reason == InstallReason.DEPENDENCY:
                base_icon = self.STATE_ICONS['dep']
            elif pkg.install_reason == InstallReason.ORPHAN:
                base_icon = self.STATE_ICONS['orphan']
            else:
                base_icon = self.STATE_ICONS['installed']
        else:
            base_icon = self.STATE_ICONS['available']

        # Add update icon if update available
        if pkg.has_update:
            return base_icon + self.STATE_ICONS['update']

        return base_icon

    def _get_state_color(self, pkg: PackageDisplayInfo) -> QColor:
        """Get row color based on package state."""
        if pkg.has_conflict:
            return self.STATE_COLORS['conflict']

        # Update takes priority for color
        if pkg.has_update:
            return self.STATE_COLORS['update']

        if pkg.installed:
            if pkg.install_reason == InstallReason.DEPENDENCY:
                return self.STATE_COLORS['dep']
            elif pkg.install_reason == InstallReason.ORPHAN:
                return self.STATE_COLORS['orphan']
            return self.STATE_COLORS['installed']

        return self.STATE_COLORS['available']

    def _get_state_tooltip(self, pkg: PackageDisplayInfo) -> str:
        """Get state tooltip for package."""
        if pkg.has_conflict:
            return f"En conflit avec: {pkg.conflict_with or 'autre paquet'}"
        if pkg.has_update:
            return "Mise à jour disponible"
        if pkg.installed:
            if pkg.install_reason == InstallReason.DEPENDENCY:
                return "Installé comme dépendance"
            elif pkg.install_reason == InstallReason.ORPHAN:
                return "Orphelin (plus nécessaire)"
            return "Installé explicitement"
        return "Non installé"

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid():
            return False

        if role == Qt.ItemDataRole.CheckStateRole and index.column() == self.COL_CHECKBOX:
            row = index.row()
            if 0 <= row < len(self._packages):
                self._packages[row].selected = (value == Qt.CheckState.Checked)
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
                return True

        return False

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags

        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

        if index.column() == self.COL_CHECKBOX:
            flags |= Qt.ItemFlag.ItemIsUserCheckable

        return flags

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole
    ):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if 0 <= section < len(self.COLUMNS):
                return self.COLUMNS[section]
        return None

    def set_packages(self, packages: List[PackageDisplayInfo]) -> None:
        """Set the package list.

        Args:
            packages: List of packages to display.
        """
        self.beginResetModel()
        self._packages = packages
        self.endResetModel()

    def get_package(self, row: int) -> Optional[PackageDisplayInfo]:
        """Get package at row.

        Args:
            row: Row index.

        Returns:
            Package or None if invalid row.
        """
        if 0 <= row < len(self._packages):
            return self._packages[row]
        return None

    def get_package_by_name(self, name: str) -> Optional[PackageDisplayInfo]:
        """Get package by name.

        Args:
            name: Package name.

        Returns:
            Package or None if not found.
        """
        for pkg in self._packages:
            if pkg.name == name:
                return pkg
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Sort packages by column."""
        self._sort_column = column
        self._sort_order = order

        if not self._packages:
            return

        reverse = (order == Qt.SortOrder.DescendingOrder)

        def sort_key(pkg: PackageDisplayInfo):
            if column == self.COL_NAME:
                return pkg.name.lower()
            elif column == self.COL_VERSION:
                return pkg.version
            elif column == self.COL_SUMMARY:
                return pkg.summary.lower() if pkg.summary else ''
            elif column == self.COL_STATE:
                # Sort by state priority: updates, explicit, deps, orphans, available
                if pkg.has_update:
                    priority = 0
                elif pkg.installed:
                    if pkg.install_reason == InstallReason.ORPHAN:
                        priority = 3
                    elif pkg.install_reason == InstallReason.DEPENDENCY:
                        priority = 2
                    else:
                        priority = 1  # Explicit
                else:
                    priority = 4  # Available
                return (priority, pkg.name.lower())
            elif column == self.COL_NUMBER:
                return pkg.row_number
            return pkg.name.lower()

        self.beginResetModel()
        self._packages.sort(key=sort_key, reverse=reverse)
        self.endResetModel()


class CheckboxDelegate(QStyledItemDelegate):
    """Delegate for rendering checkboxes in the first column."""

    def paint(self, painter, option, index):
        if index.column() == PackageTableModel.COL_CHECKBOX:
            # Draw checkbox
            checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked

            checkbox_option = QStyleOptionButton()
            checkbox_option.state = QStyle.StateFlag.State_Enabled
            if checked:
                checkbox_option.state |= QStyle.StateFlag.State_On
            else:
                checkbox_option.state |= QStyle.StateFlag.State_Off

            # Center the checkbox
            checkbox_rect = QApplication.style().subElementRect(
                QStyle.SubElement.SE_CheckBoxIndicator, checkbox_option
            )
            x = option.rect.x() + (option.rect.width() - checkbox_rect.width()) // 2
            y = option.rect.y() + (option.rect.height() - checkbox_rect.height()) // 2
            checkbox_option.rect = QRect(x, y, checkbox_rect.width(), checkbox_rect.height())

            QApplication.style().drawControl(
                QStyle.ControlElement.CE_CheckBox, checkbox_option, painter
            )
        else:
            super().paint(painter, option, index)

    def editorEvent(self, event, model, option, index):
        if index.column() == PackageTableModel.COL_CHECKBOX:
            if event.type() == QMouseEvent.Type.MouseButtonRelease:
                # Toggle checkbox
                current = index.data(Qt.ItemDataRole.CheckStateRole)
                new_value = Qt.CheckState.Unchecked if current == Qt.CheckState.Checked else Qt.CheckState.Checked
                model.setData(index, new_value, Qt.ItemDataRole.CheckStateRole)
                return True
        return super().editorEvent(event, model, option, index)


class PackageList(QTableView):
    """Table view for package list.

    Features:
    - Virtual scrolling for performance
    - Checkbox selection
    - Row number column for command reference
    - Sortable columns
    """

    selection_changed = Signal(str, bool)  # package_name, selected
    package_activated = Signal(str)  # package_name (double-click)

    def __init__(self, parent=None):
        super().__init__(parent)

        # Create model
        self._model = PackageTableModel(self)
        self.setModel(self._model)

        # Set delegate for checkboxes
        self.setItemDelegate(CheckboxDelegate(self))

        # Configure view
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(True)
        self.setShowGrid(False)
        self.setWordWrap(True)  # Enable word wrap for descriptions

        # Virtual scrolling
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(40)  # Initial row height for 2 lines

        # Column sizing
        header = self.horizontalHeader()
        header.setSectionResizeMode(PackageTableModel.COL_CHECKBOX, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(PackageTableModel.COL_NUMBER, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(PackageTableModel.COL_STATE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(PackageTableModel.COL_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(PackageTableModel.COL_VERSION, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(PackageTableModel.COL_SUMMARY, QHeaderView.ResizeMode.Stretch)

        # Fixed column widths
        self.setColumnWidth(PackageTableModel.COL_CHECKBOX, 30)
        self.setColumnWidth(PackageTableModel.COL_NUMBER, 50)
        self.setColumnWidth(PackageTableModel.COL_STATE, 60)  # Wider for 🅘🅤 double icons
        self.setColumnWidth(PackageTableModel.COL_NAME, 200)
        self.setColumnWidth(PackageTableModel.COL_VERSION, 150)

        # Connect signals
        self.doubleClicked.connect(self._on_double_click)
        self._model.dataChanged.connect(self._on_data_changed)

    def set_packages(self, packages: List[PackageDisplayInfo]) -> None:
        """Set the package list.

        Args:
            packages: List of packages to display.
        """
        self._model.set_packages(packages)

    def _on_double_click(self, index: QModelIndex) -> None:
        """Handle double-click on a row."""
        pkg = self._model.get_package(index.row())
        if pkg:
            self.package_activated.emit(pkg.name)

    def _on_data_changed(
        self,
        top_left: QModelIndex,
        bottom_right: QModelIndex,
        roles: List[int]
    ) -> None:
        """Handle data change (checkbox toggle)."""
        if Qt.ItemDataRole.CheckStateRole in roles:
            for row in range(top_left.row(), bottom_right.row() + 1):
                pkg = self._model.get_package(row)
                if pkg:
                    self.selection_changed.emit(pkg.name, pkg.selected)

    def update_row_height(self, font_size: int) -> None:
        """Update row height based on font size.

        Args:
            font_size: Current font size in points.
        """
        # Create a font with the given size to calculate metrics
        font = QFont()
        font.setPointSize(font_size)
        metrics = QFontMetrics(font)

        # Height for 2 lines + padding
        line_height = metrics.height()
        row_height = (line_height * 2) + 8  # 2 lines + 8px padding

        self.verticalHeader().setDefaultSectionSize(row_height)
