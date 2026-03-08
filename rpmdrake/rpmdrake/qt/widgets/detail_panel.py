"""Package detail panel for rpmdrake-ng.

Displayed in the right-hand QStackedWidget (page 1) when the user
selects a row in the package list.  A "← Catégories" button returns
to the category tree (page 0).
"""

from ..compat import (
    Qt,
    Signal,
    QObject,
    QEvent,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QFont,
    QTextBrowser,
    QScrollArea,
)
from .collapsible_group import CollapsibleGroup
from ..palette import get_state_colors

__all__ = ["PackageDetailPanel"]

# State labels for the badge
_STATE_LABELS = {
    'update':    ('↑ Mise à jour',  'update'),
    'installed': ('Ⓘ Installé',    'installed'),
    'dep':       ('Ⓓ Dépendance',  'dep'),
    'orphan':    ('Ⓞ Orphelin',    'orphan'),
    'conflict':  ('⚠ Conflit',     'conflict'),
}


class _WheelZoomFilter(QObject):
    """Event filter that intercepts Ctrl+wheel and emits zoom_requested.

    Installed on the QTextBrowser so that Ctrl+scroll over the description
    triggers the application-wide zoom instead of the browser's own zoom.
    """

    zoom_requested = Signal(int)   # +1 for zoom-in, -1 for zoom-out

    def eventFilter(self, obj, event) -> bool:
        # QEvent.Type.Wheel == 31 in Qt5 and Qt6
        etype = event.type()
        if getattr(etype, 'value', etype) == QEvent.Type.Wheel.value:
            mods = event.modifiers()
            ctrl = Qt.KeyboardModifier.ControlModifier
            if getattr(mods, 'value', mods) & getattr(ctrl, 'value', ctrl):
                delta = event.angleDelta().y()
                self.zoom_requested.emit(1 if delta > 0 else -1)
                return True   # event consumed — do not scroll the text browser
        return False


class PackageDetailPanel(QWidget):
    """Scrollable detail view for a single package.

    The description is the primary content (max vertical space).
    Dependencies, provides and files are collapsed by default —
    most users never need them.  Metadata (URL, licence, size, packager)
    appears at the end of the scrollable area.

    Signals
    -------
    back_clicked : emitted when the user clicks "← Catégories".
    zoom_requested : emitted with +1 / -1 when Ctrl+wheel is used over
        the description text (allows MainWindow to apply the app-wide zoom).
    """

    back_clicked   = Signal()
    zoom_requested = Signal(int)   # +1 zoom-in, -1 zoom-out

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()
        self._show_placeholder()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_font_size(self, pt: int) -> None:
        """Update all widget fonts to *pt* point size.

        Must be called by :class:`~rpmdrake.qt.main.MainWindow` whenever the
        application zoom level changes.  Widgets whose font is set via a
        stylesheet ``font-family:`` rule do **not** inherit the application
        font change, so they must be updated explicitly here.

        Args:
            pt: Target font point size (same as the application font).
        """
        # Package name: larger and bold
        name_font = QFont()
        name_font.setPointSize(pt + 2)
        name_font.setBold(True)
        self._lbl_name.setFont(name_font)

        # EVRA / installed-version arrow: monospace at application size
        mono_font = QFont("monospace")
        mono_font.setPointSize(pt)
        self._lbl_evra.setFont(mono_font)
        self._lbl_inst_version.setFont(mono_font)

        # Collapsible content (deps / provides / files): monospace
        self._deps_content.setFont(mono_font)
        self._prov_content.setFont(mono_font)
        self._files_content.setFont(mono_font)

        # Description QTextBrowser: standard font (QTextBrowser does not
        # inherit QApplication font changes automatically)
        desc_font = QFont()
        desc_font.setPointSize(pt)
        self._description.setFont(desc_font)

    def show_package(self, details: dict) -> None:
        """Populate the panel with package details.

        Args:
            details: Dict returned by
                :meth:`~rpmdrake.common.controller.Controller.get_package_details`.
        """
        name         = details.get('name', '')
        version      = details.get('version', '')
        release      = details.get('release', '')
        arch         = details.get('arch', '')
        summary      = details.get('summary', '') or ''
        desc         = details.get('description', '') or ''
        url          = details.get('url', '') or ''
        license_     = details.get('license', '') or ''
        group        = details.get('group', '') or ''
        size         = details.get('size', 0) or 0
        packager     = details.get('packager', '') or ''
        installed    = details.get('installed', False)
        has_update   = details.get('has_update', False)
        inst_version = details.get('installed_version') or ''
        inst_reason  = details.get('install_reason')
        requires     = details.get('requires', []) or []
        provides     = details.get('provides', []) or []
        files        = details.get('files', []) or []

        # --- Name (prominent) ---
        self._lbl_name.setText(name)

        # --- EVRA ---
        evra_parts = [v for v in (version, release, arch) if v]
        self._lbl_evra.setText("  ".join(evra_parts))

        # --- Installed version (for updates) ---
        if has_update and inst_version:
            self._lbl_inst_version.setText(f"Installé : {inst_version}  →  {version}")
            self._lbl_inst_version.show()
        else:
            self._lbl_inst_version.hide()

        # --- State badge ---
        state_key = self._state_key(installed, has_update, inst_reason)
        label_text, palette_key = _STATE_LABELS.get(state_key, ('Disponible', None))
        self._lbl_state.setText(label_text)
        if palette_key:
            colors = get_state_colors()
            color = colors.get(palette_key)
            hex_col = color.name() if color else '#888888'
            self._lbl_state.setStyleSheet(
                f"color: white; background-color: {hex_col};"
                " border-radius: 3px; padding: 2px 8px; font-weight: bold;"
            )
        else:
            self._lbl_state.setStyleSheet(
                "color: palette(text); border: 1px solid palette(mid);"
                " border-radius: 3px; padding: 2px 8px;"
            )

        # --- Group ---
        self._lbl_group.setText(group)
        self._lbl_group.setVisible(bool(group))

        # --- Description ---
        html = ""
        if summary:
            html += f"<p><i>{summary}</i></p>"
        if desc:
            html += "<p>" + desc.replace('\n\n', '</p><p>').replace('\n', ' ') + "</p>"
        self._description.setHtml(html or "<p><i>(pas de description)</i></p>")

        # --- Collapsible sections ---
        self._deps_content.setText("\n".join(requires[:100]) or "—")
        self._prov_content.setText("\n".join(provides[:100]) or "—")

        shown = files[:200]
        extra = len(files) - len(shown)
        files_text = "\n".join(shown)
        if extra > 0:
            files_text += f"\n… {extra} fichier(s) de plus"
        self._files_content.setText(files_text or "—")

        # --- Metadata (end of scrollable area) ---
        meta_parts = []
        if url:
            meta_parts.append(f'<a href="{url}">{url}</a>')
        if license_:
            meta_parts.append(f"Licence : {license_}")
        if size > 0:
            if size >= 1024 * 1024:
                meta_parts.append(f"Taille : {size / 1024 / 1024:.1f} Mo")
            else:
                meta_parts.append(f"Taille : {size // 1024} Ko")
        if packager:
            meta_parts.append(f"Packager : {packager}")

        if meta_parts:
            self._lbl_meta.setText("<br/>".join(meta_parts))
            self._lbl_meta.show()
        else:
            self._lbl_meta.hide()

        self._content_widget.show()
        self._placeholder.hide()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _state_key(installed: bool, has_update: bool, install_reason) -> str:
        if has_update:
            return 'update'
        if installed:
            if install_reason == 'dep':
                return 'dep'
            if install_reason == 'orphan':
                return 'orphan'
            return 'installed'
        return 'available'

    def _show_placeholder(self) -> None:
        self._content_widget.hide()
        self._placeholder.show()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- Back button (always visible, above scroll area) ---
        self._btn_back = QPushButton("← Catégories")
        self._btn_back.setFlat(True)
        self._btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_back.setStyleSheet(
            "QPushButton { text-align: left; color: palette(link);"
            " padding: 6px 8px; border: none; }"
            "QPushButton:hover { text-decoration: underline; }"
        )
        self._btn_back.clicked.connect(self.back_clicked)
        root.addWidget(self._btn_back)

        sep_top = QFrame()
        sep_top.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep_top)

        # --- Placeholder (nothing selected yet) ---
        self._placeholder = QLabel("Sélectionnez un paquet pour afficher ses détails.")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setStyleSheet("color: palette(mid); padding: 20px;")
        root.addWidget(self._placeholder, stretch=1)

        # --- Scrollable content ---
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content_widget = QWidget()
        # Ensure the content area uses the window base colour, not the
        # potentially-grey mid-tone that some Qt styles set on plain QWidgets.
        self._content_widget.setStyleSheet("QWidget { background: palette(base); }")
        content = QVBoxLayout(self._content_widget)
        content.setContentsMargins(10, 10, 10, 10)
        content.setSpacing(8)

        # Name (large, bold) — font set programmatically via update_font_size()
        self._lbl_name = QLabel()
        self._lbl_name.setWordWrap(True)
        content.addWidget(self._lbl_name)

        # EVRA (monospace) — font set programmatically via update_font_size()
        self._lbl_evra = QLabel()
        content.addWidget(self._lbl_evra)

        # Installed version arrow (only for updates)
        self._lbl_inst_version = QLabel()
        self._lbl_inst_version.hide()
        content.addWidget(self._lbl_inst_version)

        # State badge + group (one row)
        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.setSpacing(10)
        self._lbl_state = QLabel()
        self._lbl_group = QLabel()
        badge_row.addWidget(self._lbl_state)
        badge_row.addWidget(self._lbl_group)
        badge_row.addStretch()
        content.addLayout(badge_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        content.addWidget(sep)

        # Description (primary content — takes all available space)
        self._description = QTextBrowser()
        self._description.setOpenExternalLinks(True)
        self._description.setFrameShape(QFrame.Shape.NoFrame)
        self._description.setStyleSheet("background: transparent;")
        self._description.setMinimumHeight(120)
        content.addWidget(self._description, stretch=1)

        # Install event filter to forward Ctrl+wheel → app zoom
        self._zoom_filter = _WheelZoomFilter(self)
        self._zoom_filter.zoom_requested.connect(self.zoom_requested)
        self._description.installEventFilter(self._zoom_filter)

        # Collapsible sections (all collapsed by default)
        # Font (monospace) set programmatically via update_font_size()
        self._deps_group = CollapsibleGroup("Dépendances")
        self._deps_content = self._make_mono_label()
        self._deps_group.addWidget(self._deps_content)
        self._deps_group.setExpanded(False)
        content.addWidget(self._deps_group)

        self._prov_group = CollapsibleGroup("Fournit")
        self._prov_content = self._make_mono_label()
        self._prov_group.addWidget(self._prov_content)
        self._prov_group.setExpanded(False)
        content.addWidget(self._prov_group)

        self._files_group = CollapsibleGroup("Fichiers")
        self._files_content = self._make_mono_label()
        self._files_group.addWidget(self._files_content)
        self._files_group.setExpanded(False)
        content.addWidget(self._files_group)

        # Metadata footer (end of scrollable area — after collapsibles)
        sep_meta = QFrame()
        sep_meta.setFrameShape(QFrame.Shape.HLine)
        content.addWidget(sep_meta)

        self._lbl_meta = QLabel()
        self._lbl_meta.setWordWrap(True)
        self._lbl_meta.setOpenExternalLinks(True)
        self._lbl_meta.hide()
        content.addWidget(self._lbl_meta)

        content.addStretch()

        scroll.setWidget(self._content_widget)
        root.addWidget(scroll, stretch=1)

    @staticmethod
    def _make_mono_label() -> QLabel:
        """Create a selectable, word-wrapping label for monospaced content.

        Font family and size are set later by :meth:`update_font_size`.
        """
        lbl = QLabel()
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return lbl
