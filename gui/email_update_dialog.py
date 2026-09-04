"""Dialog: Email-Adressen in SchILD nach Klassenwechsel aktualisieren.

Zeigt die Vorschläge aus ``PluginBase.check_source_emails()`` als Tabelle
mit Checkboxen. Klassenwechsel sind vorausgewählt, sonstige Abweichungen
(Namensänderung, manuell vergebene Adresse) nicht — die entscheidet der
User bewusst. Kollisionen (keine eindeutige Adresse möglich) sind gesperrt.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

_REASON_TEXT: dict[str, str] = {
    "class_change": "Klassenwechsel",
    "mismatch": "Name/Schema abweichend",
    "collision": "Kollision — manuell vergeben",
}


class EmailUpdateDialog(QDialog):
    """Auswahl-Dialog für Email-Korrekturen in SchILD."""

    def __init__(self, suggestions: list[dict], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Email-Adressen aktualisieren")
        self.setMinimumSize(900, 480)
        self._suggestions = suggestions
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        intro = QLabel(
            f"<b>{len(self._suggestions)} Email-Adressen</b> passen nicht mehr "
            f"zum Schema. Ausgewählte Einträge werden in SchILD "
            f"(Feld <i>SchulEmail</i>) aktualisiert.<br>"
            f"Anschließend werden die Quelldaten neu geladen — der Wechsel "
            f"in M365 erfolgt danach über <b>Berechnen → Anwenden</b>."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(
            ["Schüler", "Klasse", "Bisherige Email", "Neue Email", "Grund"]
        )
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(True)
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        # Klassenwechsel zuerst, dann sonstige, Kollisionen zuletzt
        order = {"class_change": 0, "mismatch": 1, "collision": 2}
        for s in sorted(
            self._suggestions,
            key=lambda x: (order.get(x.get("reason", ""), 9), x.get("last_name", "")),
        ):
            reason = s.get("reason", "")
            reason_text = _REASON_TEXT.get(reason, reason)
            if reason == "class_change" and s.get("old_class"):
                reason_text += f" (alt: {s['old_class']})"

            item = QTreeWidgetItem(
                [
                    f"{s.get('last_name', '')}, {s.get('first_name', '')}",
                    s.get("class_name", ""),
                    s.get("old_email", ""),
                    s.get("email", "") or "—",
                    reason_text,
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, s)
            if reason == "collision" or not s.get("email"):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            else:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if s.get("checked")
                    else Qt.CheckState.Unchecked,
                )
            self._tree.addTopLevelItem(item)

        self._tree.itemChanged.connect(lambda *_: self._update_button())
        layout.addWidget(self._tree, stretch=1)

        btn_row = QHBoxLayout()
        btn_all = QPushButton("Alle auswählen")
        btn_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        btn_row.addWidget(btn_all)
        btn_none = QPushButton("Keine")
        btn_none.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        btn_row.addWidget(btn_none)
        btn_row.addStretch()

        self._btn_write = QPushButton()
        self._btn_write.setStyleSheet("font-weight: bold; padding: 6px 14px;")
        self._btn_write.clicked.connect(self.accept)
        btn_row.addWidget(self._btn_write)

        btn_cancel = QPushButton("Abbrechen")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

        self._update_button()

    # --- Helpers ---

    def _iter_items(self):
        for i in range(self._tree.topLevelItemCount()):
            yield self._tree.topLevelItem(i)

    def _set_all(self, state: Qt.CheckState) -> None:
        self._tree.blockSignals(True)
        for item in self._iter_items():
            if item.flags() & Qt.ItemFlag.ItemIsEnabled:
                item.setCheckState(0, state)
        self._tree.blockSignals(False)
        self._update_button()

    def _update_button(self) -> None:
        n = len(self.selected_updates())
        self._btn_write.setText(f"In SchILD schreiben ({n})")
        self._btn_write.setEnabled(n > 0)

    def selected_updates(self) -> list[dict]:
        """Angehakte Vorschläge — direkt als Write-back-Updates verwendbar
        (enthalten ``school_internal_id`` und ``email``)."""
        selected: list[dict] = []
        for item in self._iter_items():
            if (
                item.flags() & Qt.ItemFlag.ItemIsEnabled
                and item.checkState(0) == Qt.CheckState.Checked
            ):
                selected.append(item.data(0, Qt.ItemDataRole.UserRole))
        return selected
