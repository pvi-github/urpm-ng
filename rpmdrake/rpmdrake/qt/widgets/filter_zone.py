"""Filter zone widget for rpmdrake-ng.

Compact horizontal bar of state and display filters, shown/hidden
by the ⊟ toggle button in the top bar of the main window.
"""

from typing import TYPE_CHECKING

from ..compat import (
    Qt,
    Signal,
    QWidget,
    QHBoxLayout,
    QLabel,
    QCheckBox,
    QFrame,
)

if TYPE_CHECKING:
    from ...common.controller import Controller

from ...common.models import PackageState

__all__ = ["FilterZone"]


class FilterZone(QWidget):
    """Horizontal compact filter bar for package state and display filters.

    Layout::

        État: [☑ Màj] [☑ Installés] [☐ Disponibles]  |
        Afficher: [☐ libs] [☐ devel] [☐ debug] [☐ 32bit] [☐ task-*]

    Hidden by default — the main window toggles it with the ⊟ button.
    """

    filter_changed = Signal()

    def __init__(self, controller: 'Controller', parent=None):
        super().__init__(parent)
        self.controller = controller
        self._setup_ui()
        self._connect_signals()
        self._update_from_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _update_from_state(self) -> None:
        """Synchronise checkboxes with the controller's FilterState.

        Called when the controller changes the filter programmatically
        (e.g. after load_initial() clears the upgrade filter).
        """
        fs = self.controller.filter_state

        all_boxes = [
            self.chk_upgrades, self.chk_installed, self.chk_available,
            self.chk_libs, self.chk_devel, self.chk_debug,
            self.chk_i586, self.chk_tasks,
        ]
        for box in all_boxes:
            box.blockSignals(True)

        self.chk_upgrades.setChecked(PackageState.UPGRADES in fs.states)
        self.chk_installed.setChecked(PackageState.INSTALLED in fs.states)
        self.chk_available.setChecked(PackageState.AVAILABLE in fs.states)
        self.chk_libs.setChecked(fs.show_libs)
        self.chk_devel.setChecked(fs.show_devel)
        self.chk_debug.setChecked(fs.show_debug)
        self.chk_i586.setChecked(fs.show_i586)
        self.chk_tasks.setChecked(fs.show_tasks)

        for box in all_boxes:
            box.blockSignals(False)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        # State filters
        layout.addWidget(QLabel("État :"))
        self.chk_upgrades  = QCheckBox("Màj")
        self.chk_installed = QCheckBox("Installés")
        self.chk_available = QCheckBox("Disponibles")
        self.chk_available.setToolTip("Recherchez un terme ou sélectionnez une catégorie")
        layout.addWidget(self.chk_upgrades)
        layout.addWidget(self.chk_installed)
        layout.addWidget(self.chk_available)

        # Visual separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("color: palette(mid);")
        layout.addWidget(sep)

        # Display filters
        layout.addWidget(QLabel("Afficher :"))
        self.chk_libs  = QCheckBox("libs")
        self.chk_devel = QCheckBox("devel")
        self.chk_debug = QCheckBox("debug")
        self.chk_i586  = QCheckBox("32bit")
        self.chk_tasks = QCheckBox("task-*")
        for box in (self.chk_libs, self.chk_devel, self.chk_debug,
                    self.chk_i586, self.chk_tasks):
            layout.addWidget(box)

        layout.addStretch()

    def _connect_signals(self) -> None:
        self.chk_upgrades.toggled.connect(
            lambda checked: self._on_state_toggled(PackageState.UPGRADES, checked)
        )
        self.chk_installed.toggled.connect(
            lambda checked: self._on_state_toggled(PackageState.INSTALLED, checked)
        )
        self.chk_available.toggled.connect(
            lambda checked: self._on_state_toggled(PackageState.AVAILABLE, checked)
        )
        self.chk_libs.toggled.connect(
            lambda checked: self.controller.set_display_filter('libs', checked)
        )
        self.chk_devel.toggled.connect(
            lambda checked: self.controller.set_display_filter('devel', checked)
        )
        self.chk_debug.toggled.connect(
            lambda checked: self.controller.set_display_filter('debug', checked)
        )
        self.chk_i586.toggled.connect(
            lambda checked: self.controller.set_display_filter('i586', checked)
        )
        self.chk_tasks.toggled.connect(
            lambda checked: self.controller.set_display_filter('tasks', checked)
        )

    def _on_state_toggled(self, state: PackageState, checked: bool) -> None:
        fs = self.controller.filter_state
        if checked:
            fs.states.add(state)
        else:
            fs.states.discard(state)
        self.controller._invalidate_cache()
        self.controller._refresh_packages()
        self.filter_changed.emit()
