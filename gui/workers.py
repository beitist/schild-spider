from __future__ import annotations

import warnings

from PySide6.QtCore import QObject, Signal, Slot

from core.engine import compute_changeset
from core.models import ChangeSet
from core.plugin_loader import load_adapter
from plugins.base import PluginBase


class LoadWorker(QObject):
    """Lädt Schüler- und Lehrerdaten vom konfigurierten Adapter."""

    finished = Signal(list, list)  # (students, teachers)
    error = Signal(str)
    log = Signal(str)

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self.settings = settings

    @Slot()
    def run(self) -> None:
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")

                self.log.emit("Lade Schülerdaten...")
                adapter = load_adapter(self.settings)
                students = adapter.load()
                self.log.emit(f"{len(students)} Schüler geladen.")

                teachers = adapter.load_teachers()
                if teachers:
                    self.log.emit(f"{len(teachers)} Lehrer geladen.")
                else:
                    self.log.emit("Keine Lehrerdaten (CSV nicht konfiguriert).")

                for w in caught:
                    self.log.emit(f"⚠ {w.message}")

            self.finished.emit(students, teachers)

        except Exception as exc:
            self.error.emit(str(exc))


class PluginComputeWorker(QObject):
    """Berechnet ein ChangeSet für ein einzelnes Plugin."""

    finished = Signal(str, ChangeSet)  # (plugin_key, changeset)
    error = Signal(str, str)  # (plugin_key, error_message)
    log = Signal(str)

    def __init__(
        self,
        plugin_key: str,
        plugin: PluginBase,
        students: list,
        max_suspend: float,
    ) -> None:
        super().__init__()
        self.plugin_key = plugin_key
        self.plugin = plugin
        self.students = students
        self.max_suspend = max_suspend

    @Slot()
    def run(self) -> None:
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")

                self.log.emit(f"Berechne ChangeSet für {self.plugin_key}...")
                cs = compute_changeset(self.students, self.plugin, self.max_suspend)
                self.log.emit(
                    f"  {self.plugin_key}: {len(cs.new)} neu, "
                    f"{len(cs.changed)} geändert, "
                    f"{len(cs.suspended)} abgemeldet, "
                    f"{len(cs.photo_updates)} Foto-Updates"
                )

                for w in caught:
                    self.log.emit(f"⚠ {w.message}")

            self.finished.emit(self.plugin_key, cs)

        except Exception as exc:
            self.error.emit(self.plugin_key, str(exc))


class PluginApplyWorker(QObject):
    """Wendet ein (bereits gefiltertes) ChangeSet auf ein Plugin an."""

    finished = Signal(str)  # plugin_key
    error = Signal(str, str)  # (plugin_key, error_message)
    log = Signal(str)

    def __init__(
        self,
        plugin_key: str,
        plugin: PluginBase,
        changeset: ChangeSet,
    ) -> None:
        super().__init__()
        self.plugin_key = plugin_key
        self.plugin = plugin
        self.changeset = changeset

    @Slot()
    def run(self) -> None:
        try:
            cs = self.changeset

            if cs.new:
                self.log.emit(f"Lege {len(cs.new)} neue Schüler an...")
                results = self.plugin.apply_new(cs.new)
                self.log.emit(f"  Ergebnis: {len(results)} verarbeitet")

            if cs.changed:
                self.log.emit(f"Aktualisiere {len(cs.changed)} Schüler...")
                results = self.plugin.apply_changes(cs.changed)
                self.log.emit(f"  Ergebnis: {len(results)} verarbeitet")

            if cs.photo_updates:
                self.log.emit(f"Aktualisiere {len(cs.photo_updates)} Fotos...")
                results = self.plugin.apply_changes(cs.photo_updates)
                self.log.emit(f"  Ergebnis: {len(results)} verarbeitet")

            if cs.suspended:
                self.log.emit(f"Deaktiviere {len(cs.suspended)} Schüler...")
                results = self.plugin.apply_suspend(cs.suspended)
                self.log.emit(f"  Ergebnis: {len(results)} verarbeitet")

            self.finished.emit(self.plugin_key)

        except Exception as exc:
            self.error.emit(self.plugin_key, str(exc))
