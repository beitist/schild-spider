from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.models import ChangeSet, StudentRecord, TeacherRecord
from core.plugin_loader import as_bool, get_plugin_class, load_adapter, load_settings
from gui.email_update_dialog import EmailUpdateDialog
from gui.plugin_card import PluginCard, PluginCardState
from gui.settings_dialog import SettingsDialog
from gui.workers import LoadWorker, PluginApplyWorker, PluginComputeWorker


# ---------------------------------------------------------------------------
# Log-Handler → GUI
# ---------------------------------------------------------------------------


class _LogSignalBridge(QObject):
    """Brücke: nimmt Log-Nachrichten per Signal entgegen (thread-safe).

    Qt-Signals werden automatisch als QueuedConnection ausgeführt wenn
    Sender und Empfänger in verschiedenen Threads laufen. Dadurch wird
    der QTextEdit-Zugriff immer im Main-Thread ausgeführt.
    """

    message = Signal(str)


class _QtLogHandler(logging.Handler):
    """Leitet Python-Log-Einträge per Qt-Signal an die GUI weiter (thread-safe)."""

    def __init__(self, bridge: _LogSignalBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self._bridge.message.emit(msg)


# ---------------------------------------------------------------------------
# Hauptfenster
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Schild Spider")
        self.setMinimumSize(900, 600)

        self._settings: dict = {}
        self._students: list[StudentRecord] = []
        self._teachers: list[TeacherRecord] = []
        self._plugin_cards: dict[str, PluginCard] = {}
        self._selected_card_key: str | None = None
        self._worker: object | None = None
        self._worker_thread: QThread | None = None
        self._pending_write_back: list[dict] = []
        self._email_suggestions: list[dict] = []  # aus LoadWorker.email_check_ready
        self._keep_log_on_load = False  # Auto-Reload nach Write-back: Log behalten
        self._apply_stats: dict[str, tuple[int, int]] = {}  # key → (ok, fehler)
        self._apply_had_error = False

        self._build_ui()
        self._load_settings()
        self._populate_plugin_cards()

    # --- UI Aufbau ---

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Header
        header = QHBoxLayout()
        self._lbl_school = QLabel("Schule: \u2013")
        self._lbl_school.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(self._lbl_school)
        header.addStretch()
        self._btn_settings = QPushButton("Einstellungen...")
        self._btn_settings.clicked.connect(self._open_settings_dialog)
        header.addWidget(self._btn_settings)
        root.addLayout(header)

        # Haupt-Splitter: Links (Daten + Plugins) | Rechts (Vorschau + Log)
        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(main_splitter, stretch=1)

        # --- Linkes Panel ---
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)

        # Quelldaten laden
        load_row = QHBoxLayout()
        self._btn_load = QPushButton("Quelldaten laden")
        self._btn_load.setStyleSheet("padding: 6px 14px; font-size: 13px;")
        self._btn_load.clicked.connect(self._on_load_data)
        load_row.addWidget(self._btn_load)
        self._lbl_counts = QLabel("")
        self._lbl_counts.setStyleSheet("color: #666; font-size: 12px;")
        load_row.addWidget(self._lbl_counts)
        # Email-Korrekturen in SchILD (hidden bis Vorschläge vorliegen)
        self._btn_email_update = QPushButton("Email-Adressen aktualisieren")
        self._btn_email_update.setStyleSheet("padding: 6px 14px; font-size: 13px;")
        self._btn_email_update.clicked.connect(self._on_email_update)
        self._btn_email_update.hide()
        load_row.addWidget(self._btn_email_update)
        load_row.addStretch()
        left_layout.addLayout(load_row)

        # Plugin-Stack
        lbl_plugins = QLabel("Plugins")
        lbl_plugins.setStyleSheet(
            "font-weight: bold; font-size: 12px; color: #888; margin-top: 8px;"
        )
        left_layout.addWidget(lbl_plugins)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._plugin_stack_widget = QWidget()
        self._plugin_stack_layout = QVBoxLayout(self._plugin_stack_widget)
        self._plugin_stack_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._plugin_stack_layout.setSpacing(6)
        self._plugin_stack_layout.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(self._plugin_stack_widget)
        left_layout.addWidget(scroll, stretch=1)

        # Rückschreiben-Button (hidden bis Write-back-Daten vorliegen)
        self._btn_write_back = QPushButton("R\u00fcckschreiben")
        # Auffällig, damit ein verschobenes Rückschreiben nicht untergeht
        self._btn_write_back.setStyleSheet(
            "padding: 8px 14px; font-size: 13px; font-weight: bold;"
            "background-color: #fdebd0; border: 2px solid #e67e22; border-radius: 4px;"
        )
        self._btn_write_back.clicked.connect(lambda: self._on_write_back())
        self._btn_write_back.hide()
        left_layout.addWidget(self._btn_write_back)

        main_splitter.addWidget(left)

        # --- Rechtes Panel: Vorschau + Log ---
        right_splitter = QSplitter(Qt.Orientation.Vertical)

        # Vorschau
        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self._lbl_preview = QLabel("Vorschau")
        self._lbl_preview.setStyleSheet("font-size: 13px; font-weight: bold;")
        preview_layout.addWidget(self._lbl_preview)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Kategorie / Sch\u00fcler", "Details"])
        self._tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tree.itemChanged.connect(self._on_tree_item_changed)
        preview_layout.addWidget(self._tree)
        right_splitter.addWidget(preview)

        # Log
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet("font-family: monospace; font-size: 12px;")
        right_splitter.addWidget(self._log)

        # Python-Logging → GUI-Log weiterleiten (thread-safe via Qt-Signal).
        # INFO-Level: DEBUG (z.B. jeder einzelne Graph-Request) bleibt in
        # spider.log, würde das GUI-Log aber unlesbar machen.
        # "gui" NICHT anhängen — die Worker senden ihre Meldungen bereits
        # direkt per log_signal, sonst erschiene alles doppelt.
        self._log_bridge = _LogSignalBridge()
        self._log_bridge.message.connect(self._log_msg)
        self._log_handler = _QtLogHandler(self._log_bridge)
        self._log_handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
        self._log_handler.setLevel(logging.INFO)
        for logger_name in ("core", "plugins", "adapters"):
            logging.getLogger(logger_name).addHandler(self._log_handler)

        right_splitter.setSizes([400, 200])
        main_splitter.addWidget(right_splitter)
        main_splitter.setSizes([260, 640])

        # Fortschrittsbalken
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.hide()
        root.addWidget(self._progress)

    # --- Settings ---

    def _load_settings(self) -> None:
        try:
            self._settings = load_settings()
            school = self._settings.get("school_name", "Unbekannt")
            self._lbl_school.setText(f"Schule: {school}")
            self._log_msg(f"Settings geladen. Schule: {school}")
        except FileNotFoundError:
            self._log_msg("settings.json nicht gefunden. Bitte konfigurieren.")

    def _open_settings_dialog(self) -> None:
        try:
            dlg = SettingsDialog(self._settings, parent=self)
            dlg.settings_changed.connect(self._on_settings_changed)
            dlg.exec()
        except Exception as exc:
            self._log_msg(f"FEHLER beim \u00d6ffnen der Einstellungen: {exc}")
            QMessageBox.critical(
                self,
                "Einstellungen konnten nicht ge\u00f6ffnet werden",
                f"Fehler: {exc}",
            )

    def _on_settings_changed(self) -> None:
        self._load_settings()
        self._populate_plugin_cards()

    # --- Plugin-Cards ---

    def _populate_plugin_cards(self) -> None:
        # Alte Cards entfernen
        for card in self._plugin_cards.values():
            card.setParent(None)
            card.deleteLater()
        self._plugin_cards.clear()
        self._selected_card_key = None

        plugins_cfg = self._settings.get("plugins", {})
        for key, config in plugins_cfg.items():
            if not config.get("enabled", False):
                continue
            plugin_class = get_plugin_class(key)
            if plugin_class is None:
                continue

            card = PluginCard(key, plugin_class.plugin_name())
            card.selected.connect(self._on_card_selected)
            card.compute_requested.connect(self._on_plugin_compute)
            card.apply_requested.connect(self._on_plugin_apply)
            self._plugin_stack_layout.addWidget(card)
            self._plugin_cards[key] = card

            # Falls bereits Quelldaten vorhanden
            if self._students or self._teachers:
                card.state = PluginCardState.DATA_LOADED

        # Erste Card automatisch auswählen
        if self._plugin_cards:
            first_key = next(iter(self._plugin_cards))
            self._on_card_selected(first_key)

    def _on_card_selected(self, plugin_key: str) -> None:
        # Alle deselektieren, gewählte selektieren
        for key, card in self._plugin_cards.items():
            card.set_selected(key == plugin_key)
        self._selected_card_key = plugin_key

        card = self._plugin_cards[plugin_key]
        self._lbl_preview.setText(f"Vorschau: {card.display_name}")
        self._refresh_preview()

    # --- Quelldaten laden ---

    def _on_load_data(self) -> None:
        if self._is_busy():
            return

        if self._keep_log_on_load:
            self._keep_log_on_load = False
        else:
            self._log.clear()
        self._students.clear()
        self._teachers.clear()
        self._pending_write_back.clear()
        self._btn_write_back.hide()
        self._email_suggestions = []
        self._btn_email_update.hide()
        self._lbl_counts.setText("")

        # Cards zurücksetzen
        for card in self._plugin_cards.values():
            card.state = PluginCardState.IDLE
            card.changeset = None
            card.excluded_ids = set()
        self._tree.clear()

        self._load_settings()
        self._populate_plugin_cards()

        self._btn_load.setEnabled(False)
        self._progress.show()

        thread = QThread()
        worker = LoadWorker(self._settings)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log_signal.connect(self._log_msg)
        worker.finished.connect(self._on_load_done)
        worker.error.connect(self._on_load_error)
        worker.email_check_ready.connect(self._on_email_check_ready)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)

        self._worker = worker
        self._worker_thread = thread
        thread.start()

    def _on_load_done(self, students: list, teachers: list) -> None:
        self._progress.hide()
        self._btn_load.setEnabled(True)
        self._students = students
        self._teachers = teachers

        parts = [f"{len(students)} SuS"]
        if teachers:
            parts.append(f"{len(teachers)} LuL")
        self._lbl_counts.setText(" \u00b7 ".join(parts))

        for card in self._plugin_cards.values():
            card.state = PluginCardState.DATA_LOADED

        if not self._plugin_cards:
            self._log_msg("Keine aktiven Plugins konfiguriert.")

    def _on_load_error(self, msg: str) -> None:
        self._progress.hide()
        self._btn_load.setEnabled(True)
        self._log_msg(f"\nFEHLER: {msg}")
        QMessageBox.critical(self, "Fehler beim Laden", msg)

    # --- Plugin: Berechnen ---

    def _on_plugin_compute(self, plugin_key: str) -> None:
        self._on_card_selected(plugin_key)
        if self._is_busy():
            return
        if not self._students and not self._teachers:
            self._log_msg("Keine Quelldaten geladen. Bitte zuerst 'Quelldaten laden'.")
            return

        card = self._plugin_cards[plugin_key]
        card.state = PluginCardState.COMPUTING
        card.excluded_ids = set()
        self._disable_all_actions()
        self._progress.show()

        plugin_class = get_plugin_class(plugin_key)
        plugin_config = self._settings.get("plugins", {}).get(plugin_key, {})
        plugin_instance = plugin_class.from_config(plugin_config)
        card.plugin_instance = plugin_instance

        # Filepicker für Plugins die eine Eingabe-Datei brauchen
        for req in plugin_instance.pre_compute_files():
            path, _ = QFileDialog.getOpenFileName(
                self, req["label"], "", req.get("filter", "")
            )
            if not path:
                card.state = PluginCardState.IDLE
                self._enable_all_actions()
                self._progress.hide()
                return
            setattr(plugin_instance, req["key"], path)

        max_suspend = self._settings.get("failsafe", {}).get(
            "max_suspend_percentage", 15.0
        )

        thread = QThread()
        worker = PluginComputeWorker(
            plugin_key,
            plugin_instance,
            self._students,
            max_suspend,
            teachers=self._teachers,
        )
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log_signal.connect(self._log_msg)
        worker.finished.connect(self._on_plugin_compute_done)
        worker.error.connect(self._on_plugin_worker_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(lambda k, m: thread.quit())

        self._worker = worker
        self._worker_thread = thread
        thread.start()

    def _on_plugin_compute_done(self, plugin_key: str, changeset: ChangeSet) -> None:
        self._progress.hide()
        self._enable_all_actions()

        card = self._plugin_cards[plugin_key]
        card.changeset = changeset
        card.state = PluginCardState.COMPUTED

        if changeset.requires_force:
            self._log_msg(
                f"\n\u26a0 FAILSAFE: {card.display_name} \u2014 "
                f"{changeset.suspend_percentage}% Abmeldungen! "
                f"Anwenden erfordert explizite Best\u00e4tigung."
            )

        has_changes = (
            changeset.new
            or changeset.changed
            or changeset.suspended
            or changeset.photo_updates
            or changeset.group_changes
        )
        if has_changes:
            self._log_msg("Vorschau bereit. Pr\u00fcfe die \u00c4nderungen.")
        else:
            self._log_msg("Keine \u00c4nderungen gefunden. Alles synchron.")

        if self._selected_card_key == plugin_key:
            self._refresh_preview()

    # --- Plugin: Anwenden ---

    def _on_plugin_apply(self, plugin_key: str) -> None:
        self._on_card_selected(plugin_key)
        if self._is_busy():
            return

        card = self._plugin_cards[plugin_key]
        if card.changeset is None:
            return

        # Erst filtern (abgew\u00e4hlte Eintr\u00e4ge raus), dann best\u00e4tigen \u2014
        # so z\u00e4hlt die Failsafe-Quote nur die tats\u00e4chlich anzuwendenden
        # Abmeldungen.
        filtered_cs = self._build_filtered_changeset(card)
        if not self._confirm_apply(card, filtered_cs):
            return

        card.state = PluginCardState.APPLYING
        self._disable_all_actions()
        self._progress.show()
        self._apply_had_error = False

        # Gecachte Plugin-Instanz aus Compute-Phase verwenden
        # (enthält Token, User-Cache, Gruppen-Cache etc.)
        plugin_instance = card.plugin_instance
        if plugin_instance is None:
            plugin_class = get_plugin_class(plugin_key)
            plugin_config = self._settings.get("plugins", {}).get(plugin_key, {})
            plugin_instance = plugin_class.from_config(plugin_config)

        thread = QThread()
        worker = PluginApplyWorker(plugin_key, plugin_instance, filtered_cs)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log_signal.connect(self._log_msg)
        worker.write_back_ready.connect(self._on_write_back_ready)
        worker.summary_ready.connect(self._on_apply_summary)
        worker.error.connect(self._on_plugin_worker_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(lambda k, m: thread.quit())
        # thread.finished statt worker.finished → Thread ist beendet
        # bevor QMessageBox den Event-Loop blockiert (verhindert Absturz).
        thread.finished.connect(lambda key=plugin_key: self._on_plugin_apply_done(key))

        self._worker = worker
        self._worker_thread = thread
        thread.start()

    def _on_apply_summary(self, plugin_key: str, ok: int, fail: int) -> None:
        """Empf\u00e4ngt die OK/Fehler-Z\u00e4hlung vom Apply-Worker."""
        self._apply_stats[plugin_key] = (ok, fail)

    def _on_plugin_apply_done(self, plugin_key: str) -> None:
        self._progress.hide()
        self._enable_all_actions()

        # Nach einem Abbruch (Exception) hat _on_plugin_worker_error bereits
        # den Fehlerdialog gezeigt — kein zusätzlicher "Fertig"-Dialog.
        if self._apply_had_error:
            return

        card = self._plugin_cards.get(plugin_key)
        if card:
            card.state = PluginCardState.APPLIED

        name = card.display_name if card else plugin_key
        ok, fail = self._apply_stats.pop(plugin_key, (0, 0))

        if fail:
            summary = (
                f"Synchronisation für {name} abgeschlossen: {ok} OK, {fail} Fehler."
            )
            self._log_msg(f"\n{summary}")
            body = f"{summary}\n\nDetails stehen im Log."
            icon = QMessageBox.Icon.Warning
            title = "Abgeschlossen mit Fehlern"
        else:
            summary = f"Synchronisation für {name} erfolgreich abgeschlossen."
            self._log_msg(f"\n{summary}")
            body = summary
            icon = QMessageBox.Icon.Information
            title = "Fertig"

        # Write-back-Daten (z.B. generierte Emails) direkt anbieten — der
        # Button unten wird sonst leicht übersehen.
        pending = len(self._pending_write_back)
        if pending and self._plugin_auto_write_back(plugin_key):
            body += (
                f"\n\n{pending} generierte Werte werden automatisch nach "
                f"SchILD zurückgeschrieben, danach werden die Quelldaten neu geladen."
            )
            box = QMessageBox(icon, title, body, QMessageBox.StandardButton.Ok, self)
            box.exec()
            self._on_write_back()
            return

        if pending:
            body += (
                f"\n\n{pending} generierte Werte (z.B. Email-Adressen) liegen vor.\n"
                f"Jetzt nach SchILD zurückschreiben? Die Quelldaten werden danach "
                f"automatisch neu geladen."
            )
            box = QMessageBox(
                icon,
                title,
                body,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                self,
            )
            box.setDefaultButton(QMessageBox.StandardButton.Yes)
            if box.exec() == QMessageBox.StandardButton.Yes:
                self._on_write_back()
            else:
                self._log_msg(
                    f"Rückschreiben verschoben — {pending} Werte warten "
                    f"(Button 'Rückschreiben' unten links)."
                )
            return

        QMessageBox(icon, title, body, QMessageBox.StandardButton.Ok, self).exec()

    def _plugin_auto_write_back(self, plugin_key: str) -> bool:
        """Liest die Plugin-Option 'auto_write_back' aus den Settings."""
        cfg = self._settings.get("plugins", {}).get(plugin_key, {})
        return as_bool(cfg.get("auto_write_back", False))

    # --- Vorschau-Tree mit Checkboxen ---

    def _refresh_preview(self) -> None:
        self._tree.blockSignals(True)
        self._tree.clear()

        if self._selected_card_key is None:
            self._lbl_preview.setText("Vorschau")
            self._tree.blockSignals(False)
            return

        card = self._plugin_cards.get(self._selected_card_key)
        if card is None or card.changeset is None:
            self._tree.blockSignals(False)
            return

        cs = card.changeset
        excluded = card.excluded_ids

        self._add_preview_category(
            "Neue Sch\u00fcler", cs.new, excluded, show_class=True
        )
        self._add_preview_category(
            "\u00c4nderungen", cs.changed, excluded, show_class=True
        )
        self._add_preview_suspend_category(
            "Abmeldungen", cs.suspended, excluded, cs.suspend_percentage
        )
        self._add_preview_category(
            "Foto-Updates", cs.photo_updates, excluded, detail="Neues Foto"
        )
        self._add_preview_group_category(
            "Klassengruppen (SuS)", cs.group_changes, excluded, "sus"
        )
        self._add_preview_group_category(
            "Lehrergruppen (KuK)", cs.group_changes, excluded, "kuk"
        )
        self._add_preview_group_category(
            "Kategorien", cs.group_changes, excluded, "category"
        )
        self._add_preview_group_category("Kurse", cs.group_changes, excluded, "course")

        if not (
            cs.new or cs.changed or cs.suspended or cs.photo_updates or cs.group_changes
        ):
            QTreeWidgetItem(self._tree, ["Keine \u00c4nderungen", "Alles synchron"])

        self._tree.blockSignals(False)

    def _add_preview_category(
        self,
        label: str,
        items: list[dict],
        excluded: set[str],
        show_class: bool = False,
        detail: str = "",
    ) -> None:
        if not items:
            return

        cat = QTreeWidgetItem(self._tree, [f"{label} ({len(items)})", ""])
        cat.setFlags(cat.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        cat.setExpanded(True)

        all_checked = True
        any_checked = False

        for s in items:
            sid = s["school_internal_id"]
            child = QTreeWidgetItem(cat)
            child.setText(0, f"{s['last_name']}, {s['first_name']}")
            if show_class:
                child.setText(1, self._describe_student_change(s))
            elif detail:
                child.setText(1, detail)
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            child.setData(0, Qt.ItemDataRole.UserRole, sid)

            if sid in excluded:
                child.setCheckState(0, Qt.CheckState.Unchecked)
                all_checked = False
            else:
                child.setCheckState(0, Qt.CheckState.Checked)
                any_checked = True

        if all_checked:
            cat.setCheckState(0, Qt.CheckState.Checked)
        elif any_checked:
            cat.setCheckState(0, Qt.CheckState.PartiallyChecked)
        else:
            cat.setCheckState(0, Qt.CheckState.Unchecked)

    @staticmethod
    def _describe_student_change(s: dict) -> str:
        """Detail-Text für einen Vorschau-Eintrag (neu oder geändert).

        Geändert: konkrete Unterschiede aus der Engine (``_diff``), z.B.
        "Email: alt → neu; Klasse: 10a → 10b". Neu: Klasse + Email, wobei
        eine vom Plugin generierte Adresse als "Email neu" markiert ist.
        """
        diff = s.get("_diff")
        if diff:
            text = "; ".join(diff)
            if not any(part.startswith("Klasse") for part in diff):
                text = f"Klasse: {s.get('class_name', '')} | {text}"
            return text

        info = f"Klasse: {s.get('class_name', '')}"
        email = (s.get("email") or "").strip()
        if email and s.get("_email_generated"):
            info += f" | Email neu: {email}"
        elif email:
            info += f" | {email}"
        else:
            info += " | keine Email"
        return info

    def _add_preview_suspend_category(
        self,
        label: str,
        ids: list[str],
        excluded: set[str],
        suspend_pct: float = 0.0,
    ) -> None:
        if not ids:
            return

        detail = f"{suspend_pct}%" if suspend_pct else ""
        cat = QTreeWidgetItem(self._tree, [f"{label} ({len(ids)})", detail])
        cat.setFlags(cat.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        cat.setExpanded(True)

        all_checked = True
        any_checked = False

        for sid in ids:
            child = QTreeWidgetItem(cat)
            child.setText(0, f"ID: {sid}")
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            child.setData(0, Qt.ItemDataRole.UserRole, sid)

            if sid in excluded:
                child.setCheckState(0, Qt.CheckState.Unchecked)
                all_checked = False
            else:
                child.setCheckState(0, Qt.CheckState.Checked)
                any_checked = True

        if all_checked:
            cat.setCheckState(0, Qt.CheckState.Checked)
        elif any_checked:
            cat.setCheckState(0, Qt.CheckState.PartiallyChecked)
        else:
            cat.setCheckState(0, Qt.CheckState.Unchecked)

    def _add_preview_group_category(
        self,
        label: str,
        all_changes: list[dict],
        excluded: set[str],
        group_type: str,
    ) -> None:
        """Zeigt Gruppen-Änderungen als 3-Level-Baum (Kategorie → Gruppe → Änderung)."""
        changes = [c for c in all_changes if c.get("group_type") == group_type]
        if not changes:
            return

        # Nach Klasse/Gruppe gruppieren
        from collections import OrderedDict

        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for c in changes:
            key = c.get("class_name", "")
            groups.setdefault(key, []).append(c)

        total_count = len(changes)
        cat = QTreeWidgetItem(self._tree, [f"{label} ({total_count})", ""])
        cat.setFlags(cat.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        cat.setExpanded(True)

        cat_all_checked = True
        cat_any_checked = False

        for class_name, group_changes in groups.items():
            group_name = group_changes[0].get("group_name", class_name)
            is_new = any(c["action"] == "create_group" for c in group_changes)
            member_changes = [c for c in group_changes if c["action"] != "create_group"]

            detail = "NEU" if is_new else ""
            if member_changes:
                n = len(member_changes)
                detail += f" + {n}" if detail else str(n)
                detail += " Änderung" if n == 1 else " Änderungen"

            group_item = QTreeWidgetItem(cat, [group_name, detail])
            group_item.setFlags(group_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            group_item.setExpanded(True)

            grp_all_checked = True
            grp_any_checked = False

            for c in group_changes:
                child = QTreeWidgetItem(group_item)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setData(0, Qt.ItemDataRole.UserRole, c["id"])

                if "display_text" in c:
                    child.setText(0, c["display_text"])
                    if c.get("display_detail"):
                        child.setText(1, c["display_detail"])
                elif c["action"] == "create_group":
                    child.setText(0, "Gruppe anlegen")
                elif c["action"] == "add_member":
                    child.setText(0, f"{c['member_name']}")
                    child.setText(1, "hinzufügen")
                elif c["action"] == "remove_member":
                    child.setText(0, f"{c['member_name']}")
                    child.setText(1, "entfernen")

                if c["id"] in excluded:
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                    grp_all_checked = False
                else:
                    child.setCheckState(0, Qt.CheckState.Checked)
                    grp_any_checked = True

            if grp_all_checked:
                group_item.setCheckState(0, Qt.CheckState.Checked)
            elif grp_any_checked:
                group_item.setCheckState(0, Qt.CheckState.PartiallyChecked)
            else:
                group_item.setCheckState(0, Qt.CheckState.Unchecked)

            if grp_all_checked:
                cat_any_checked = True
            elif grp_any_checked:
                cat_any_checked = True
                cat_all_checked = False
            else:
                cat_all_checked = False

        if cat_all_checked:
            cat.setCheckState(0, Qt.CheckState.Checked)
        elif cat_any_checked:
            cat.setCheckState(0, Qt.CheckState.PartiallyChecked)
        else:
            cat.setCheckState(0, Qt.CheckState.Unchecked)

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return

        self._tree.blockSignals(True)

        parent = item.parent()
        grandparent = parent.parent() if parent else None

        if parent is None:
            # Top-Level (Kategorie) → an Kinder + Enkel propagieren
            state = item.checkState(0)
            if state != Qt.CheckState.PartiallyChecked:
                for i in range(item.childCount()):
                    child = item.child(i)
                    child.setCheckState(0, state)
                    for j in range(child.childCount()):
                        child.child(j).setCheckState(0, state)
        elif grandparent is None:
            # Level 2 (Gruppe oder Schüler-Eintrag)
            state = item.checkState(0)
            if state != Qt.CheckState.PartiallyChecked:
                # Enkel propagieren (nur bei Gruppen-Items mit Kindern)
                for i in range(item.childCount()):
                    item.child(i).setCheckState(0, state)
            # Eltern-Tristate aktualisieren
            self._update_tristate(parent)
        else:
            # Level 3 (Blatt: einzelne Gruppenänderung)
            # Eltern-Gruppe aktualisieren
            self._update_tristate(parent)
            # Großeltern-Kategorie aktualisieren
            self._update_tristate(grandparent)

        self._tree.blockSignals(False)
        self._sync_exclusions_from_tree()

    @staticmethod
    def _update_tristate(item: QTreeWidgetItem) -> None:
        """Aktualisiert den Check-Zustand eines Eltern-Items basierend auf seinen Kindern."""
        checked = 0
        partial = 0
        total = item.childCount()
        for i in range(total):
            state = item.child(i).checkState(0)
            if state == Qt.CheckState.Checked:
                checked += 1
            elif state == Qt.CheckState.PartiallyChecked:
                partial += 1
        if checked == total:
            item.setCheckState(0, Qt.CheckState.Checked)
        elif checked == 0 and partial == 0:
            item.setCheckState(0, Qt.CheckState.Unchecked)
        else:
            item.setCheckState(0, Qt.CheckState.PartiallyChecked)

    def _sync_exclusions_from_tree(self) -> None:
        if self._selected_card_key is None:
            return

        card = self._plugin_cards.get(self._selected_card_key)
        if card is None:
            return

        excluded: set[str] = set()
        for cat_idx in range(self._tree.topLevelItemCount()):
            cat_item = self._tree.topLevelItem(cat_idx)
            for child_idx in range(cat_item.childCount()):
                child = cat_item.child(child_idx)
                sid = child.data(0, Qt.ItemDataRole.UserRole)
                if sid and child.checkState(0) == Qt.CheckState.Unchecked:
                    excluded.add(sid)
                # Enkel scannen (Level 3, für Gruppen-Änderungen)
                for gc_idx in range(child.childCount()):
                    gc = child.child(gc_idx)
                    gc_id = gc.data(0, Qt.ItemDataRole.UserRole)
                    if gc_id and gc.checkState(0) == Qt.CheckState.Unchecked:
                        excluded.add(gc_id)

        card.excluded_ids = excluded

    # --- Write-back ---

    def _on_write_back_ready(self, plugin_key: str, data: list) -> None:
        """Empfängt Write-back-Daten vom Apply-Worker und speichert sie.

        Dedupliziert nach (ID, Email) — mehrfaches Anwenden ohne
        zwischenzeitliches Rückschreiben erzeugt sonst doppelte Einträge.
        """
        seen = {
            (d.get("school_internal_id"), d.get("email"))
            for d in self._pending_write_back
        }
        for item in data:
            key = (item.get("school_internal_id"), item.get("email"))
            if key not in seen:
                self._pending_write_back.append(item)
                seen.add(key)

        count = len(self._pending_write_back)
        self._btn_write_back.setText(f"R\u00fcckschreiben ({count})")
        self._btn_write_back.show()

    def _on_write_back(self, *, reload: bool = True) -> None:
        """Schreibt die gesammelten Daten über den Adapter zurück.

        Danach werden die Quelldaten neu geladen (reload=True), damit der
        nächste Sync die zurückgeschriebenen Werte sieht. Beim Schließen
        des Fensters (closeEvent) wird reload=False übergeben — ein
        laufender Load-Thread beim Beenden wäre ein Absturzrisiko.
        """
        if not self._pending_write_back:
            return
        if (
            self._perform_write_back(self._pending_write_back, "generierte Werte")
            is not None
        ):
            self._pending_write_back.clear()
            self._btn_write_back.hide()
            if reload:
                self._reload_source_data()

    def _reload_source_data(self) -> None:
        """Quelldaten neu laden, ohne das Log zu leeren (nach Write-back)."""
        if self._is_busy():
            return
        self._keep_log_on_load = True
        self._log_msg("\n--- Quelldaten werden neu geladen ---")
        self._on_load_data()

    def _perform_write_back(
        self, updates: list[dict], label: str
    ) -> tuple[int, int] | None:
        """Schreibt Updates über den Adapter zurück und loggt das Ergebnis.

        Returns (ok, fehler), oder None wenn der Write-back gar nicht
        möglich war (Adapter ohne Write-back, Exception).
        """
        try:
            adapter = load_adapter(self._settings)
            if not adapter.supports_write_back():
                self._log_msg(
                    "Adapter unterstützt kein Write-back. "
                    "Daten im Log oben manuell übertragen."
                )
                return None

            self._log_msg(f"\nSchreibe {len(updates)} {label} zurück...")
            results = adapter.write_back(updates)
            ok = sum(1 for r in results if r.get("success"))
            fail = len(results) - ok
            self._log_msg(f"Write-back: {ok} OK, {fail} Fehler")
            for r in results:
                if not r.get("success"):
                    sid = r.get("school_internal_id", "?")
                    self._log_msg(f"  ✗ {sid}: {r.get('message', '')}")

            # Dateipfad anzeigen (CSV-Adapter gibt Pfad in message zurück)
            for r in results:
                msg = r.get("message", "")
                if msg and r.get("success") and ("/" in msg or "\\" in msg):
                    self._log_msg(f"Exportiert nach: {msg}")
                    break
            return ok, fail

        except Exception as exc:
            self._log_msg(f"Write-back Fehler: {exc}")
            QMessageBox.critical(self, "Write-back Fehler", str(exc))
            return None

    # --- Email-Korrekturen (Klassenwechsel) ---

    def _on_email_check_ready(self, auto_applied: list, suggestions: list) -> None:
        """Empfängt das Ergebnis der Email-Prüfung vom LoadWorker.

        auto_applied: bereits im Datensatz korrigierte Adressen
            (Klassenwechsel) — müssen nur noch nach SchILD zurück.
        suggestions: mehrdeutige Fälle für den Auswahl-Dialog.
        """
        if auto_applied:
            self._on_write_back_ready("", list(auto_applied))

        self._email_suggestions = list(suggestions)
        n = len(self._email_suggestions)
        if n:
            self._btn_email_update.setText(f"Email-Adressen aktualisieren ({n})")
            self._btn_email_update.show()
        else:
            self._btn_email_update.hide()

    def _on_email_update(self) -> None:
        """Dialog mit den Vorschlägen; ausgewählte Adressen nach SchILD schreiben."""
        if not self._email_suggestions or self._is_busy():
            return

        dlg = EmailUpdateDialog(self._email_suggestions, parent=self)
        if dlg.exec() != EmailUpdateDialog.DialogCode.Accepted:
            return
        updates = dlg.selected_updates()
        if not updates:
            return

        for u in updates:
            self._log_msg(
                f"  {u.get('class_name', '')}: {u.get('last_name', '')}, "
                f"{u.get('first_name', '')}: {u.get('old_email', '')} "
                f"→ {u.get('email', '')}"
            )
        outcome = self._perform_write_back(updates, "Email-Adressen")
        if outcome is None:
            return
        ok, fail = outcome

        QMessageBox.information(
            self,
            "Email-Adressen aktualisiert",
            f"{ok} Adressen in SchILD aktualisiert"
            + (f", {fail} Fehler (siehe Log)" if fail else "")
            + ".\n\nDie Quelldaten werden jetzt neu geladen. Danach im "
            'M365-Plugin "Berechnen" → "Anwenden" ausführen, um die '
            "Adressen auch in Microsoft 365 zu ändern.",
        )

        # Quelldaten neu laden, damit die neuen Adressen in den Records
        # stehen — danach zeigt M365 "Berechnen" die Schüler als geändert
        # und "Anwenden" setzt den neuen UPN.
        self._reload_source_data()

    # --- Close-Event ---

    def closeEvent(self, event) -> None:
        """Warnt bei ausstehenden Write-back-Daten."""
        if self._pending_write_back:
            reply = QMessageBox.warning(
                self,
                "Nicht zur\u00fcckgeschriebene Daten",
                f"Es liegen {len(self._pending_write_back)} generierte Werte "
                f"vor, die noch nicht zur\u00fcckgeschrieben wurden.\n\n"
                f"Jetzt r\u00fcckschreiben?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._on_write_back(reload=False)
                event.accept()
            elif reply == QMessageBox.StandardButton.Discard:
                event.accept()
            else:
                event.ignore()
                return
        super().closeEvent(event)

    # --- Helpers ---

    def _confirm_apply(self, card: PluginCard, filtered_cs: ChangeSet) -> bool:
        """Bestätigungs-Dialoge vor dem Anwenden, inkl. Failsafe-Override.

        Die Failsafe-Quote wird über das GEFILTERTE ChangeSet berechnet —
        wer Abmeldungen in der Vorschau abwählt, senkt die Quote.
        Oberhalb von max_suspend_percentage ist eine explizite Bestätigung
        nötig, oberhalb von require_confirmation_above eine doppelte.
        """
        failsafe = self._settings.get("failsafe", {})
        max_suspend = float(failsafe.get("max_suspend_percentage", 15))
        confirm_above = float(failsafe.get("require_confirmation_above", 50))

        total_target = filtered_cs.total_in_target
        n_suspend = len(filtered_cs.suspended)
        eff_pct = (n_suspend / total_target * 100) if total_target else 0.0

        if eff_pct > max_suspend:
            reply = QMessageBox.warning(
                self,
                "Failsafe: Viele Abmeldungen",
                f"{n_suspend} Abmeldungen = {eff_pct:.1f}% der Konten im "
                f"Zielsystem (Schwellwert: {max_suspend:.0f}%).\n\n"
                f"Das kann auf einen unvollständigen SchILD-Export "
                f"hindeuten — bitte prüfe die Quelldaten.\n\n"
                f"Änderungen für '{card.display_name}' trotzdem anwenden?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False

            if eff_pct > confirm_above:
                reply = QMessageBox.critical(
                    self,
                    "Failsafe: Bitte noch einmal bestätigen",
                    f"Es würden {n_suspend} von {total_target} Konten "
                    f"deaktiviert ({eff_pct:.1f}%).\n\n"
                    f"Wirklich fortfahren?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return False

            return True

        reply = QMessageBox.question(
            self,
            "Änderungen anwenden?",
            f"Sollen die Änderungen für '{card.display_name}' jetzt angewendet werden?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _build_filtered_changeset(self, card: PluginCard) -> ChangeSet:
        cs = card.changeset
        excluded = card.excluded_ids
        return ChangeSet(
            new=[s for s in cs.new if s["school_internal_id"] not in excluded],
            changed=[s for s in cs.changed if s["school_internal_id"] not in excluded],
            suspended=[sid for sid in cs.suspended if sid not in excluded],
            photo_updates=[
                s for s in cs.photo_updates if s["school_internal_id"] not in excluded
            ],
            group_changes=[g for g in cs.group_changes if g["id"] not in excluded],
            total_in_source=cs.total_in_source,
            total_in_target=cs.total_in_target,
            suspend_percentage=cs.suspend_percentage,
            requires_force=cs.requires_force,
        )

    def _is_busy(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.isRunning()

    def _disable_all_actions(self) -> None:
        self._btn_load.setEnabled(False)
        self._btn_settings.setEnabled(False)
        for card in self._plugin_cards.values():
            card.set_buttons_enabled(False)

    def _enable_all_actions(self) -> None:
        self._btn_load.setEnabled(True)
        self._btn_settings.setEnabled(True)
        for card in self._plugin_cards.values():
            card.refresh_buttons()

    def _on_plugin_worker_error(self, plugin_key: str, msg: str) -> None:
        self._apply_had_error = True
        self._progress.hide()
        self._enable_all_actions()

        card = self._plugin_cards.get(plugin_key)
        if card:
            if card.state == PluginCardState.COMPUTING:
                card.state = PluginCardState.DATA_LOADED
            elif card.state == PluginCardState.APPLYING:
                card.state = PluginCardState.COMPUTED

        self._log_msg(f"\nFEHLER ({plugin_key}): {msg}")
        QMessageBox.critical(self, "Fehler", msg)

    def _log_msg(self, msg: str) -> None:
        self._log.append(msg)
