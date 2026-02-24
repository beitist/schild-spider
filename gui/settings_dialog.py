from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.models import ConfigField
from core.plugin_loader import (
    get_adapter_class,
    get_adapter_registry,
    get_plugin_class,
    get_plugin_registry,
    save_settings,
)


class SettingsDialog(QDialog):
    """Einstellungen mit Adapter-Auswahl und Plugin-Manager."""

    settings_changed = Signal()

    def __init__(self, settings: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Einstellungen")
        self.setMinimumSize(780, 620)

        self._settings = settings
        self._plugin_pages: dict[str, _ConfigPage] = {}

        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- Allgemein ---
        general_group = QGroupBox("Allgemein")
        general_form = QFormLayout(general_group)

        self._txt_school = QLineEdit(self._settings.get("school_name", ""))
        general_form.addRow("Schulname:", self._txt_school)

        self._txt_class_filter = QLineEdit(self._settings.get("debug_class_filter", ""))
        self._txt_class_filter.setPlaceholderText(
            "leer = alle Klassen (z.B. 'test' zum Testen)"
        )
        self._txt_class_filter.setMinimumHeight(28)
        general_form.addRow("Klassenfilter:", self._txt_class_filter)

        root.addWidget(general_group)

        # --- Datenquelle (Adapter) ---
        adapter_group = QGroupBox("Datenquelle")
        adapter_layout = QVBoxLayout(adapter_group)

        # Adapter-Auswahl
        combo_row = QHBoxLayout()
        combo_row.addWidget(QLabel("Typ:"))
        self._cmb_adapter = QComboBox()
        combo_row.addWidget(self._cmb_adapter, stretch=1)
        adapter_layout.addLayout(combo_row)

        # Adapter-Config (Stacked)
        self._adapter_stack = QStackedWidget()
        adapter_layout.addWidget(self._adapter_stack)

        self._adapter_pages: dict[str, _ConfigPage] = {}
        self._populate_adapters()

        root.addWidget(adapter_group)

        # --- Plugins ---
        plugin_group = QGroupBox("Plugins")
        plugin_layout = QHBoxLayout(plugin_group)

        splitter = QSplitter()
        plugin_layout.addWidget(splitter)

        self._plugin_list = QListWidget()
        self._plugin_list.currentRowChanged.connect(self._on_plugin_selected)
        splitter.addWidget(self._plugin_list)

        # ScrollArea um den Stack, damit viele Felder nicht gestaucht werden
        self._plugin_scroll = QScrollArea()
        self._plugin_scroll.setWidgetResizable(True)
        self._plugin_stack = QStackedWidget()
        self._plugin_scroll.setWidget(self._plugin_stack)
        splitter.addWidget(self._plugin_scroll)

        splitter.setSizes([180, 500])

        self._populate_plugins()

        root.addWidget(plugin_group, stretch=1)

        # --- Buttons ---
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        btn_save = QPushButton("Speichern")
        btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(btn_save)

        btn_cancel = QPushButton("Abbrechen")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        root.addLayout(btn_row)

    # --- Adapter ---

    def _populate_adapters(self) -> None:
        registry = get_adapter_registry()
        adapter_cfg = self._settings.get("adapter", {})
        current_type = adapter_cfg.get("type", "schild_csv")

        for key, (module_path, class_name) in registry.items():
            adapter_class = get_adapter_class(key)
            if adapter_class is None:
                continue

            self._cmb_adapter.addItem(adapter_class.adapter_name(), key)

            config = adapter_cfg if adapter_cfg.get("type") == key else {}
            page = _ConfigPage(
                config_class=adapter_class,
                config=config,
                show_enabled=False,
                show_test=True,
            )
            self._adapter_stack.addWidget(page)
            self._adapter_pages[key] = page

        # Aktuellen Adapter auswählen
        idx = self._cmb_adapter.findData(current_type)
        if idx >= 0:
            self._cmb_adapter.setCurrentIndex(idx)
            self._adapter_stack.setCurrentIndex(idx)

        self._cmb_adapter.currentIndexChanged.connect(
            self._adapter_stack.setCurrentIndex
        )

    # --- Plugins ---

    def _populate_plugins(self) -> None:
        registry = get_plugin_registry()
        plugins_cfg = self._settings.get("plugins", {})

        for key, (module_path, class_name) in registry.items():
            plugin_class = get_plugin_class(key)
            if plugin_class is None:
                continue

            display_name = plugin_class.plugin_name()
            config = plugins_cfg.get(key, {})

            item = QListWidgetItem(display_name)
            self._plugin_list.addItem(item)

            page = _ConfigPage(
                config_class=plugin_class,
                config=config,
                show_enabled=True,
                show_test=True,
            )
            self._plugin_stack.addWidget(page)
            self._plugin_pages[key] = page

        if self._plugin_list.count() > 0:
            self._plugin_list.setCurrentRow(0)

    @Slot(int)
    def _on_plugin_selected(self, row: int) -> None:
        self._plugin_stack.setCurrentIndex(row)

    # --- Speichern ---

    def _on_save(self) -> None:
        self._settings["school_name"] = self._txt_school.text().strip()
        self._settings["debug_class_filter"] = self._txt_class_filter.text().strip()

        # Adapter
        adapter_key = self._cmb_adapter.currentData()
        adapter_page = self._adapter_pages.get(adapter_key)
        adapter_cfg = adapter_page.collect_config() if adapter_page else {}
        adapter_cfg["type"] = adapter_key
        self._settings["adapter"] = adapter_cfg

        # Plugins
        self._settings.setdefault("plugins", {})
        for key, page in self._plugin_pages.items():
            self._settings["plugins"][key] = page.collect_config()

        save_settings(self._settings)
        self.settings_changed.emit()
        self.accept()


# ---------------------------------------------------------------------------
# Generische Config-Seite (verwendet von Adaptern UND Plugins)
# ---------------------------------------------------------------------------


class _ConfigPage(QWidget):
    """Dynamische Settings-Seite, generiert aus config_schema()."""

    def __init__(
        self,
        config_class: type,
        config: dict,
        show_enabled: bool = True,
        show_test: bool = True,
    ) -> None:
        super().__init__()
        self._config_class = config_class
        self._config = config
        self._show_enabled = show_enabled
        self._show_test = show_test
        self._field_widgets: dict[str, QLineEdit] = {}
        self._chk_enabled: QCheckBox | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Aktiviert-Checkbox (nur für Plugins)
        if self._show_enabled:
            self._chk_enabled = QCheckBox("Aktiviert")
            self._chk_enabled.setChecked(self._config.get("enabled", False))
            layout.addWidget(self._chk_enabled)

        # Dynamisches Formular aus config_schema
        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.setContentsMargins(4, 8, 4, 8)
        schema: list[ConfigField] = self._config_class.config_schema()

        for field in schema:
            txt = QLineEdit(self._config.get(field.key, field.default))
            txt.setPlaceholderText(field.placeholder)
            txt.setMinimumHeight(32)

            if field.field_type in ("dir", "path"):
                row = QHBoxLayout()
                row.addWidget(txt)
                btn = QPushButton("...")
                btn.setFixedWidth(30)
                if field.field_type == "dir":
                    btn.clicked.connect(
                        lambda checked=False, t=txt: self._browse_dir(t)
                    )
                else:
                    btn.clicked.connect(
                        lambda checked=False, t=txt: self._browse_file(t)
                    )
                row.addWidget(btn)
                form.addRow(f"{field.label}:", row)
            else:
                if field.field_type == "password":
                    txt.setEchoMode(QLineEdit.EchoMode.Password)
                form.addRow(f"{field.label}:", txt)

            self._field_widgets[field.key] = txt

        layout.addLayout(form)

        # Verbindungstest-Button (nur für Plugins)
        if self._show_test:
            btn_row = QHBoxLayout()
            self._btn_test = QPushButton("Verbindung testen")
            self._btn_test.clicked.connect(self._on_test_connection)
            btn_row.addWidget(self._btn_test)

            self._lbl_status = QLabel("")
            btn_row.addWidget(self._lbl_status, stretch=1)

            layout.addLayout(btn_row)

    def _browse_dir(self, target: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Ordner wählen")
        if path:
            target.setText(path)

    def _browse_file(self, target: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Datei wählen")
        if path:
            target.setText(path)

    def _on_test_connection(self) -> None:
        self._lbl_status.setText("Teste...")
        self._lbl_status.setStyleSheet("")
        self._btn_test.setEnabled(False)

        try:
            config = self.collect_config()
            instance = self._config_class.from_config(config)
            ok, msg = instance.test_connection()

            if ok:
                self._lbl_status.setText(msg)
                self._lbl_status.setStyleSheet("color: green;")
            else:
                self._lbl_status.setText(msg)
                self._lbl_status.setStyleSheet("color: red;")
        except Exception as exc:
            self._lbl_status.setText(f"Fehler: {exc}")
            self._lbl_status.setStyleSheet("color: red;")
        finally:
            self._btn_test.setEnabled(True)

    def collect_config(self) -> dict:
        """Sammelt die aktuellen Formularwerte als Config-Dict."""
        config: dict = {}
        if self._chk_enabled is not None:
            config["enabled"] = self._chk_enabled.isChecked()
        for key, widget in self._field_widgets.items():
            config[key] = widget.text().strip()
        return config
