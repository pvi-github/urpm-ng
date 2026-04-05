"""Search bar widget for rpmdrake-ng."""

from ..compat import QLineEdit, Signal, Qt

__all__ = ["SearchBar"]


class SearchBar(QLineEdit):
    """Search bar with clear button and placeholder.

    Emits search_changed signal when text changes.
    """

    search_changed = Signal(str)
    focus_list_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setPlaceholderText("Rechercher des paquets...")
        self.setClearButtonEnabled(True)

        # Style
        self.setMinimumHeight(32)

        # Connect signals
        self.textChanged.connect(self._on_text_changed)

    def keyPressEvent(self, event) -> None:
        """Arrow Down from search bar jumps to the package list."""
        if event.key() == Qt.Key.Key_Down:
            self.focus_list_requested.emit()
            return
        super().keyPressEvent(event)

    def _on_text_changed(self, text: str) -> None:
        """Handle text change."""
        self.search_changed.emit(text)

    def focus_search(self) -> None:
        """Focus the search bar and select all text."""
        self.setFocus()
        self.selectAll()
