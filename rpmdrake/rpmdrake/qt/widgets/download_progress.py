"""Collapsible progress bar widget for downloads and installations."""

from enum import Enum
from typing import List, Optional
from dataclasses import dataclass

from ..compat import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QFrame, Qt, QPushButton, Signal
)
from ..palette import PHASE_COLORS


class ProgressPhase(Enum):
    """Current phase of the transaction."""
    IDLE = "idle"
    DOWNLOAD = "download"
    INSTALL = "install"
    ERASE = "erase"
    RPMDB_SYNC = "rpmdb_sync"


@dataclass
class SlotInfo:
    """Info about a download slot."""
    slot: int
    name: Optional[str] = None
    bytes_done: int = 0
    bytes_total: int = 0
    source: str = ""
    source_type: str = ""


class DownloadSlotWidget(QFrame):
    """Widget for a single download slot (shown when expanded)."""

    def __init__(self, slot_num: int, parent=None):
        super().__init__(parent)
        self.slot_num = slot_num
        self._setup_ui()

    def _setup_ui(self):
        """Setup the UI."""
        self.setFrameStyle(QFrame.Shape.NoFrame)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(6)

        # Col 1: Slot number (auto-size based on font)
        self.slot_label = QLabel(f"#{self.slot_num + 1}")
        self.slot_label.setStyleSheet("color: #888; font-family: monospace;")
        layout.addWidget(self.slot_label)

        # Col 2: Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(100)
        self.progress_bar.setFixedHeight(14)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid #ccc;
                border-radius: 2px;
                background: #f0f0f0;
            }}
            QProgressBar::chunk {{
                background: {PHASE_COLORS["download"]};
            }}
        """)
        layout.addWidget(self.progress_bar)

        # Col 3: Package name (stretch to fill middle)
        self.name_label = QLabel("")
        self.name_label.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.name_label, stretch=1)

        # Col 4: Size info + source (right-aligned, auto-size)
        self.size_label = QLabel("")
        self.size_label.setStyleSheet("color: #666; font-family: monospace;")
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.size_label)

        self.source_label = QLabel("")
        self.source_label.setStyleSheet("color: #888;")
        self.source_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.source_label)

    def set_progress(self, info: SlotInfo):
        """Update the slot with progress info."""
        if info.name is None:
            # Idle slot
            self.name_label.setText("")
            self.progress_bar.setValue(0)
            self.progress_bar.setRange(0, 100)
            self.size_label.setText("")
            self.source_label.setText("")
        else:
            # Active download
            self.name_label.setText(info.name)

            if info.bytes_total > 0:
                pct = int(info.bytes_done * 100 / info.bytes_total)
                self.progress_bar.setRange(0, 100)
                self.progress_bar.setValue(pct)

                # Size display
                done_mb = info.bytes_done / 1024 / 1024
                total_mb = info.bytes_total / 1024 / 1024
                self.size_label.setText(f"{done_mb:.1f}/{total_mb:.1f}MB")
            else:
                self.progress_bar.setRange(0, 0)  # Indeterminate
                self.size_label.setText("")

            # Source with color based on type
            if info.source:
                source_text = info.source
                if info.source_type == "peer":
                    self.source_label.setStyleSheet("color: #388e3c;")
                    if not source_text.startswith("peer@"):
                        source_text = f"peer@{source_text}"
                else:
                    self.source_label.setStyleSheet("color: #888;")
                self.source_label.setText(f"({source_text})")
            else:
                self.source_label.setText("")


class CollapsibleProgressWidget(QWidget):
    """Collapsible progress bar widget for transactions.

    Shows a compact single-line progress bar by default.
    Click to expand and see detailed parallel download info.

    Phases:
    - Download (blue): downloading packages
    - Install (orange): installing packages
    - RPMdb sync (gray): waiting for database sync
    """

    # Signal emitted when cancel button is clicked
    cancel_requested = Signal()

    def __init__(self, num_slots: int = 4, parent=None):
        super().__init__(parent)
        self.num_slots = num_slots
        self._expanded = False
        self._phase = ProgressPhase.IDLE
        self._slots: List[DownloadSlotWidget] = []
        self._setup_ui()
        self.hide()  # Hidden by default

    def _setup_ui(self):
        """Setup the UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header (always visible) - clickable
        self.header = QFrame()
        self.header.setFrameStyle(QFrame.Shape.StyledPanel)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.mousePressEvent = self._on_header_click

        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.setSpacing(8)

        # Expand/collapse indicator (auto-size)
        self.expand_label = QLabel("▶")
        self.expand_label.setStyleSheet("color: #666;")
        header_layout.addWidget(self.expand_label)

        # Phase label
        self.phase_label = QLabel("Téléchargement")
        self.phase_label.setStyleSheet("font-weight: bold;")
        header_layout.addWidget(self.phase_label)

        # Count label [3/14]
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #666;")
        header_layout.addWidget(self.count_label)

        # Main progress bar
        self.main_progress = QProgressBar()
        self.main_progress.setMinimumWidth(100)
        self.main_progress.setTextVisible(False)
        header_layout.addWidget(self.main_progress, stretch=1)

        # Percentage (auto-size)
        self.pct_label = QLabel("")
        self.pct_label.setStyleSheet("font-family: monospace;")
        header_layout.addWidget(self.pct_label)

        # Speed / current package name (auto-size, stretch to fill)
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color: #666; font-family: monospace;")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self.info_label, stretch=1)

        # Cancel button
        self.cancel_btn = QPushButton("Annuler")
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                font-weight: bold;
                padding: 4px 12px;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #d32f2f; }
            QPushButton:pressed { background-color: #b71c1c; }
        """)
        self.cancel_btn.clicked.connect(self._on_cancel_click)
        header_layout.addWidget(self.cancel_btn)

        layout.addWidget(self.header)

        # Detail panel (shown when expanded)
        self.detail_panel = QFrame()
        self.detail_panel.setFrameStyle(QFrame.Shape.StyledPanel)
        self.detail_panel.setStyleSheet("background: #fafafa; border-top: 1px solid #ddd;")
        detail_layout = QVBoxLayout(self.detail_panel)
        detail_layout.setContentsMargins(4, 4, 4, 4)
        detail_layout.setSpacing(0)

        # Slot widgets
        for i in range(self.num_slots):
            slot = DownloadSlotWidget(i, self)
            self._slots.append(slot)
            detail_layout.addWidget(slot)

        self.detail_panel.hide()
        layout.addWidget(self.detail_panel)

        # Set initial phase styling
        self._update_phase_style()

    def _on_header_click(self, event):
        """Toggle expanded state (only during download phase)."""
        # Only allow expansion during download phase
        if self._phase != ProgressPhase.DOWNLOAD:
            return
        self._expanded = not self._expanded
        self.expand_label.setText("▼" if self._expanded else "▶")
        self.detail_panel.setVisible(self._expanded)

    def _on_cancel_click(self):
        """Handle cancel button click."""
        self.cancel_requested.emit()

    def _collapse_details(self):
        """Collapse detail panel and hide expand indicator."""
        self._expanded = False
        self.expand_label.hide()
        self.detail_panel.hide()
        self.header.setCursor(Qt.CursorShape.ArrowCursor)

    def _update_phase_style(self):
        """Update styling based on current phase."""
        color = PHASE_COLORS.get(self._phase.value, "#2196f3")

        self.main_progress.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid #ccc;
                border-radius: 3px;
                background: #e0e0e0;
            }}
            QProgressBar::chunk {{
                background: {color};
                border-radius: 2px;
            }}
        """)

    def set_phase(self, phase: ProgressPhase):
        """Set the current phase."""
        if phase != self._phase:
            self._phase = phase
            self._update_phase_style()

            if phase == ProgressPhase.DOWNLOAD:
                self.phase_label.setText("Téléchargement")
                self.expand_label.show()
                self.header.setCursor(Qt.CursorShape.PointingHandCursor)
            elif phase == ProgressPhase.INSTALL:
                self.phase_label.setText("Installation")
                self._collapse_details()
            elif phase == ProgressPhase.ERASE:
                self.phase_label.setText("Suppression")
                self._collapse_details()
            elif phase == ProgressPhase.RPMDB_SYNC:
                self.phase_label.setText("Finalisation")
                self.main_progress.setRange(0, 0)  # Indeterminate
                self.pct_label.setText("")
                self.info_label.setText("Mise à jour de la base RPM…")
                self._collapse_details()
            elif phase == ProgressPhase.IDLE:
                self.hide()

    def start_transaction(self, action: str = "install"):
        """Show the widget and start in appropriate phase.

        Args:
            action: 'install', 'upgrade', or 'erase'
        """
        # For erase, skip download phase and go straight to erase
        if action == "erase":
            self.set_phase(ProgressPhase.ERASE)
        else:
            self.set_phase(ProgressPhase.DOWNLOAD)

        self.main_progress.setRange(0, 100)
        self.main_progress.setValue(0)
        self.count_label.setText("")
        self.pct_label.setText("0%")
        self.info_label.setText("")
        self.show()
        # Hide status frame if we have access to the main window
        if hasattr(self.parent(), 'parent') and hasattr(self.parent().parent(), 'status_frame'):
            self.parent().parent().status_frame.hide()

    def update_download(
        self,
        pkg_current: int,
        pkg_total: int,
        bytes_done: int,
        bytes_total: int,
        slots: List[SlotInfo],
        speed: float = 0.0
    ):
        """Update download progress."""
        if self._phase != ProgressPhase.DOWNLOAD:
            self.set_phase(ProgressPhase.DOWNLOAD)

        self.count_label.setText(f"[{pkg_current}/{pkg_total}]")

        if bytes_total > 0:
            pct = int(bytes_done * 100 / bytes_total)
            self.main_progress.setRange(0, 100)
            self.main_progress.setValue(pct)
            self.pct_label.setText(f"{pct}%")
        else:
            self.main_progress.setRange(0, 0)
            self.pct_label.setText("")

        if speed > 0:
            if speed > 1024 * 1024:
                self.info_label.setText(f"{speed / 1024 / 1024:.1f} MB/s")
            else:
                self.info_label.setText(f"{speed / 1024:.0f} KB/s")
        else:
            self.info_label.setText("")

        # Update slots
        for i, slot_widget in enumerate(self._slots):
            if i < len(slots):
                slot_widget.set_progress(slots[i])
            else:
                slot_widget.set_progress(SlotInfo(slot=i))

    def update_install(self, name: str, current: int, total: int):
        """Update install progress."""
        if self._phase != ProgressPhase.INSTALL:
            self.set_phase(ProgressPhase.INSTALL)
            # Clear slots when switching to install
            for slot in self._slots:
                slot.set_progress(SlotInfo(slot=slot.slot_num))

        self.count_label.setText(f"[{current}/{total}]")

        if total > 0:
            pct = int(current * 100 / total)
            self.main_progress.setRange(0, 100)
            self.main_progress.setValue(pct)
            self.pct_label.setText(f"{pct}%")
        else:
            self.main_progress.setRange(0, 0)
            self.pct_label.setText("")

        # Show package name
        self.info_label.setText(name)

    def update_erase(self, name: str, current: int, total: int):
        """Update erase/remove progress."""
        if self._phase != ProgressPhase.ERASE:
            self.set_phase(ProgressPhase.ERASE)
            # Clear slots
            for slot in self._slots:
                slot.set_progress(SlotInfo(slot=slot.slot_num))

        self.count_label.setText(f"[{current}/{total}]")

        if total > 0:
            pct = int(current * 100 / total)
            self.main_progress.setRange(0, 100)
            self.main_progress.setValue(pct)
            self.pct_label.setText(f"{pct}%")
        else:
            self.main_progress.setRange(0, 0)
            self.pct_label.setText("")

        # Show package name
        self.info_label.setText(name)

    def start_rpmdb_sync(self):
        """Switch to rpmdb sync phase."""
        self.set_phase(ProgressPhase.RPMDB_SYNC)
        self.count_label.setText("")
        # Clear slots
        for slot in self._slots:
            slot.set_progress(SlotInfo(slot=slot.slot_num))

    def finish(self):
        """Transaction complete - hide widget."""
        self._phase = ProgressPhase.IDLE
        self._expanded = False
        self.expand_label.setText("▶")
        self.detail_panel.hide()
        self.hide()
        # Show status frame again
        if hasattr(self.parent(), 'parent') and hasattr(self.parent().parent(), 'status_frame'):
            self.parent().parent().status_frame.show()

    def reset(self):
        """Reset to initial state."""
        self.finish()
