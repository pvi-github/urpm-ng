"""Package list widget for rpmdrake-ng."""

from typing import List, Optional, Union

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
    QSize,
    QMouseEvent,
    QColor,
    QBrush,
    QPalette,
    QPainter,
    QPen,
    QFont,
    QFontMetrics,
)

from ...common.models import PackageDisplayInfo, SectionHeader, InstallReason
from ..palette import get_state_colors

__all__ = ["PackageList", "PackageTableModel", "SectionHeader"]


# ---------------------------------------------------------------------------
# Module-level color constants
# ---------------------------------------------------------------------------

# Section header background/foreground
_SECTION_BG = QColor("#546e7a")
_SECTION_FG = QColor("#ffffff")

# Active row = navigation cursor / current row (no checkbox)
_ACTIVE_BG_LIGHT = QColor("#e3f2fd")   # Blue 50
_ACTIVE_FG_LIGHT = QColor("#000000")
_ACTIVE_BG_DARK  = QColor("#0d47a1")   # Blue 900
_ACTIVE_FG_DARK  = QColor("#ffffff")

# Selected row = checkbox checked
_SEL_BG_LIGHT = QColor("#bbdefb")      # Blue 100
_SEL_FG_LIGHT = QColor("#000000")
_SEL_BG_DARK  = QColor("#1565c0")      # Blue 800
_SEL_FG_DARK  = QColor("#ffffff")


# Type alias for model rows
_Row = Union[PackageDisplayInfo, SectionHeader]


def _pkg_state_key(pkg: PackageDisplayInfo) -> str:
    """Return the state key string for a package.

    Used by both :class:`PackageTableModel` and :class:`StateBadgeDelegate`
    to ensure consistent state determination across model and view.
    """
    if pkg.has_conflict:
        return 'conflict'
    if pkg.has_update:
        return 'update'
    if pkg.installed:
        if pkg.install_reason == InstallReason.DEPENDENCY:
            return 'dep'
        if pkg.install_reason == InstallReason.ORPHAN:
            return 'orphan'
        return 'installed'
    return 'available'


# ---------------------------------------------------------------------------
# Table model
# ---------------------------------------------------------------------------

class PackageTableModel(QAbstractTableModel):
    """Table model for the package list.

    Rows are either :class:`PackageDisplayInfo` (normal package rows) or
    :class:`SectionHeader` (non-interactive separators).

    Columns
    -------
    COL_PACKAGE  — name (bold) + summary below, custom delegate
    COL_VERSION  — version only (with upgrade arrow when update available)
    COL_RELEASE  — release tag
    COL_ARCH     — architecture
    COL_STATE    — circled-letter state icon, coloured
    COL_CHECKBOX — selection checkbox (rightmost)
    """

    COLUMNS = ['Paquets', 'Version', 'Rév.', 'Arch', 'État', '']

    COL_PACKAGE  = 0
    COL_VERSION  = 1
    COL_RELEASE  = 2
    COL_ARCH     = 3
    COL_STATE    = 4
    COL_CHECKBOX = 5

    # Circled-letter state icons
    STATE_ICONS = {
        'installed': 'Ⓘ',
        'dep':       'Ⓓ',
        'orphan':    'Ⓞ',
        'update':    '↑',   # simple upward arrow — unambiguous
        'available': '',
        'conflict':  '⚠',  # warning triangle — clearer than ✗
    }

    # Section header colors — defined at module level, referenced here for clarity
    _SECTION_BG = _SECTION_BG
    _SECTION_FG = _SECTION_FG

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: List[_Row] = []
        self._syncing_sections = False

    # -----------------------------------------------------------------------
    # QAbstractTableModel interface
    # -----------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()

        if row < 0 or row >= len(self._rows):
            return None

        item = self._rows[row]

        # --- Section header rows ---
        if isinstance(item, SectionHeader):
            if role == Qt.ItemDataRole.DisplayRole and col == self.COL_PACKAGE:
                return item.title
            if role == Qt.ItemDataRole.BackgroundRole:
                return QBrush(self._SECTION_BG)
            if role == Qt.ItemDataRole.ForegroundRole:
                return QBrush(self._SECTION_FG)
            if role == Qt.ItemDataRole.FontRole:
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.ItemDataRole.CheckStateRole and col == self.COL_CHECKBOX:
                if item.checkable:
                    return Qt.CheckState.Checked if item.checked else Qt.CheckState.Unchecked
            return None

        # --- Normal package rows ---
        pkg: PackageDisplayInfo = item

        if role == Qt.ItemDataRole.DisplayRole:
            if col == self.COL_PACKAGE:
                return pkg.name          # PackageDelegate draws name + summary
            elif col == self.COL_VERSION:
                return self._version_display(pkg)
            elif col == self.COL_RELEASE:
                return pkg.release
            elif col == self.COL_ARCH:
                return pkg.arch
            elif col == self.COL_STATE:
                return self._state_icon(pkg)
            return None

        elif role == Qt.ItemDataRole.CheckStateRole:
            if col == self.COL_CHECKBOX:
                return Qt.CheckState.Checked if pkg.selected else Qt.CheckState.Unchecked
            return None

        elif role == Qt.ItemDataRole.ForegroundRole:
            # Colour only the state badge column — other columns keep default text colour.
            if col == self.COL_STATE:
                color = self._state_color(pkg)
                return QBrush(color) if color else None
            # Dim text for older (non-latest) package versions
            if not pkg.is_latest:
                from ..palette import get_secondary_colors
                return QBrush(QColor(get_secondary_colors()['text_muted']))
            return None

        elif role == Qt.ItemDataRole.ToolTipRole:
            if col == self.COL_PACKAGE:
                return pkg.nevra
            elif col == self.COL_STATE:
                return self._state_tooltip(pkg)
            elif col == self.COL_VERSION:
                if pkg.has_update:
                    return f"Mise à jour : {pkg.installed_version} → {pkg.version}-{pkg.release}"
                if not pkg.is_latest:
                    return "Ancienne version — une version plus récente est disponible"
            return None

        elif role == Qt.ItemDataRole.UserRole:
            return pkg

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags

        item = self._rows[index.row()] if 0 <= index.row() < len(self._rows) else None

        if isinstance(item, SectionHeader):
            flags = Qt.ItemFlag.ItemIsEnabled
            if item.checkable and index.column() == self.COL_CHECKBOX:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            return flags

        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == self.COL_CHECKBOX:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid():
            return False

        if role == Qt.ItemDataRole.CheckStateRole and index.column() == self.COL_CHECKBOX:
            row = index.row()
            item = self._rows[row] if 0 <= row < len(self._rows) else None
            # Accept both Qt.CheckState enum and raw int (PySide6 compatibility)
            value_int = getattr(value, 'value', value)
            is_checked = (value_int == 2)  # 2 == Qt.CheckState.Checked

            if isinstance(item, PackageDisplayInfo):
                item.selected = is_checked
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
                return True

            if isinstance(item, SectionHeader) and item.checkable:
                item.checked = is_checked
                self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
                return True

        return False

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if 0 <= section < len(self.COLUMNS):
                return self.COLUMNS[section]
        return None

    # -----------------------------------------------------------------------
    # Data management
    # -----------------------------------------------------------------------

    def set_sections(
        self,
        sections: List[tuple],
    ) -> None:
        """Populate the table from a list of named sections.

        Args:
            sections: List of ``(title, packages)`` tuples where *title* is
                the section header text and *packages* is a list of
                :class:`PackageDisplayInfo`.  An empty title skips the header.
                Example::

                    [
                        ("══ Mises à jour (3) ══", [...]),
                        ("══ Installés (1247) ══", [...]),
                    ]
        """
        self.beginResetModel()
        self._rows = []
        for header, packages in sections:
            if header:
                if isinstance(header, SectionHeader):
                    self._rows.append(header)
                else:
                    self._rows.append(SectionHeader(header))
            self._rows.extend(packages)
        self.endResetModel()

    def get_package(self, row: int) -> Optional[PackageDisplayInfo]:
        """Return the package at *row*, or None for section headers / OOB."""
        item = self._rows[row] if 0 <= row < len(self._rows) else None
        return item if isinstance(item, PackageDisplayInfo) else None

    def get_package_by_name(self, name: str) -> Optional[PackageDisplayInfo]:
        """Return the first package whose name matches, or None."""
        for item in self._rows:
            if isinstance(item, PackageDisplayInfo) and item.name == name:
                return item
        return None

    def sync_section_checks(self) -> None:
        """Update checkable section headers based on their packages' state.

        A section header is checked when **all** its packages are selected,
        and unchecked otherwise.  Called after individual checkbox changes
        so the header stays consistent.

        Uses ``_syncing_sections`` guard to prevent re-entrant signals.
        """
        self._syncing_sections = True
        current_header: Optional[SectionHeader] = None
        header_row = -1
        section_packages: list[PackageDisplayInfo] = []

        for i, item in enumerate(self._rows):
            if isinstance(item, SectionHeader):
                # Flush previous section
                if current_header is not None and current_header.checkable:
                    self._apply_section_check(
                        current_header, header_row, section_packages,
                    )
                current_header = item
                header_row = i
                section_packages = []
            elif isinstance(item, PackageDisplayInfo):
                section_packages.append(item)

        # Flush last section
        if current_header is not None and current_header.checkable:
            self._apply_section_check(
                current_header, header_row, section_packages,
            )
        self._syncing_sections = False

    def _apply_section_check(
        self,
        header: SectionHeader,
        header_row: int,
        packages: list,
    ) -> None:
        """Set header.checked = all packages selected, emit dataChanged."""
        all_selected = bool(packages) and all(p.selected for p in packages)
        if header.checked != all_selected:
            header.checked = all_selected
            idx = self.index(header_row, self.COL_CHECKBOX)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.CheckStateRole])

    # -----------------------------------------------------------------------
    # Display helpers
    # -----------------------------------------------------------------------

    def _version_display(self, pkg: PackageDisplayInfo) -> str:
        """Version cell text — shows the available (new) version.

        The installed→available arrow is shown in the tooltip to keep the
        column compact. Older versions are prefixed with a clock icon.
        """
        if not pkg.is_latest:
            return f"\u231b {pkg.version}"  # ⌛ hourglass for older versions
        return pkg.version

    def _state_icon(self, pkg: PackageDisplayInfo) -> str:
        """Single-char state icon for COL_STATE (used for accessibility/fallback)."""
        return self.STATE_ICONS.get(_pkg_state_key(pkg), '')

    def _state_color(self, pkg: PackageDisplayInfo) -> Optional[QColor]:
        """State color for COL_STATE — adapts to the active light/dark theme."""
        return get_state_colors().get(_pkg_state_key(pkg))

    def _state_tooltip(self, pkg: PackageDisplayInfo) -> str:
        """Tooltip text for COL_STATE."""
        if pkg.has_conflict:
            return f"En conflit avec : {pkg.conflict_with or 'autre paquet'}"
        if pkg.has_update:
            return "Mise à jour disponible"
        if pkg.installed:
            if pkg.install_reason == InstallReason.DEPENDENCY:
                return "Installé comme dépendance"
            elif pkg.install_reason == InstallReason.ORPHAN:
                return "Orphelin (plus nécessaire)"
            return "Installé explicitement"
        return "Non installé"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Background drawing helpers
# ---------------------------------------------------------------------------

# Cached dark-mode flag — avoids calling QApplication.palette() on every cell
# paint (~180 times per repaint).  Invalidated by _refresh_dark_mode_cache()
# which is connected to QApplication.paletteChanged in PackageList.__init__.
_dark_mode_cache: Optional[bool] = None


def _refresh_dark_mode_cache() -> None:
    """Recompute and cache the dark-mode flag (call on palette change)."""
    global _dark_mode_cache
    _dark_mode_cache = QApplication.palette().color(
        QPalette.ColorRole.Window
    ).lightness() < 128


def _is_dark() -> bool:
    """Return True when the application is running in dark mode (cached)."""
    global _dark_mode_cache
    if _dark_mode_cache is None:
        _refresh_dark_mode_cache()
    return _dark_mode_cache


# Pre-computed color tuples — avoids tuple construction per cell
_ACTIVE_COLORS_LIGHT = (_ACTIVE_BG_LIGHT, _ACTIVE_FG_LIGHT)
_ACTIVE_COLORS_DARK  = (_ACTIVE_BG_DARK, _ACTIVE_FG_DARK)
_SEL_COLORS_LIGHT    = (_SEL_BG_LIGHT, _SEL_FG_LIGHT)
_SEL_COLORS_DARK     = (_SEL_BG_DARK, _SEL_FG_DARK)


def _active_colors() -> tuple:
    """(bg, fg) for the active/current row (navigation cursor, no checkbox)."""
    return _ACTIVE_COLORS_DARK if _is_dark() else _ACTIVE_COLORS_LIGHT


def _sel_colors() -> tuple:
    """(bg, fg) for a checkbox-selected row."""
    return _SEL_COLORS_DARK if _is_dark() else _SEL_COLORS_LIGHT


def _is_row_selected(option, index) -> bool:
    """Return True if this row is currently highlighted in the view.

    Reads PackageList.highlighted_row which is updated synchronously on
    currentChanged — reliable across all repaints, including those triggered
    by splitter resizes during event processing.
    """
    if option.widget is None:
        return False
    row = getattr(option.widget, 'highlighted_row', -1)
    return row >= 0 and row == index.row()


def _draw_background(painter, option, index):
    """Fill the cell background and return the foreground QColor to use.

    Returns None when the default palette text colour should be used.
    Draws entirely with painter.fillRect — no reliance on PE_PanelItemViewItem
    or Qt style machinery, which behaves inconsistently across themes/bindings.

    Priority (highest first):
    - Section header row      → blue-grey background, white text
    - Checkbox-selected row   → medium blue background  (_SEL_BG_*)
    - Active/current row      → light blue background   (_ACTIVE_BG_*)
    - Normal row              → no fill, default text colour
    """
    pkg = index.data(Qt.ItemDataRole.UserRole)
    if not isinstance(pkg, PackageDisplayInfo):
        painter.fillRect(option.rect, _SECTION_BG)
        return _SECTION_FG

    if pkg.selected:
        bg, fg = _sel_colors()
        painter.fillRect(option.rect, bg)
        return fg

    if _is_row_selected(option, index):
        bg, fg = _active_colors()
        painter.fillRect(option.rect, bg)
        return fg

    return None   # default text colour


# ---------------------------------------------------------------------------
# Delegates
# ---------------------------------------------------------------------------

class PackageDelegate(QStyledItemDelegate):
    """Delegate for COL_PACKAGE: draws package name (bold) + summary below.

    The row height is set externally via QHeaderView.setDefaultSectionSize()
    to accommodate two lines.

    Font and metrics objects are cached and only rebuilt when the base font
    changes (zoom in/out or style change).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cached_base_font: Optional[QFont] = None
        self._name_font: Optional[QFont] = None
        self._name_metrics: Optional[QFontMetrics] = None
        self._summary_font: Optional[QFont] = None
        self._summary_metrics: Optional[QFontMetrics] = None

    def _ensure_fonts(self, base_font: QFont) -> None:
        """Rebuild cached fonts if the base font changed."""
        if self._cached_base_font is not None and self._cached_base_font == base_font:
            return
        self._cached_base_font = QFont(base_font)
        self._name_font = QFont(base_font)
        self._name_font.setBold(True)
        self._name_metrics = QFontMetrics(self._name_font)
        self._summary_font = QFont(base_font)
        self._summary_metrics = QFontMetrics(self._summary_font)

    def paint(self, painter, option, index):
        pkg = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(pkg, PackageDisplayInfo):
            super().paint(painter, option, index)
            return

        self.initStyleOption(option, index)
        self._ensure_fonts(option.font)

        painter.save()

        fg = _draw_background(painter, option, index)

        rect = option.rect
        padding = 3
        x = rect.x() + padding
        w = rect.width() - 2 * padding

        # --- Line 1: package name (bold) ---
        painter.setFont(self._name_font)
        painter.setPen(fg if fg is not None else option.palette.text().color())

        name_h = self._name_metrics.height()
        name_y = rect.y() + padding

        painter.drawText(
            x, name_y, w, name_h,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            pkg.name,
        )

        # --- Line 2: summary (normal, dimmed) ---
        summary = pkg.summary
        if summary:
            painter.setFont(self._summary_font)

            if fg is not None:
                painter.setPen(fg)
            else:
                painter.setPen(option.palette.placeholderText().color())

            summary_h = self._summary_metrics.height()
            summary_y = name_y + name_h + 2

            elided = self._summary_metrics.elidedText(
                summary, Qt.TextElideMode.ElideRight, w
            )
            painter.drawText(
                x, summary_y, w, summary_h,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                elided,
            )

        painter.restore()


class StateBadgeDelegate(QStyledItemDelegate):
    """Delegate for COL_STATE: draws a filled rounded badge with a letter.

    Each package state is represented by a color-filled rounded square with
    a single white bold letter centered inside:

    - Installé     → green  ``I``
    - Dépendance   → blue-gray ``D``
    - Orphelin     → violet ``O``
    - Mise à jour  → orange ``U``
    - Conflit      → red    ``C``
    - Disponible   → *(no badge)*

    Colors come from :func:`~rpmdrake.qt.palette.get_state_colors` so they
    automatically switch between light and dark theme.
    """

    _LETTERS: dict[str, str] = {
        'installed': 'I',
        'dep':       'D',
        'orphan':    'O',
        'update':    'U',
        'conflict':  'C',
        'available': '',
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cached_base_font: Optional[QFont] = None
        self._cached_side: int = 0
        self._badge_font: Optional[QFont] = None

    def _ensure_badge_font(self, base_font: QFont, side: int) -> None:
        """Rebuild cached badge font if base font or badge size changed."""
        if (self._cached_base_font is not None
                and self._cached_base_font == base_font
                and self._cached_side == side):
            return
        self._cached_base_font = QFont(base_font)
        self._cached_side = side
        self._badge_font = QFont(base_font)
        self._badge_font.setBold(True)
        self._badge_font.setPixelSize(max(side - 7, 8))

    def paint(self, painter, option, index):
        pkg = index.data(Qt.ItemDataRole.UserRole)
        self.initStyleOption(option, index)
        _draw_background(painter, option, index)

        if not isinstance(pkg, PackageDisplayInfo):
            return

        state_key = _pkg_state_key(pkg)
        letter = self._LETTERS.get(state_key, '')
        color = get_state_colors().get(state_key)

        if not letter or not color:
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        margin = 3
        side = min(option.rect.height() - 2 * margin, 22)
        cx = option.rect.center().x()
        cy = option.rect.center().y()
        badge_rect = QRect(cx - side // 2, cy - side // 2, side, side)

        painter.setBrush(QBrush(color))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.drawRoundedRect(badge_rect, 4, 4)

        self._ensure_badge_font(option.font, side)
        painter.setFont(self._badge_font)
        painter.setPen(QColor('white'))
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, letter)

        painter.restore()

    def sizeHint(self, option, index):
        base = super().sizeHint(option, index)
        return QSize(30, base.height())


class TextCellDelegate(QStyledItemDelegate):
    """Default delegate for text-only columns (Version, Rév., Arch).

    Draws our custom selection background then the cell text — bypasses
    PE_PanelItemViewItem so the system selection color never overwrites ours.
    """

    def paint(self, painter, option, index):
        self.initStyleOption(option, index)
        painter.save()
        fg = _draw_background(painter, option, index)

        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text:
            painter.setFont(option.font)
            painter.setPen(fg if fg is not None else option.palette.text().color())
            painter.drawText(
                option.rect.adjusted(4, 0, -4, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                text,
            )

        painter.restore()


class CheckboxDelegate(QStyledItemDelegate):
    """Delegate for COL_CHECKBOX: draws a centred checkbox."""

    def paint(self, painter, option, index):
        self.initStyleOption(option, index)
        _draw_background(painter, option, index)   # return value unused

        # Only draw checkbox for package rows and checkable section headers
        check_state = index.data(Qt.ItemDataRole.CheckStateRole)
        if check_state is None:
            return

        checked = (
            check_state == Qt.CheckState.Checked
            or getattr(check_state, 'value', check_state) == 2
            or check_state == 2
        )

        # Draw the checkbox manually so it works correctly in both
        # light and dark themes (Fusion style ignores the option palette
        # for checkbox indicators, giving black-on-black in dark mode).
        pal = QApplication.palette()
        size = 14
        x = option.rect.x() + (option.rect.width() - size) // 2
        y = option.rect.y() + (option.rect.height() - size) // 2
        box = QRect(x, y, size, size)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Box background + border
        border_color = pal.color(QPalette.ColorRole.Text)
        bg_color = pal.color(QPalette.ColorRole.Base)
        painter.setPen(QPen(border_color, 1.5))
        painter.setBrush(QBrush(bg_color))
        painter.drawRoundedRect(box, 2, 2)

        # Checkmark
        if checked:
            accent = pal.color(QPalette.ColorRole.Highlight)
            painter.setBrush(QBrush(accent))
            painter.setPen(QPen(accent, 1.5))
            painter.drawRoundedRect(box, 2, 2)
            # White checkmark
            painter.setPen(QPen(QColor("#ffffff"), 2.0))
            painter.drawLine(x + 3, y + size // 2, x + size // 2 - 1, y + size - 4)
            painter.drawLine(x + size // 2 - 1, y + size - 4, x + size - 3, y + 3)

        painter.restore()

    def editorEvent(self, event, model, option, index):
        # Only handle rows that have a checkbox (packages + checkable headers)
        check_state = index.data(Qt.ItemDataRole.CheckStateRole)
        if check_state is None:
            return False

        event_type = getattr(event.type(), 'value', event.type())
        if event_type == 3:  # MouseButtonRelease
            is_checked = (
                check_state == Qt.CheckState.Checked
                or getattr(check_state, 'value', check_state) == 2
                or check_state == 2
            )
            new_value = Qt.CheckState.Unchecked if is_checked else Qt.CheckState.Checked
            model.setData(index, new_value, Qt.ItemDataRole.CheckStateRole)
            return True
        return super().editorEvent(event, model, option, index)


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

class PackageList(QTableView):
    """Table view for the package list.

    Features
    --------
    - Section header rows (non-interactive visual separators)
    - Custom two-line delegate for the package name column
    - Checkbox column at the rightmost position
    - Virtual scrolling for performance
    """

    selection_changed = Signal(str, bool)    # package nevra, selected
    section_check_toggled = Signal(str, bool)  # section title, checked
    package_activated = Signal(str)          # package_name (double-click)

    # Row index of the currently highlighted row, -1 if none.
    # Updated on currentChanged and stored here so delegates can read it
    # during repaint without relying on currentIndex() which may be stale
    # when a second repaint is triggered during event processing.
    highlighted_row: int = -1

    def __init__(self, parent=None):
        super().__init__(parent)

        self._model = PackageTableModel(self)
        self.setModel(self._model)

        # Column delegates
        self.setItemDelegateForColumn(
            PackageTableModel.COL_PACKAGE, PackageDelegate(self)
        )
        text_delegate = TextCellDelegate(self)
        self.setItemDelegateForColumn(PackageTableModel.COL_VERSION, text_delegate)
        self.setItemDelegateForColumn(PackageTableModel.COL_RELEASE, text_delegate)
        self.setItemDelegateForColumn(PackageTableModel.COL_ARCH, text_delegate)
        self.setItemDelegateForColumn(
            PackageTableModel.COL_STATE, StateBadgeDelegate(self)
        )
        self.setItemDelegateForColumn(
            PackageTableModel.COL_CHECKBOX, CheckboxDelegate(self)
        )

        # View settings
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(False)   # Section BG colours replace alternating
        self.setSortingEnabled(False)          # Sections impose a logical order
        self.setShowGrid(False)
        self.setWordWrap(False)

        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(40)

        # Column sizing
        header = self.horizontalHeader()
        header.setSectionResizeMode(
            PackageTableModel.COL_PACKAGE, QHeaderView.ResizeMode.Stretch
        )
        header.setSectionResizeMode(
            PackageTableModel.COL_VERSION, QHeaderView.ResizeMode.Interactive
        )
        header.setSectionResizeMode(
            PackageTableModel.COL_RELEASE, QHeaderView.ResizeMode.Interactive
        )
        header.setSectionResizeMode(
            PackageTableModel.COL_ARCH, QHeaderView.ResizeMode.Fixed
        )
        header.setSectionResizeMode(
            PackageTableModel.COL_STATE, QHeaderView.ResizeMode.Fixed
        )
        header.setSectionResizeMode(
            PackageTableModel.COL_CHECKBOX, QHeaderView.ResizeMode.Fixed
        )
        header.setStretchLastSection(False)

        # Signals
        self.doubleClicked.connect(self._on_double_click)
        self._model.dataChanged.connect(self._on_data_changed)
        self.selectionModel().currentChanged.connect(self._on_current_changed)

        # Invalidate the dark-mode cache when the system theme changes
        QApplication.instance().paletteChanged.connect(_refresh_dark_mode_cache)

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def set_sections(
        self,
        sections: List[tuple],
    ) -> None:
        """Populate the list from named sections.

        Args:
            sections: List of ``(title, packages)`` tuples — see
                :meth:`PackageTableModel.set_sections`.
        """
        self._model.set_sections(sections)

    def set_packages(self, packages: List[PackageDisplayInfo]) -> None:
        """Compatibility wrapper: display a flat package list as one unnamed section.

        Use :meth:`set_sections` when the full sectioned layout is available.

        Args:
            packages: Flat list of packages to display.
        """
        self._model.set_sections([("", packages)])

    def update_row_height(self, font_size: int) -> None:
        """Adjust row height and column widths for the given font size.

        Args:
            font_size: Current application font size in points.
        """
        font = QFont()
        font.setPointSize(font_size)
        metrics = QFontMetrics(font)

        line_h = metrics.height()
        row_h = line_h * 2 + 10   # Two lines + padding

        self.verticalHeader().setDefaultSectionSize(row_h)

        char_w = metrics.horizontalAdvance("M")
        self.setColumnWidth(PackageTableModel.COL_VERSION,  char_w * 14)
        self.setColumnWidth(PackageTableModel.COL_RELEASE,  char_w * 8)
        self.setColumnWidth(PackageTableModel.COL_ARCH,     char_w * 6)
        self.setColumnWidth(PackageTableModel.COL_STATE,    char_w * 3 + 8)
        self.setColumnWidth(PackageTableModel.COL_CHECKBOX, char_w * 2 + 10)

    # -----------------------------------------------------------------------
    # Key events
    # -----------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        """Space toggles the checkbox for the current row."""
        key = event.key()
        is_space = (
            key == 32
            or key == Qt.Key.Key_Space
            or (hasattr(key, 'value') and key.value == 32)
        )
        if is_space:
            index = self.currentIndex()
            if index.isValid():
                checkbox_idx = self._model.index(index.row(), PackageTableModel.COL_CHECKBOX)
                current = checkbox_idx.data(Qt.ItemDataRole.CheckStateRole)
                is_checked = (
                    current == Qt.CheckState.Checked
                    or getattr(current, 'value', current) == 2
                    or current == 2
                )
                new_value = Qt.CheckState.Unchecked if is_checked else Qt.CheckState.Checked
                self._model.setData(checkbox_idx, new_value, Qt.ItemDataRole.CheckStateRole)
                return
        super().keyPressEvent(event)

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _on_current_changed(self, current: QModelIndex, previous: QModelIndex) -> None:
        """Cache the highlighted row index so delegates can read it during repaint.

        Only repaints the two affected rows (previous + current) instead of
        the entire viewport, which avoids repainting hundreds of cells on
        every arrow-key press.
        """
        self.highlighted_row = current.row() if current.isValid() else -1
        # Repaint only the rows that changed highlight state
        if previous.isValid():
            self._repaint_row(previous.row())
        if current.isValid():
            self._repaint_row(current.row())

    def _repaint_row(self, row: int) -> None:
        """Repaint a single row by its row number."""
        first_col = self._model.index(row, 0)
        last_col = self._model.index(row, self._model.columnCount() - 1)
        rect = self.visualRect(first_col) | self.visualRect(last_col)
        if not rect.isEmpty():
            self.viewport().update(rect)

    def _on_double_click(self, index: QModelIndex) -> None:
        pkg = self._model.get_package(index.row())
        if pkg:
            self.package_activated.emit(pkg.name)

    def _on_data_changed(
        self,
        top_left: QModelIndex,
        bottom_right: QModelIndex,
        roles: List[int],
    ) -> None:
        # Avoid the enum-vs-int comparison pitfall in PySide6: our model only
        # calls setData for checkboxes, so any dataChanged means a checkbox changed.
        for row in range(top_left.row(), bottom_right.row() + 1):
            item = self._model._rows[row] if 0 <= row < len(self._model._rows) else None
            if isinstance(item, PackageDisplayInfo):
                self.selection_changed.emit(item.nevra, item.selected)
            elif isinstance(item, SectionHeader) and item.checkable:
                if not self._model._syncing_sections:
                    self.section_check_toggled.emit(item.title, item.checked)
