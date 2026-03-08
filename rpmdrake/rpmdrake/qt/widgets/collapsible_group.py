"""Collapsible group widget for rpmdrake-ng."""

from ..compat import Qt, QWidget, QVBoxLayout, QPushButton

__all__ = ["CollapsibleGroup"]


class CollapsibleGroup(QWidget):
    """A widget with a clickable bold header that shows/hides its content.

    Example::

        group = CollapsibleGroup("Dépendances")
        group.addWidget(QLabel("libfoo"))
        layout.addWidget(group)
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._expanded = True
        self._title = title

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

        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(4, 4, 4, 4)
        self._content_layout.setSpacing(4)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._header)
        layout.addWidget(self._content)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def addWidget(self, widget: QWidget) -> None:
        """Add *widget* to the collapsible content area."""
        self._content_layout.addWidget(widget)

    def addLayout(self, layout) -> None:
        """Add *layout* to the collapsible content area."""
        self._content_layout.addLayout(layout)

    def content_layout(self) -> QVBoxLayout:
        """Return the content layout for direct manipulation."""
        return self._content_layout

    def setExpanded(self, expanded: bool) -> None:
        """Programmatically expand or collapse the group."""
        if self._expanded != expanded:
            self._toggle()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        arrow = "▼" if self._expanded else "▶"
        self._header.setText(f"{arrow} {self._title}")
