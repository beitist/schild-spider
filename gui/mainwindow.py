from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
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
from core.plugin_loader import get_plugin_class, load_adapter, load_settings
from gui.plugin_card import PluginCard, PluginCardState
from gui.settings_dialog import SettingsDialog
from gui.workers import LoadWorker, PluginApplyWorker, PluginComputeWorker


# ---------------------------------------------------------------------------
# Log-Handler → GUI
# ---------------------------------------------------------------------------


class _QtLogHandler(logging.Handler):
    """Leitet Python-Log-Einträge an ein QTextEdit weiter."""

    def __init__(self, callback) -> None:
        super().__init__()
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self._callback(msg)


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
        self._btn_write_back.setStyleSheet(
            "padding: 6px 14px; font-size: 13px; font-weight: bold;"
        )
        self._btn_write_back.clicked.connect(self._on_write_back)
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

        # Python-Logging → GUI-Log weiterleiten
        self._log_handler = _QtLogHandler(self._log_msg)
        self._log_handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
        self._log_handler.setLevel(logging.DEBUG)
        logging.getLogger("core").addHandler(self._log_handler)

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
            if self._students:
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

        self._log.clear()
        self._students.clear()
        self._teachers.clear()
        self._pending_write_back.clear()
        self._btn_write_back.hide()
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
        worker.log.connect(self._log_msg)
        worker.finished.connect(self._on_load_done)
        worker.error.connect(self._on_load_error)
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
        if not self._students:
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

        max_suspend = self._settings.get("failsafe", {}).get(
            "max_suspend_percentage", 15.0
        )

        thread = QThread()
        worker = PluginComputeWorker(
            plugin_key, plugin_instance, self._students, max_suspend
        )
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log.connect(self._log_msg)
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
                f"{changeset.suspend_percentage}% Abmeldungen! Anwenden blockiert."
            )

        has_changes = (
            changeset.new
            or changeset.changed
            or changeset.suspended
            or changeset.photo_updates
        )
        if has_changes and not changeset.requires_force:
            self._log_msg("Vorschau bereit. Pr\u00fcfe die \u00c4nderungen.")
        elif not has_changes:
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
        if card.changeset.requires_force:
            QMessageBox.warning(
                self,
                "Failsafe",
                f"Abmeldungen \u00fcberschreiten den Schwellwert "
                f"({card.changeset.suspend_percentage}%).\n\n"
                f"Bitte pr\u00fcfe die SchILD-Daten auf Vollst\u00e4ndigkeit.",
            )
            return

        reply = QMessageBox.question(
            self,
            "\u00c4nderungen anwenden?",
            f"Sollen die \u00c4nderungen f\u00fcr "
            f"'{card.display_name}' jetzt angewendet werden?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        card.state = PluginCardState.APPLYING
        self._disable_all_actions()
        self._progress.show()

        filtered_cs = self._build_filtered_changeset(card)

        plugin_class = get_plugin_class(plugin_key)
        plugin_config = self._settings.get("plugins", {}).get(plugin_key, {})
        plugin_instance = plugin_class.from_config(plugin_config)

        thread = QThread()
        worker = PluginApplyWorker(plugin_key, plugin_instance, filtered_cs)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log.connect(self._log_msg)
        worker.write_back_ready.connect(self._on_write_back_ready)
        worker.error.connect(self._on_plugin_worker_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(lambda k, m: thread.quit())
        # thread.finished statt worker.finished → Thread ist beendet
        # bevor QMessageBox den Event-Loop blockiert (verhindert Absturz).
        thread.finished.connect(lambda key=plugin_key: self._on_plugin_apply_done(key))

        self._worker = worker
        self._worker_thread = thread
        thread.start()

    def _on_plugin_apply_done(self, plugin_key: str) -> None:
        self._progress.hide()
        self._enable_all_actions()

        card = self._plugin_cards.get(plugin_key)
        if card:
            card.state = PluginCardState.APPLIED

        name = card.display_name if card else plugin_key
        self._log_msg(f"\nSynchronisation f\u00fcr {name} abgeschlossen.")
        QMessageBox.information(
            self,
            "Fertig",
            f"Synchronisation f\u00fcr {name} erfolgreich abgeschlossen.",
        )

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

        if not (cs.new or cs.changed or cs.suspended or cs.photo_updates):
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
                email = (s.get("email") or "").strip()
                info = f"Klasse: {s['class_name']}"
                if email:
                    info += f" | {email}"
                child.setText(1, info)
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

    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return

        self._tree.blockSignals(True)

        parent = item.parent()
        if parent is None:
            # Kategorie-Item → Zustand an alle Kinder propagieren
            state = item.checkState(0)
            if state != Qt.CheckState.PartiallyChecked:
                for i in range(item.childCount()):
                    item.child(i).setCheckState(0, state)
        else:
            # Kind-Item → Eltern-Tristate aktualisieren
            checked = sum(
                1
                for i in range(parent.childCount())
                if parent.child(i).checkState(0) == Qt.CheckState.Checked
            )
            total = parent.childCount()
            if checked == 0:
                parent.setCheckState(0, Qt.CheckState.Unchecked)
            elif checked == total:
                parent.setCheckState(0, Qt.CheckState.Checked)
            else:
                parent.setCheckState(0, Qt.CheckState.PartiallyChecked)

        self._tree.blockSignals(False)
        self._sync_exclusions_from_tree()

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

        card.excluded_ids = excluded

    # --- Write-back ---

    def _on_write_back_ready(self, plugin_key: str, data: list) -> None:
        """Empfängt Write-back-Daten vom Apply-Worker und speichert sie."""
        self._pending_write_back.extend(data)
        count = len(self._pending_write_back)
        self._btn_write_back.setText(f"R\u00fcckschreiben ({count})")
        self._btn_write_back.show()

    def _on_write_back(self) -> None:
        """Schreibt die gesammelten Daten \u00fcber den Adapter zur\u00fcck."""
        if not self._pending_write_back:
            return

        try:
            adapter = load_adapter(self._settings)
            if not adapter.supports_write_back():
                self._log_msg(
                    "Adapter unterst\u00fctzt kein Write-back. "
                    "Daten im Log oben manuell \u00fcbertragen."
                )
                return

            count = len(self._pending_write_back)
            self._log_msg(f"\nSchreibe {count} generierte Werte zur\u00fcck...")
            results = adapter.write_back(self._pending_write_back)
            ok = sum(1 for r in results if r.get("success"))
            fail = len(results) - ok
            self._log_msg(f"Write-back: {ok} OK, {fail} Fehler")

            # Dateipfad anzeigen (CSV-Adapter gibt Pfad in message zur\u00fcck)
            for r in results:
                msg = r.get("message", "")
                if msg and r.get("success") and ("/" in msg or "\\" in msg):
                    self._log_msg(f"Exportiert nach: {msg}")
                    break

            self._pending_write_back.clear()
            self._btn_write_back.hide()

        except Exception as exc:
            self._log_msg(f"Write-back Fehler: {exc}")
            QMessageBox.critical(self, "Write-back Fehler", str(exc))

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
                self._on_write_back()
                event.accept()
            elif reply == QMessageBox.StandardButton.Discard:
                event.accept()
            else:
                event.ignore()
                return
        super().closeEvent(event)

    # --- Helpers ---

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
