"""Abstract interface for rpmdrake-ng GUI views.

ViewInterface defines the contract between the Controller and any GUI
implementation (Qt, GTK, etc.). This allows the same business logic
to drive different frontends.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from .models import PackageDisplayInfo

__all__ = ["ViewInterface"]


class ViewInterface(ABC):
    """Abstract interface for GUI views (Qt, GTK).

    All methods are called by the Controller to update the UI.
    Implementations must handle thread safety (e.g., Qt signals).
    """

    @abstractmethod
    def on_package_list_update(self, packages: List['PackageDisplayInfo']) -> None:
        """Update the package list display.

        Args:
            packages: List of packages to display.
        """
        pass

    @abstractmethod
    def show_loading(self, loading: bool) -> None:
        """Show or hide loading indicator.

        Args:
            loading: True to show loading, False to hide.
        """
        pass

    @abstractmethod
    def on_progress(
        self,
        phase: str,
        name: str,
        current: int,
        total: int,
        speed: float = 0.0
    ) -> None:
        """Update progress display.

        Args:
            phase: Current phase ('download', 'install', 'erase').
            name: Current package name.
            current: Current progress value.
            total: Total progress value.
            speed: Transfer speed in bytes/sec (for downloads).
        """
        pass

    @abstractmethod
    def on_question(
        self,
        question_id: str,
        qtype: str,
        message: str,
        choices: List[str]
    ) -> None:
        """Display a question dialog.

        Called when user input is needed (alternatives, conflicts, etc.).
        The view should display the question and call controller.answer_question()
        with the user's choice.

        Args:
            question_id: Unique identifier for this question.
            qtype: Question type ('alternative', 'conflict', 'config').
            message: Question message to display.
            choices: Available choices.
        """
        pass

    @abstractmethod
    def on_transaction_complete(self, success: bool, summary: dict) -> None:
        """Handle transaction completion.

        Args:
            success: True if transaction succeeded.
            summary: Transaction summary with keys:
                - installed: int, number of packages installed
                - removed: int, number of packages removed
                - rpmnew_files: List[str], config files saved as .rpmnew
                - errors: List[str], any errors that occurred
        """
        pass

    @abstractmethod
    def show_error(self, title: str, message: str) -> None:
        """Display an error message.

        Args:
            title: Error dialog title.
            message: Error message.
        """
        pass

    @abstractmethod
    def show_confirmation(
        self,
        title: str,
        message: str,
        details: dict
    ) -> None:
        """Display transaction confirmation dialog.

        Args:
            title: Dialog title.
            message: Confirmation message.
            details: Transaction details with keys:
                - install: List[dict], packages to install
                - remove: List[dict], packages to remove
                - download_size: int, total download size in bytes
                - disk_delta: int, disk space change in bytes
        """
        pass

    def on_filter_state_changed(self) -> None:
        """Called when filter state is changed programmatically.

        The view should update its filter checkboxes to match
        the controller's filter_state.
        """
        pass

    def start_transaction(self) -> None:
        """Signal start of a transaction.

        The view should show a progress widget/indicator.
        """
        pass

    def on_download_progress(
        self,
        pkg_current: int,
        pkg_total: int,
        bytes_done: int,
        bytes_total: int,
        slots: list
    ) -> None:
        """Update download progress with parallel slot details.

        Args:
            pkg_current: Number of completed packages.
            pkg_total: Total number of packages to download.
            bytes_done: Total bytes downloaded so far.
            bytes_total: Total bytes to download.
            slots: List of slot info dicts with keys:
                - slot: int, slot number (0-3)
                - name: str or None, package name (None if idle)
                - bytes_done: int, bytes downloaded for this package
                - bytes_total: int, total bytes for this package
                - source: str, server or peer name
                - source_type: str, 'server', 'peer', or 'cache'
        """
        pass

    def on_install_progress(self, name: str, current: int, total: int) -> None:
        """Update install progress.

        Args:
            name: Current package being installed.
            current: Number of packages installed so far.
            total: Total number of packages to install.
        """
        pass

    def start_rpmdb_sync(self) -> None:
        """Signal start of rpmdb sync phase.

        The view should show an indeterminate progress indicator
        while waiting for the database sync.
        """
        pass

    def finish_transaction(self) -> None:
        """Signal end of transaction.

        The view should hide the progress widget/indicator.
        """
        pass

    def show_action_confirmation(self, action: str, packages: List[str]) -> bool:
        """Show confirmation dialog before executing an action.

        Args:
            action: Action label (Installer, Supprimer, Mettre à jour).
            packages: List of package names.

        Returns:
            True if user confirmed, False otherwise.
        """
        return True  # Default: no confirmation (for non-interactive use)

    def show_transaction_confirmation(self, action: str, summary: dict) -> bool:
        """Show detailed transaction confirmation dialog.

        Args:
            action: Action type ('install', 'erase', 'upgrade').
            summary: Resolution summary with keys:
                - requested: List[str], explicitly requested packages
                - install_deps: List[str], dependencies to install
                - upgrade: List[str], packages to upgrade
                - remove: List[str], explicitly requested removals
                - remove_deps: List[str], reverse dependencies being removed
                - orphans_created: List[str], packages becoming orphans

        Returns:
            True if user confirmed, False otherwise.
        """
        return True  # Default: no confirmation (for non-interactive use)

    def show_alternative_choice(
        self,
        capability: str,
        required_by: str,
        providers: List[str]
    ) -> str:
        """Show dialog for choosing between alternative providers.

        Called during resolution when a dependency has multiple providers.

        Args:
            capability: The capability being satisfied (e.g., "libreoffice-langpack").
            required_by: Package that requires this capability.
            providers: List of package names that can provide the capability.

        Returns:
            Chosen package name, or empty string if cancelled.
        """
        return providers[0] if providers else ""  # Default: first choice
