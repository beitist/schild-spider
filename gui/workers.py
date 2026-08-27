from __future__ import annotations

import logging
import warnings

from PySide6.QtCore import QObject, Signal, Slot

from core.engine import compute_changeset
from core.models import ChangeSet
from core.plugin_loader import load_adapter
from plugins.base import PluginBase, failure_reason, is_success

log = logging.getLogger(__name__)


class LoadWorker(QObject):
    """Lädt Schüler- und Lehrerdaten vom konfigurierten Adapter."""

    finished = Signal(list, list)  # (students, teachers)
    error = Signal(str)
    log_signal = Signal(str)

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self.settings = settings

    def _emit(self, msg: str) -> None:
        """Gibt Meldung ans GUI UND in spider.log aus."""
        log.info(msg)
        self.log_signal.emit(msg)

    @Slot()
    def run(self) -> None:
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")

                self._emit("Lade Schülerdaten...")
                adapter = load_adapter(self.settings)
                students = adapter.load()
                self._emit(f"{len(students)} Schüler geladen.")

                # Kurs-Statistik (Diagnostik für LuL-Gruppen)
                total_courses = sum(len(s.courses) for s in students)
                if total_courses:
                    with_teacher = sum(
                        1 for s in students for c in s.courses if c.teacher_name
                    )
                    self._emit(
                        f"  Kurse: {total_courses} Zuordnungen, "
                        f"{with_teacher} mit Lehrkraft"
                    )

                # Debug-Klassenfilter
                class_filter = (
                    (self.settings.get("debug_class_filter", "") or "").strip().lower()
                )
                if class_filter:
                    before = len(students)
                    students = [
                        s for s in students if class_filter in s.class_name.lower()
                    ]
                    self._emit(
                        f"\u26a0 FILTER aktiv: '{class_filter}' "
                        f"\u2192 {len(students)} von {before} Sch\u00fclern"
                    )

                teachers = adapter.load_teachers()
                if teachers:
                    self._emit(f"{len(teachers)} Lehrer geladen.")
                    # Diagnostik: fehlende IDs/Emails fallen sonst erst im
                    # Zielsystem auf — dort aber ohne erkennbare Ursache.
                    with_id = sum(1 for t in teachers if t.teacher_id)
                    with_email = sum(1 for t in teachers if t.email)
                    self._emit(
                        f"  davon {with_id} mit SchILD-ID, {with_email} mit Email"
                    )
                    if with_email < len(teachers):
                        ohne = [
                            t.kuerzel or t.last_name for t in teachers if not t.email
                        ]
                        self._emit(
                            f"  ⚠ ohne Email (EMailDienstlich leer): "
                            f"{', '.join(ohne[:15])}"
                            + (" ..." if len(ohne) > 15 else "")
                        )
                else:
                    self._emit("Keine Lehrerdaten (Quelle nicht konfiguriert).")

                for w in caught:
                    self._emit(f"⚠ {w.message}")

            self.finished.emit(students, teachers)

        except Exception as exc:
            log.exception("LoadWorker fehlgeschlagen")
            self.error.emit(str(exc))


class PluginComputeWorker(QObject):
    """Berechnet ein ChangeSet für ein einzelnes Plugin."""

    finished = Signal(str, ChangeSet)  # (plugin_key, changeset)
    error = Signal(str, str)  # (plugin_key, error_message)
    log_signal = Signal(str)

    def __init__(
        self,
        plugin_key: str,
        plugin: PluginBase,
        students: list,
        max_suspend: float,
        teachers: list | None = None,
    ) -> None:
        super().__init__()
        self.plugin_key = plugin_key
        self.plugin = plugin
        self.students = students
        self.max_suspend = max_suspend
        self.teachers = teachers or []

    def _emit(self, msg: str) -> None:
        """Gibt Meldung ans GUI UND in spider.log aus."""
        log.info(msg)
        self.log_signal.emit(msg)

    @Slot()
    def run(self) -> None:
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")

                self._emit(f"Berechne ChangeSet für {self.plugin_key}...")

                # Das Plugin bestimmt seine SOLL-Datensätze selbst —
                # Standard sind die Schüler, Lehrer-Plugins bauen sich ihre
                # Datensätze aus den TeacherRecords.
                source = self.plugin.build_source(self.students, self.teachers)
                if source is not self.students:
                    self._emit(f"  Quelldatensätze: {len(source)}")

                cs = compute_changeset(source, self.plugin, self.max_suspend)
                self._emit(
                    f"  {self.plugin_key}: {len(cs.new)} neu, "
                    f"{len(cs.changed)} geändert, "
                    f"{len(cs.suspended)} abgemeldet, "
                    f"{len(cs.photo_updates)} Foto-Updates"
                )

                # Vorschau-Daten anreichern (z.B. Emails vorgenerieren)
                self._emit("Vorschau anreichern...")
                self.plugin.enrich_preview(cs)

                # Gruppen-Diff berechnen (für Vorschau)
                if self.students:
                    from dataclasses import asdict

                    self._emit("Berechne Gruppen-Diff...")
                    student_dicts = [asdict(s) for s in self.students]
                    teacher_dicts = [asdict(t) for t in self.teachers]
                    cs.group_changes = self.plugin.compute_group_diff(
                        student_dicts, teacher_dicts
                    )
                    if cs.group_changes:
                        self._emit(
                            f"  {len(cs.group_changes)} Gruppenänderungen geplant"
                        )

                for w in caught:
                    self._emit(f"⚠ {w.message}")

            self.finished.emit(self.plugin_key, cs)

        except Exception as exc:
            log.exception("ComputeWorker fehlgeschlagen: %s", self.plugin_key)
            self.error.emit(self.plugin_key, str(exc))


class PluginApplyWorker(QObject):
    """Wendet ein (bereits gefiltertes) ChangeSet auf ein Plugin an."""

    finished = Signal(str)  # plugin_key
    error = Signal(str, str)  # (plugin_key, error_message)
    log_signal = Signal(str)
    write_back_ready = Signal(str, list)  # (plugin_key, write_back_data)
    summary_ready = Signal(str, int, int)  # (plugin_key, ok_gesamt, fehler_gesamt)

    # Log-Obergrenze pro Phase, damit Massenfehler das Log nicht sprengen
    _MAX_FAILURE_LINES = 20

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

    def _emit(self, msg: str) -> None:
        """Gibt Meldung ans GUI UND in spider.log aus."""
        log.info(msg)
        self.log_signal.emit(msg)

    def _report_results(self, results: list) -> tuple[int, int]:
        """Fasst Plugin-Ergebnisse zusammen und listet jeden Fehler im Log auf.

        Returns: (ok_count, fail_count)
        """
        failures = [r for r in results if not is_success(r)]
        ok = len(results) - len(failures)
        self._emit(f"  Ergebnis: {ok} OK, {len(failures)} Fehler")

        for r in failures[: self._MAX_FAILURE_LINES]:
            ident = r.get("school_internal_id") or r.get("group") or "?"
            self._emit(f"  ✗ {ident}: {failure_reason(r)}")
        if len(failures) > self._MAX_FAILURE_LINES:
            self._emit(
                f"  ... und {len(failures) - self._MAX_FAILURE_LINES} weitere Fehler"
            )

        return ok, len(failures)

    @Slot()
    def run(self) -> None:
        try:
            cs = self.changeset
            phase = "init"
            total_ok = 0
            total_fail = 0
            label = self.plugin.source_label()

            if cs.new:
                phase = f"apply_new ({len(cs.new)} {label})"
                self._emit(f"Lege {len(cs.new)} neue {label} an...")
                ok, fail = self._report_results(self.plugin.apply_new(cs.new))
                total_ok += ok
                total_fail += fail

            if cs.changed:
                phase = f"apply_changes ({len(cs.changed)} {label})"
                self._emit(f"Aktualisiere {len(cs.changed)} {label}...")
                ok, fail = self._report_results(self.plugin.apply_changes(cs.changed))
                total_ok += ok
                total_fail += fail

            if cs.photo_updates:
                phase = f"apply_photos ({len(cs.photo_updates)} Fotos)"
                self._emit(f"Aktualisiere {len(cs.photo_updates)} Fotos...")
                ok, fail = self._report_results(
                    self.plugin.apply_changes(cs.photo_updates)
                )
                total_ok += ok
                total_fail += fail

            if cs.suspended:
                phase = f"apply_suspend ({len(cs.suspended)} {label})"
                self._emit(f"Deaktiviere {len(cs.suspended)} {label}...")
                ok, fail = self._report_results(self.plugin.apply_suspend(cs.suspended))
                total_ok += ok
                total_fail += fail

            # Write-back-Daten prüfen und im Log anzeigen
            phase = "write_back_check"
            write_back_data = self.plugin.get_write_back_data()
            if write_back_data:
                self._emit(f"\n--- Generierte Daten ({len(write_back_data)}) ---")
                for item in write_back_data:
                    name = (
                        f"{item.get('first_name', '')} {item.get('last_name', '')}"
                    ).strip()
                    email = item.get("email", "")
                    cls = item.get("class_name", "")
                    if name and email:
                        self._emit(f"  {cls}: {name} \u2192 {email}")
                self._emit(
                    "Bitte \u00fcber 'R\u00fcckschreiben' an SchILD zur\u00fcckschreiben."
                )
                self.write_back_ready.emit(self.plugin_key, write_back_data)

            # Gruppenänderungen anwenden
            if cs.group_changes:
                phase = f"apply_groups ({len(cs.group_changes)} Änderungen)"
                self._emit(f"Wende {len(cs.group_changes)} Gruppenänderungen an...")
                ok, fail = self._report_results(
                    self.plugin.apply_group_changes(cs.group_changes)
                )
                total_ok += ok
                total_fail += fail

            phase = "done"
            self.summary_ready.emit(self.plugin_key, total_ok, total_fail)
            self.finished.emit(self.plugin_key)

        except Exception as exc:
            log.exception(
                "ApplyWorker fehlgeschlagen: %s (Phase: %s)", self.plugin_key, phase
            )
            self.error.emit(self.plugin_key, f"{exc} (Phase: {phase})")
