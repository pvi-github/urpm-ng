"""Qt implementation of ViewInterface."""

from typing import TYPE_CHECKING, List

from .compat import (
    QObject, Signal, Slot, QMessageBox,
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QDialog, Qt, QFrame,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QScrollArea, QWidget,
)

from ..common.models import PackageDisplayInfo

if TYPE_CHECKING:
    from .main import MainWindow
    from ..common.interfaces import ViewInterface

__all__ = ["QtView"]


class QtView(QObject):  # Implements ViewInterface (no formal inheritance due to metaclass conflict)
    """Qt implementation of ViewInterface.

    Uses Qt signals to ensure thread-safe UI updates from
    background threads.
    """

    # Signals for thread-safe updates
    _update_packages = Signal(list)
    _show_loading = Signal(bool)
    _show_progress = Signal(str, str, int, int, float)
    _show_question = Signal(str, str, str, list)
    _show_complete = Signal(bool, dict)
    _show_error = Signal(str, str)
    _show_confirm = Signal(str, str, dict)
    _filter_changed = Signal()

    def __init__(self, window: 'MainWindow'):
        super().__init__()
        self.window = window

        # Connect signals to slots
        self._update_packages.connect(self._do_update_packages)
        self._show_loading.connect(self._do_show_loading)
        self._show_progress.connect(self._do_show_progress)
        self._show_question.connect(self._do_show_question)
        self._show_complete.connect(self._do_show_complete)
        self._show_error.connect(self._do_show_error)
        self._show_confirm.connect(self._do_show_confirm)
        self._filter_changed.connect(self._do_filter_changed)

    # =========================================================================
    # ViewInterface implementation (called from any thread)
    # =========================================================================

    def on_package_list_update(self, packages: List[PackageDisplayInfo]) -> None:
        """Update the package list display."""
        self._update_packages.emit(packages)

    def show_loading(self, loading: bool) -> None:
        """Show or hide loading indicator."""
        self._show_loading.emit(loading)

    def on_progress(
        self,
        phase: str,
        name: str,
        current: int,
        total: int,
        speed: float = 0.0
    ) -> None:
        """Update progress display."""
        self._show_progress.emit(phase, name, current, total, speed)

    def on_question(
        self,
        question_id: str,
        qtype: str,
        message: str,
        choices: List[str]
    ) -> None:
        """Display a question dialog."""
        self._show_question.emit(question_id, qtype, message, choices)

    def on_transaction_complete(self, success: bool, summary: dict) -> None:
        """Handle transaction completion."""
        self._show_complete.emit(success, summary)

    def show_error(self, title: str, message: str) -> None:
        """Display an error message."""
        self._show_error.emit(title, message)

    def show_confirmation(self, title: str, message: str, details: dict) -> None:
        """Display transaction confirmation dialog."""
        self._show_confirm.emit(title, message, details)

    def on_filter_state_changed(self) -> None:
        """Update filter panel when state changes programmatically."""
        self._filter_changed.emit()

    def show_action_confirmation(self, action: str, packages: List[str]) -> bool:
        """Show confirmation dialog before executing an action."""
        pkg_count = len(packages)
        pkg_list = "\n".join(f"  - {p}" for p in packages[:10])
        if pkg_count > 10:
            pkg_list += f"\n  ... et {pkg_count - 10} autres"

        result = QMessageBox.question(
            self.window,
            f"Confirmer: {action}",
            f"{action} {pkg_count} paquet(s) ?\n\n{pkg_list}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        return result == QMessageBox.StandardButton.Yes

    def show_alternative_choice(
        self,
        capability: str,
        required_by: str,
        providers: List[str]
    ) -> str:
        """Show dialog for choosing between alternative providers.

        Args:
            capability: The capability being satisfied.
            required_by: Package that requires this capability.
            providers: List of package names that can provide it.

        Returns:
            Chosen package name, or empty string if cancelled.
        """
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Choix requis")
        dialog.setMinimumWidth(400)
        dialog.setMinimumHeight(300)

        layout = QVBoxLayout(dialog)

        # Header
        header = QLabel(f"<b>{capability}</b> (requis par {required_by})")
        header.setWordWrap(True)
        layout.addWidget(header)

        # Search filter
        filter_edit = QLineEdit()
        filter_edit.setPlaceholderText("Filtrer (ex: fr, french)...")
        layout.addWidget(filter_edit)

        # Provider list
        list_widget = QListWidget()
        for provider in sorted(providers):
            item = QListWidgetItem(provider)
            list_widget.addItem(item)
        layout.addWidget(list_widget, 1)

        # Filter function
        def filter_list(text):
            text = text.lower()
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                item.setHidden(text not in item.text().lower())

        filter_edit.textChanged.connect(filter_list)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_cancel = QPushButton("Annuler")
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #757575;
                color: white;
                font-weight: bold;
                padding: 6px 16px;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #616161; }
            QPushButton:pressed { background-color: #424242; }
        """)

        btn_ok = QPushButton("Choisir")
        btn_ok.setEnabled(False)
        btn_ok.setStyleSheet("""
            QPushButton {
                background-color: #2196f3;
                color: white;
                font-weight: bold;
                padding: 6px 16px;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #1976d2; }
            QPushButton:pressed { background-color: #1565c0; }
            QPushButton:disabled { background-color: #90caf9; }
        """)

        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_ok)
        layout.addLayout(btn_layout)

        # Enable OK button when selection made
        def on_selection_changed():
            btn_ok.setEnabled(len(list_widget.selectedItems()) > 0)

        list_widget.itemSelectionChanged.connect(on_selection_changed)
        list_widget.itemDoubleClicked.connect(lambda: dialog.accept())

        btn_ok.clicked.connect(dialog.accept)
        btn_cancel.clicked.connect(dialog.reject)

        # Show dialog
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = list_widget.selectedItems()
            if selected:
                return selected[0].text()
        return ""

    def show_transaction_confirmation(self, action: str, summary: dict) -> bool:
        """Show detailed transaction confirmation dialog."""
        action_labels = {
            'install': 'Installation',
            'erase': 'Suppression',
            'upgrade': 'Mise à jour'
        }
        action_icons = {
            'install': '📥',
            'erase': '🗑',
            'upgrade': '⬆'
        }

        title = action_labels.get(action, action)
        icon = action_icons.get(action, '📦')

        # Create dialog
        dialog = QDialog(self.window)
        dialog.setWindowTitle(f"Confirmer: {title}")
        dialog.setMinimumWidth(500)
        dialog.setMinimumHeight(400)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(12)

        # Header with summary
        requested = summary.get('requested', [])
        install_deps = summary.get('install_deps', [])
        upgrades = summary.get('upgrade', [])
        remove = summary.get('remove', [])
        remove_deps = summary.get('remove_deps', [])
        orphans = summary.get('orphans_created', [])

        total_install = len(requested) + len(install_deps)
        total_upgrade = len(upgrades)
        total_remove = len(remove) + len(remove_deps)

        header_parts = [f"<b>{icon} {title}</b>"]
        summary_parts = []
        if total_install:
            summary_parts.append(f"+{total_install} à installer")
        if total_upgrade:
            summary_parts.append(f"↑{total_upgrade} à mettre à jour")
        if total_remove:
            summary_parts.append(f"−{total_remove} à supprimer")

        header_label = QLabel(header_parts[0] + "<br/>" + " &nbsp;•&nbsp; ".join(summary_parts))
        header_label.setStyleSheet("font-size: 12pt; padding: 8px;")
        layout.addWidget(header_label)

        # Separator
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("color: palette(mid);")
        layout.addWidget(separator)

        # Scrollable content area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(8)
        content_layout.setContentsMargins(0, 0, 8, 0)

        def add_section(title: str, packages: List[str], symbol: str):
            """Add a section with package list."""
            if not packages:
                return

            # Section header
            section = QWidget()
            section_layout = QVBoxLayout(section)
            section_layout.setContentsMargins(0, 0, 0, 0)
            section_layout.setSpacing(2)

            header = QLabel(f"<b>{symbol} {title} ({len(packages)})</b>")
            section_layout.addWidget(header)

            # Package list
            pkg_list = QListWidget()
            pkg_list.setFrameShape(QFrame.Shape.NoFrame)
            pkg_list.setStyleSheet("""
                QListWidget {
                    background: transparent;
                    border: none;
                }
                QListWidget::item {
                    padding: 2px 4px;
                }
            """)
            pkg_list.setMaximumHeight(min(len(packages) * 22 + 4, 150))

            for pkg in sorted(packages):
                pkg_list.addItem(f"  {pkg}")

            section_layout.addWidget(pkg_list)
            content_layout.addWidget(section)

        # Add sections
        add_section("Paquets demandés", requested, "●")
        add_section("Dépendances à installer", install_deps, "+")
        add_section("Mises à jour", upgrades, "↑")
        add_section("Paquets à supprimer", remove, "−")

        if remove_deps:
            add_section("⚠ Dépendances inverses supprimées", remove_deps, "−")

        if orphans:
            add_section("Deviendront orphelins", orphans, "?")

        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_cancel = QPushButton("Annuler")
        btn_cancel.setMinimumWidth(100)
        btn_cancel.setStyleSheet("""
            QPushButton {
                background-color: #757575;
                color: white;
                font-weight: bold;
                padding: 6px 16px;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #616161; }
            QPushButton:pressed { background-color: #424242; }
        """)

        btn_confirm = QPushButton(f"Confirmer {title}")
        btn_confirm.setMinimumWidth(140)
        btn_confirm.setStyleSheet("""
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
        """)

        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_confirm)
        layout.addLayout(btn_layout)

        btn_cancel.clicked.connect(dialog.reject)
        btn_confirm.clicked.connect(dialog.accept)

        return dialog.exec() == QDialog.DialogCode.Accepted

    # =========================================================================
    # Slots (run on main thread)
    # =========================================================================

    @Slot(list)
    def _do_update_packages(self, packages: List[PackageDisplayInfo]) -> None:
        """Update package list on main thread."""
        self.window.package_list.set_packages(packages)

    @Slot(bool)
    def _do_show_loading(self, loading: bool) -> None:
        """Show/hide loading on main thread."""
        self.window.set_loading(loading)

    @Slot(str, str, int, int, float)
    def _do_show_progress(
        self,
        phase: str,
        name: str,
        current: int,
        total: int,
        speed: float
    ) -> None:
        """Show progress on main thread."""
        status = f"{phase}: {name} ({current}/{total})"
        if speed > 0:
            status += f" - {speed / 1024 / 1024:.1f} MB/s"
        self.window.statusBar().showMessage(status)

    @Slot(str, str, str, list)
    def _do_show_question(
        self,
        question_id: str,
        qtype: str,
        message: str,
        choices: List[str]
    ) -> None:
        """Show question dialog on main thread."""
        # TODO: Implement proper question dialog
        pass

    @Slot(bool, dict)
    def _do_show_complete(self, success: bool, summary: dict) -> None:
        """Show completion on main thread."""
        if success:
            count = summary.get('installed', 0) or summary.get('removed', 0)
            msg = f"Transaction terminée avec succès.\n{count} paquet(s) traité(s)."
            self._show_styled_message("Terminé", msg, "info")
            # Refresh package list after successful transaction
            self.window.controller.refresh_after_transaction()
        else:
            errors = summary.get('errors', [])
            msg = "La transaction a échoué.\n\n" + "\n".join(errors)
            self._show_styled_message("Erreur", msg, "error")

    @Slot(str, str)
    def _do_show_error(self, title: str, message: str) -> None:
        """Show error dialog on main thread."""
        self._show_styled_message(title, message, "error")

    def _show_styled_message(self, title: str, message: str, msg_type: str = "info") -> None:
        """Show a styled message dialog.

        Args:
            title: Dialog title.
            message: Message to display.
            msg_type: One of 'info', 'error', 'warning'.
        """
        icons = {'info': 'ℹ️', 'error': '❌', 'warning': '⚠️'}
        colors = {'info': '#2196f3', 'error': '#c62828', 'warning': '#f57c00'}

        icon = icons.get(msg_type, 'ℹ️')
        color = colors.get(msg_type, '#2196f3')

        dialog = QDialog(self.window)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(400)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(16)

        # Header
        header = QLabel(f"<span style='font-size: 18pt;'>{icon}</span> <b>{title}</b>")
        layout.addWidget(header)

        # Message
        msg_label = QLabel(message)
        msg_label.setWordWrap(True)
        layout.addWidget(msg_label)

        # Button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_ok = QPushButton("OK")
        btn_ok.setMinimumWidth(100)
        btn_ok.setStyleSheet(f"""
            QPushButton {{
                background-color: {color};
                color: white;
                font-weight: bold;
                padding: 6px 16px;
                border: none;
                border-radius: 4px;
            }}
            QPushButton:hover {{ background-color: {color}; opacity: 0.9; }}
            QPushButton:pressed {{ background-color: {color}; opacity: 0.8; }}
        """)
        btn_ok.clicked.connect(dialog.accept)

        btn_layout.addWidget(btn_ok)
        layout.addLayout(btn_layout)

        dialog.exec()

    @Slot(str, str, dict)
    def _do_show_confirm(self, title: str, message: str, details: dict) -> None:
        """Show confirmation dialog on main thread."""
        action = details.get('action', '')
        packages = details.get('packages', [])

        detail_text = f"Paquets ({len(packages)}):\n"
        for pkg in packages[:10]:
            detail_text += f"  - {pkg}\n"
        if len(packages) > 10:
            detail_text += f"  ... et {len(packages) - 10} autres\n"

        result = QMessageBox.question(
            self.window,
            title,
            f"{message}\n\n{detail_text}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if result == QMessageBox.StandardButton.Yes:
            self.window.statusBar().showMessage(f"Exécution de {action}...")

    @Slot()
    def _do_filter_changed(self) -> None:
        """Update filter panel on main thread."""
        self.window.filter_panel._update_from_state()
