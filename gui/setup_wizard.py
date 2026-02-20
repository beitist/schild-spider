"""Erststart-Assistent — erzeugt settings.json bei erstem Programmstart.

Zeigt ein Dialogfenster mit:
1. Schulname (Pflichtfeld)
2. Datenquelle / Adapter (Combobox, genau 1)
3. Output-Plugins (Checkboxen, beliebig viele)

Nach „Fertig" wird generate_default_settings() aufgerufen und die
settings.json geschrieben. Die API-Keys etc. müssen anschließend
im Settings-Dialog konfiguriert werden.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from core.plugin_loader import (
    generate_default_settings,
    get_adapter_class,
    get_adapter_registry,
    get_plugin_class,
    get_plugin_registry,
    save_settings,
)


class SetupWizard(QDialog):
    """Einrichtungs-Dialog beim ersten Programmstart."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Schild Spider — Ersteinrichtung")
        self.setMinimumWidth(500)
        # Dialog darf nicht einfach geschlossen werden (Pflicht-Setup)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

        self._plugin_checks: dict[str, QCheckBox] = {}
        self._result_settings: dict | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # --- Willkommen ---
        welcome = QLabel(
            "<h2>Willkommen bei Schild Spider!</h2>"
            "<p>Bitte richte die Grundkonfiguration ein. "
            "API-Zugangsdaten und Dateipfade können anschließend "
            "unter <b>Einstellungen</b> konfiguriert werden.</p>"
        )
        welcome.setWordWrap(True)
        layout.addWidget(welcome)

        # --- Schulname ---
        school_group = QGroupBox("Schule")
        school_form = QFormLayout(school_group)
        self._txt_school = QLineEdit()
        self._txt_school.setPlaceholderText("z.B. Käthe-Kollwitz-Berufskolleg")
        school_form.addRow("Schulname:", self._txt_school)
        layout.addWidget(school_group)

        # --- Datenquelle (Adapter) ---
        adapter_group = QGroupBox("Datenquelle (Input)")
        adapter_form = QFormLayout(adapter_group)
        self._cmb_adapter = QComboBox()

        # Alle registrierten Adapter als Auswahl anbieten
        for key, (module_path, class_name) in get_adapter_registry().items():
            adapter_class = get_adapter_class(key)
            if adapter_class is not None:
                self._cmb_adapter.addItem(adapter_class.adapter_name(), key)

        adapter_form.addRow("Typ:", self._cmb_adapter)
        layout.addWidget(adapter_group)

        # --- Output-Plugins ---
        plugin_group = QGroupBox("Zielsysteme (Output)")
        plugin_layout = QVBoxLayout(plugin_group)

        # Alle registrierten Plugins als Checkboxen anbieten
        for key, (module_path, class_name) in get_plugin_registry().items():
            plugin_class = get_plugin_class(key)
            if plugin_class is None:
                continue
            chk = QCheckBox(plugin_class.plugin_name())
            self._plugin_checks[key] = chk
            plugin_layout.addWidget(chk)

        if not self._plugin_checks:
            plugin_layout.addWidget(QLabel("Keine Plugins registriert."))

        layout.addWidget(plugin_group)

        # --- Hinweis ---
        hint = QLabel(
            "<i>💡 Die API-Zugangsdaten und Dateipfade für aktivierte "
            "Plugins können anschließend im Einstellungs-Dialog "
            "konfiguriert werden.</i>"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; margin-top: 8px;")
        layout.addWidget(hint)

        # --- Buttons ---
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_finish = QPushButton("Einrichtung abschließen")
        btn_finish.setStyleSheet("padding: 8px 20px; font-size: 13px;")
        btn_finish.clicked.connect(self._on_finish)
        btn_row.addWidget(btn_finish)
        layout.addLayout(btn_row)

    def _on_finish(self) -> None:
        """Validiert Eingaben und erzeugt die Settings."""
        school_name = self._txt_school.text().strip()
        if not school_name:
            QMessageBox.warning(
                self,
                "Schulname fehlt",
                "Bitte gib einen Schulnamen ein.",
            )
            return

        # Gewählten Adapter und aktivierte Plugins sammeln
        adapter_type = self._cmb_adapter.currentData()
        enabled_plugins = [
            key for key, chk in self._plugin_checks.items() if chk.isChecked()
        ]

        # Settings erzeugen und speichern
        self._result_settings = generate_default_settings(
            school_name=school_name,
            adapter_type=adapter_type,
            enabled_plugins=enabled_plugins,
        )
        save_settings(self._result_settings)

        self.accept()

    def get_settings(self) -> dict | None:
        """Gibt die erzeugten Settings zurück (None wenn abgebrochen)."""
        return self._result_settings
