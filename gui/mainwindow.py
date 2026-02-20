from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.engine import compute_changeset
from core.models import ChangeSet
from core.plugin_loader import load_adapter, load_plugins, load_settings
from gui.settings_dialog import SettingsDialog


# ---------------------------------------------------------------------------
# Worker für Hintergrund-Operationen
# ---------------------------------------------------------------------------


class ComputeWorker(QObject):
    """Berechnet ChangeSets in einem Hintergrund-Thread."""

    finished = Signal(dict)  # {plugin_name: ChangeSet}
    error = Signal(str)
    log = Signal(str)

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self.settings = settings

    @Slot()
    def run(self) -> None:
        try:
            self.log.emit("Lade Schülerdaten...")
            adapter = load_adapter(self.settings)
            students = adapter.load()
            self.log.emit(f"{len(students)} Schüler geladen.")

            self.log.emit("Lade aktive Plugins...")
            plugins = load_plugins(self.settings)
            if not plugins:
                self.error.emit("Keine aktiven Plugins konfiguriert.")
                return

            max_suspend = self.settings.get("failsafe", {}).get(
                "max_suspend_percentage", 15.0
            )

            results: dict[str, ChangeSet] = {}
            for name, plugin in plugins:
                self.log.emit(f"Berechne ChangeSet für {name}...")
                cs = compute_changeset(students, plugin, max_suspend)
                results[name] = cs
                self.log.emit(
                    f"  {name}: {len(cs.new)} neu, {len(cs.changed)} geändert, "
                    f"{len(cs.suspended)} abgemeldet, {len(cs.photo_updates)} Foto-Updates"
                )

            self.finished.emit(results)

        except Exception as exc:
            self.error.emit(str(exc))


class ApplyWorker(QObject):
    """Wendet ChangeSets auf Plugins an."""

    finished = Signal()
    error = Signal(str)
    log = Signal(str)

    def __init__(
        self, settings: dict, changesets: dict[str, ChangeSet]
    ) -> None:
        super().__init__()
        self.settings = settings
        self.changesets = changesets

    @Slot()
    def run(self) -> None:
        try:
            plugins = load_plugins(self.settings)
            plugin_map = {name: plugin for name, plugin in plugins}

            for name, cs in self.changesets.items():
                plugin = plugin_map.get(name)
                if not plugin:
                    self.log.emit(f"Plugin {name} nicht gefunden, überspringe.")
                    continue

                self.log.emit(f"\n--- {name} ---")

                if cs.new:
                    self.log.emit(f"Lege {len(cs.new)} neue Schüler an...")
                    results = plugin.apply_new(cs.new)
                    self.log.emit(f"  Ergebnis: {len(results)} verarbeitet")

                if cs.changed:
                    self.log.emit(
                        f"Aktualisiere {len(cs.changed)} Schüler..."
                    )
                    results = plugin.apply_changes(cs.changed)
                    self.log.emit(f"  Ergebnis: {len(results)} verarbeitet")

                if cs.photo_updates:
                    self.log.emit(
                        f"Aktualisiere {len(cs.photo_updates)} Fotos..."
                    )
                    results = plugin.apply_changes(cs.photo_updates)
                    self.log.emit(f"  Ergebnis: {len(results)} verarbeitet")

                if cs.suspended:
                    self.log.emit(
                        f"Deaktiviere {len(cs.suspended)} Schüler..."
                    )
                    results = plugin.apply_suspend(cs.suspended)
                    self.log.emit(f"  Ergebnis: {len(results)} verarbeitet")

            self.log.emit("\nSynchronisation abgeschlossen.")
            self.finished.emit()

        except Exception as exc:
            self.error.emit(str(exc))


# ---------------------------------------------------------------------------
# Hauptfenster
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Schild Spider")
        self.setMinimumSize(900, 600)

        self._settings: dict = {}
        self._changesets: dict[str, ChangeSet] = {}
        self._worker_thread: QThread | None = None

        self._build_ui()
        self._load_settings()

    # --- UI Aufbau ---

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Header
        header = QHBoxLayout()
        self._lbl_school = QLabel("Schule: –")
        self._lbl_school.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(self._lbl_school)
        header.addStretch()

        self._btn_settings = QPushButton("Einstellungen...")
        self._btn_settings.clicked.connect(self._open_settings_dialog)
        header.addWidget(self._btn_settings)
        layout.addLayout(header)

        # Splitter: Tree links, Log rechts
        splitter = QSplitter()
        layout.addWidget(splitter, stretch=1)

        # Vorschau-Tree
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Kategorie / Schüler", "Details"])
        self._tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        splitter.addWidget(self._tree)

        # Log-Bereich
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setStyleSheet("font-family: monospace; font-size: 12px;")
        splitter.addWidget(self._log)

        splitter.setSizes([500, 400])

        # Fortschrittsbalken
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # Indeterminate
        self._progress.hide()
        layout.addWidget(self._progress)

        # Buttons
        btn_layout = QHBoxLayout()
        self._btn_compute = QPushButton("Änderungen berechnen")
        self._btn_compute.setStyleSheet(
            "padding: 8px 16px; font-size: 13px;"
        )
        self._btn_compute.clicked.connect(self._on_compute)
        btn_layout.addWidget(self._btn_compute)

        self._btn_apply = QPushButton("Änderungen anwenden")
        self._btn_apply.setStyleSheet(
            "padding: 8px 16px; font-size: 13px;"
        )
        self._btn_apply.setEnabled(False)
        self._btn_apply.clicked.connect(self._on_apply)
        btn_layout.addWidget(self._btn_apply)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

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
        """Öffnet den Settings-Dialog mit Plugin-Manager."""
        dlg = SettingsDialog(self._settings, parent=self)
        dlg.settings_changed.connect(self._load_settings)
        dlg.exec()

    # --- Phase 1: Berechnen ---

    def _on_compute(self) -> None:
        self._log.clear()
        self._tree.clear()
        self._changesets.clear()
        self._btn_apply.setEnabled(False)
        self._btn_compute.setEnabled(False)
        self._progress.show()

        # Settings neu laden
        self._load_settings()

        thread = QThread()
        worker = ComputeWorker(self._settings)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log.connect(self._log_msg)
        worker.finished.connect(lambda r: self._on_compute_done(r))
        worker.error.connect(self._on_worker_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)

        self._worker_thread = thread
        thread.start()

    def _on_compute_done(self, results: dict) -> None:
        self._progress.hide()
        self._btn_compute.setEnabled(True)
        self._changesets = results
        self._populate_tree()

        has_failsafe = any(cs.requires_force for cs in results.values())
        has_changes = any(
            cs.new or cs.changed or cs.suspended or cs.photo_updates
            for cs in results.values()
        )

        if has_failsafe:
            self._log_msg(
                "\n⚠ FAILSAFE: Mehr als 15% der Schüler würden deaktiviert! "
                "Anwenden blockiert."
            )
            QMessageBox.warning(
                self,
                "Failsafe-Warnung",
                "Mehr als 15% der Schüler im Zielsystem würden deaktiviert.\n\n"
                "Die Synchronisation wurde blockiert. Bitte prüfe die "
                "SchILD-Daten auf Vollständigkeit.",
            )
        elif has_changes:
            self._btn_apply.setEnabled(True)
            self._log_msg("\nVorschau bereit. Prüfe die Änderungen und klicke 'Anwenden'.")
        else:
            self._log_msg("\nKeine Änderungen gefunden. Alles synchron.")

    # --- Phase 2: Vorschau ---

    def _populate_tree(self) -> None:
        self._tree.clear()

        for plugin_name, cs in self._changesets.items():
            plugin_item = QTreeWidgetItem(self._tree, [plugin_name, ""])
            plugin_item.setExpanded(True)

            # Neue Schüler
            if cs.new:
                cat = QTreeWidgetItem(
                    plugin_item,
                    [f"Neue Schüler ({len(cs.new)})", ""],
                )
                cat.setExpanded(False)
                for s in cs.new:
                    QTreeWidgetItem(
                        cat,
                        [
                            f"{s['last_name']}, {s['first_name']}",
                            f"Klasse: {s['class_name']}",
                        ],
                    )

            # Änderungen
            if cs.changed:
                cat = QTreeWidgetItem(
                    plugin_item,
                    [f"Änderungen ({len(cs.changed)})", ""],
                )
                cat.setExpanded(False)
                for s in cs.changed:
                    QTreeWidgetItem(
                        cat,
                        [
                            f"{s['last_name']}, {s['first_name']}",
                            f"Klasse: {s['class_name']}",
                        ],
                    )

            # Abmeldungen
            if cs.suspended:
                cat = QTreeWidgetItem(
                    plugin_item,
                    [
                        f"Abmeldungen ({len(cs.suspended)})",
                        f"{cs.suspend_percentage}%",
                    ],
                )
                cat.setExpanded(False)
                for sid in cs.suspended:
                    QTreeWidgetItem(cat, [f"ID: {sid}", ""])

            # Foto-Updates
            if cs.photo_updates:
                cat = QTreeWidgetItem(
                    plugin_item,
                    [f"Foto-Updates ({len(cs.photo_updates)})", ""],
                )
                cat.setExpanded(False)
                for s in cs.photo_updates:
                    QTreeWidgetItem(
                        cat,
                        [
                            f"{s['last_name']}, {s['first_name']}",
                            "Neues Foto",
                        ],
                    )

            # Keine Änderungen
            if not (cs.new or cs.changed or cs.suspended or cs.photo_updates):
                QTreeWidgetItem(
                    plugin_item, ["Keine Änderungen", "Alles synchron"]
                )

    # --- Phase 3: Anwenden ---

    def _on_apply(self) -> None:
        reply = QMessageBox.question(
            self,
            "Änderungen anwenden?",
            "Sollen die berechneten Änderungen jetzt auf die Zielsysteme "
            "angewendet werden?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._btn_apply.setEnabled(False)
        self._btn_compute.setEnabled(False)
        self._progress.show()

        thread = QThread()
        worker = ApplyWorker(self._settings, self._changesets)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.log.connect(self._log_msg)
        worker.finished.connect(lambda: self._on_apply_done())
        worker.error.connect(self._on_worker_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)

        self._worker_thread = thread
        thread.start()

    def _on_apply_done(self) -> None:
        self._progress.hide()
        self._btn_compute.setEnabled(True)
        self._log_msg("\nAlle Änderungen wurden angewendet.")
        QMessageBox.information(
            self,
            "Fertig",
            "Synchronisation erfolgreich abgeschlossen.",
        )

    # --- Helpers ---

    def _on_worker_error(self, msg: str) -> None:
        self._progress.hide()
        self._btn_compute.setEnabled(True)
        self._btn_apply.setEnabled(False)
        self._log_msg(f"\nFEHLER: {msg}")
        QMessageBox.critical(self, "Fehler", msg)

    def _log_msg(self, msg: str) -> None:
        self._log.append(msg)
