from __future__ import annotations

from enum import Enum, auto

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from core.models import ChangeSet


class PluginCardState(Enum):
    IDLE = auto()
    DATA_LOADED = auto()
    COMPUTING = auto()
    COMPUTED = auto()
    APPLYING = auto()
    APPLIED = auto()


# Status-Indikator je State
_STATE_INDICATOR: dict[PluginCardState, str] = {
    PluginCardState.IDLE: "\u25cb",  # ○
    PluginCardState.DATA_LOADED: "\u25cf",  # ●  blau
    PluginCardState.COMPUTING: "\u25cf",  # ●  gelb
    PluginCardState.COMPUTED: "\u25cf",  # ●  orange
    PluginCardState.APPLYING: "\u25cf",  # ●  gelb
    PluginCardState.APPLIED: "\u2714",  # ✔  grün
}

_STATE_COLOR: dict[PluginCardState, str] = {
    PluginCardState.IDLE: "#999",
    PluginCardState.DATA_LOADED: "#4a90d9",
    PluginCardState.COMPUTING: "#d4a017",
    PluginCardState.COMPUTED: "#e67e22",
    PluginCardState.APPLYING: "#d4a017",
    PluginCardState.APPLIED: "#27ae60",
}


class PluginCard(QFrame):
    """Karte für ein einzelnes Plugin im Plugin-Stack."""

    selected = Signal(str)  # plugin_key
    compute_requested = Signal(str)  # plugin_key
    apply_requested = Signal(str)  # plugin_key

    def __init__(self, plugin_key: str, display_name: str, parent=None) -> None:
        super().__init__(parent)
        self._plugin_key = plugin_key
        self._display_name = display_name
        self._state = PluginCardState.IDLE
        self._changeset: ChangeSet | None = None
        self._excluded_ids: set[str] = set()
        self._is_selected = False

        self._build_ui()
        self._update_ui()

    # --- Properties ---

    @property
    def plugin_key(self) -> str:
        return self._plugin_key

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def state(self) -> PluginCardState:
        return self._state

    @state.setter
    def state(self, value: PluginCardState) -> None:
        self._state = value
        self._update_ui()

    @property
    def changeset(self) -> ChangeSet | None:
        return self._changeset

    @changeset.setter
    def changeset(self, value: ChangeSet | None) -> None:
        self._changeset = value
        self._update_ui()

    @property
    def excluded_ids(self) -> set[str]:
        return self._excluded_ids

    @excluded_ids.setter
    def excluded_ids(self, value: set[str]) -> None:
        self._excluded_ids = value
        self._update_summary()

    # --- UI ---

    def _build_ui(self) -> None:
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        # Zeile 1: Indikator + Name
        header = QHBoxLayout()
        self._lbl_indicator = QLabel()
        self._lbl_indicator.setFixedWidth(18)
        header.addWidget(self._lbl_indicator)

        self._lbl_name = QLabel(self._display_name)
        self._lbl_name.setStyleSheet("font-weight: bold; font-size: 13px;")
        header.addWidget(self._lbl_name)
        header.addStretch()
        layout.addLayout(header)

        # Zeile 2: Zusammenfassung
        self._lbl_summary = QLabel("")
        self._lbl_summary.setStyleSheet("color: #666; font-size: 11px;")
        self._lbl_summary.hide()
        layout.addWidget(self._lbl_summary)

        # Zeile 3: Buttons
        btn_row = QHBoxLayout()
        self._btn_compute = QPushButton("Berechnen")
        self._btn_compute.setFixedHeight(28)
        self._btn_compute.clicked.connect(
            lambda: self.compute_requested.emit(self._plugin_key)
        )
        btn_row.addWidget(self._btn_compute)

        self._btn_apply = QPushButton("Anwenden")
        self._btn_apply.setFixedHeight(28)
        self._btn_apply.clicked.connect(
            lambda: self.apply_requested.emit(self._plugin_key)
        )
        btn_row.addWidget(self._btn_apply)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._apply_stylesheet()

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet("""
            PluginCard {
                background-color: #f5f5f5;
                border: 2px solid #ddd;
                border-radius: 6px;
            }
            PluginCard[selected="true"] {
                border-color: #4a90d9;
                background-color: #e8f0fe;
            }
        """)

    def set_selected(self, selected: bool) -> None:
        self._is_selected = selected
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_buttons_enabled(self, enabled: bool) -> None:
        """Überschreibt Button-States (z.B. während ein Worker läuft)."""
        self._btn_compute.setEnabled(enabled)
        self._btn_apply.setEnabled(enabled)

    def refresh_buttons(self) -> None:
        """Setzt Button-States basierend auf dem aktuellen Card-State."""
        self._update_buttons()

    # --- Internes Update ---

    def _update_ui(self) -> None:
        self._update_indicator()
        self._update_buttons()
        self._update_summary()

    def _update_indicator(self) -> None:
        char = _STATE_INDICATOR.get(self._state, "\u25cb")
        color = _STATE_COLOR.get(self._state, "#999")
        self._lbl_indicator.setText(char)
        self._lbl_indicator.setStyleSheet(f"color: {color}; font-size: 16px;")

    def _update_buttons(self) -> None:
        s = self._state
        self._btn_compute.setEnabled(
            s
            in (
                PluginCardState.DATA_LOADED,
                PluginCardState.COMPUTED,
                PluginCardState.APPLIED,
            )
        )

        can_apply = s == PluginCardState.COMPUTED
        if can_apply and self._changeset is not None and self._changeset.requires_force:
            can_apply = False
        self._btn_apply.setEnabled(can_apply)

    def _update_summary(self) -> None:
        cs = self._changeset
        if cs is None or self._state in (
            PluginCardState.IDLE,
            PluginCardState.DATA_LOADED,
        ):
            self._lbl_summary.hide()
            return

        excluded = self._excluded_ids
        n_new = sum(1 for s in cs.new if s["school_internal_id"] not in excluded)
        n_changed = sum(
            1 for s in cs.changed if s["school_internal_id"] not in excluded
        )
        n_suspended = sum(1 for sid in cs.suspended if sid not in excluded)
        n_photos = sum(
            1 for s in cs.photo_updates if s["school_internal_id"] not in excluded
        )

        parts = []
        if n_new:
            parts.append(f"{n_new} neu")
        if n_changed:
            parts.append(f"{n_changed} geändert")
        if n_suspended:
            parts.append(f"{n_suspended} abgemeldet")
        if n_photos:
            parts.append(f"{n_photos} Fotos")

        text = " \u00b7 ".join(parts) if parts else "Keine Änderungen"

        if cs.requires_force:
            text += "  \u26a0 Failsafe!"

        self._lbl_summary.setText(text)
        self._lbl_summary.show()

    # --- Events ---

    def mousePressEvent(self, event) -> None:
        # Klick auf die Card (außer Buttons) → Auswahl
        self.selected.emit(self._plugin_key)
        super().mousePressEvent(event)
