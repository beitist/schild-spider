from __future__ import annotations

import logging
import warnings

from PySide6.QtCore import QObject, Signal, Slot

from core.engine import compute_changeset
from core.models import ChangeSet
from core.plugin_loader import load_adapter, load_plugins
from plugins.base import PluginBase, failure_reason, is_success

log = logging.getLogger(__name__)


class LoadWorker(QObject):
    """Lädt Schüler- und Lehrerdaten vom konfigurierten Adapter."""

    finished = Signal(list, list)  # (students, teachers)
    error = Signal(str)
    log_signal = Signal(str)
    # Email-Korrekturen: (automatisch gesetzt, manuell zu entscheiden)
    email_check_ready = Signal(list, list)

    def __init__(self, settings: dict) -> None:
        super().__init__()
        self.settings = settings

    def _emit(self, msg: str) -> None:
        """Gibt Meldung ans GUI UND in spider.log aus."""
        log.info(msg)
        self.log_signal.emit(msg)

    def _check_emails(
        self, all_students: list, visible_students: list
    ) -> tuple[list[dict], list[dict]]:
        """Prüft bei jedem Laden ALLE Quell-Emails und meldet das Ergebnis.

        Das Schema (Domain + Template) liefert das erste aktive Plugin, das
        eines hat (in der Praxis Microsoft 365). Geprüft wird gegen das
        Schema und auf doppelt vergebene Adressen.

        Eindeutig veraltete Adressen (fremde Klasse nach Klassenwechsel)
        werden SOFORT im geladenen Datensatz korrigiert — alle Plugins
        arbeiten ab hier mit der richtigen Adresse. Alles Mehrdeutige
        (Namensänderung, Dublette) bleibt unangetastet und geht an den
        Auswahl-Dialog. Die Zusammenfassung erscheint immer, auch wenn
        alles passt, damit sichtbar ist, dass geprüft wurde.

        Kollisionen werden über ALLE Schüler geprüft (auch bei aktivem
        Klassenfilter), gezählt und korrigiert nur die sichtbaren.
        Reine Berechnung — keine API-Zugriffe.

        Returns: (automatisch gesetzt, manuell zu entscheiden)
        """
        from collections import Counter
        from dataclasses import asdict

        from core import email_generator as eg

        try:
            plugins = load_plugins(self.settings)
        except Exception as exc:
            self._emit(f"⚠ Email-Prüfung übersprungen: {exc}")
            return [], []

        # Anzeigename statt Registry-Schlüssel ("Microsoft 365" statt "m365")
        owner = next(
            (
                (p.plugin_name(), p.email_scheme())
                for _, p in plugins
                if p.email_scheme()
            ),
            None,
        )
        if owner is None:
            self._emit(
                "ℹ Email-Prüfung übersprungen: kein aktives Plugin mit "
                "Email-Schema (z.B. Microsoft 365 deaktiviert oder ohne Domain)."
            )
            return [], []
        plugin_name, scheme = owner

        result = eg.check_emails([asdict(s) for s in all_students], scheme)
        by_id = {s.school_internal_id: s for s in visible_students}

        auto: list[dict] = []
        manual: list[dict] = []
        for item in result["findings"]:
            record = by_id.get(item.get("school_internal_id", ""))
            if record is None:
                continue
            # Klassenwechsel ist eindeutig: die Adresse trägt eine fremde
            # Klasse, das ist nie Absicht → direkt korrigieren.
            if item.get("reason") == eg.EMAIL_CLASS_CHANGE and item.get("email"):
                record.email = item["email"]
                auto.append(item)
            else:
                manual.append(item)

        counts = Counter(
            result["status"][sid] for sid in by_id if sid in result["status"]
        )
        self._emit_email_summary(plugin_name, scheme, counts, auto, manual, eg)
        return auto, manual

    def _emit_email_summary(
        self,
        plugin_name: str,
        scheme,
        counts,
        auto: list[dict],
        manual: list[dict],
        eg,
    ) -> None:
        """Schreibt die Zusammenfassung der Email-Prüfung ins Log."""
        total = sum(counts.values())
        umlaute = (
            "Klassen-Umlaute ausgeschrieben"
            if scheme.class_umlauts == eg.CLASS_UMLAUT_EXPAND
            else "Klassen-Umlaute ohne Punkte"
        )
        self._emit(
            f"Email-Prüfung ({plugin_name}, Schema "
            f"{scheme.template}@{scheme.domain}, {umlaute}): "
            f"{total} Schüler geprüft"
        )
        zeilen = [
            (eg.EMAIL_OK, "✓", "passen"),
            (eg.EMAIL_CLASS_CHANGE, "✎", "an die neue Klasse angepasst"),
            (eg.EMAIL_DUPLICATE, "⚠", "doppelt vergeben"),
            (eg.EMAIL_MISMATCH, "⚠", "weichen vom Schema ab"),
            (eg.EMAIL_COLLISION, "⚠", "ohne freie Adresse (manuell vergeben)"),
            (eg.EMAIL_EMPTY, "·", "ohne Email (vergibt das Plugin beim Anlegen)"),
            (eg.EMAIL_FOREIGN, "·", "mit fremder Domain (nicht angefasst)"),
        ]
        for key, symbol, text in zeilen:
            if counts.get(key) or key == eg.EMAIL_OK:
                self._emit(f"  {symbol} {counts.get(key, 0)} {text}")

        def _person(item: dict) -> str:
            return (
                f"{item.get('class_name', '')}: {item.get('last_name', '')}, "
                f"{item.get('first_name', '')}"
            )

        if auto:
            self._emit("  Angepasst (Rückschreiben nach SchILD steht noch aus):")
            for item in auto[:20]:
                self._emit(
                    f"    {_person(item)}: "
                    f"{item.get('old_email', '')} → {item.get('email', '')}"
                )
            if len(auto) > 20:
                self._emit(f"    ... und {len(auto) - 20} weitere")

        duplicates = [m for m in manual if m.get("reason") == eg.EMAIL_DUPLICATE]
        if duplicates:
            # Dubletten scheitern sonst beim Sync mit "already exists" —
            # deshalb alle einzeln nennen, nicht gekürzt.
            self._emit("  Doppelt vergeben (niedrigste SchILD-ID behält die Adresse):")
            for item in duplicates:
                self._emit(
                    f"    {_person(item)} (ID {item.get('school_internal_id', '')}): "
                    f"{item.get('old_email', '')} → Vorschlag "
                    f"{item.get('email', '') or '—'}"
                )

        if manual:
            faelle = "Fall" if len(manual) == 1 else "Fälle"
            self._emit(
                f"  → {len(manual)} {faelle} zur Entscheidung: "
                f"Button 'Email-Adressen aktualisieren'"
            )

    @Slot()
    def run(self) -> None:
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")

                self._emit("Lade Schülerdaten...")
                adapter = load_adapter(self.settings)
                students = adapter.load()
                all_students = list(students)
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

                # Email-Prüfung bei jedem Laden: veraltete Adressen werden
                # direkt korrigiert, mehrdeutige Fälle gehen an den Dialog.
                auto_emails, manual_emails = self._check_emails(all_students, students)

                for w in caught:
                    self._emit(f"⚠ {w.message}")

            self.email_check_ready.emit(auto_emails, manual_emails)
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

            # Änderungen VOR Neuanlagen: Eine Adresse kann von einem
            # bestehenden Datensatz zu einem neuen wandern, etwa wenn ein
            # Zuzug nach ID zwischen zwei gleichnamige Schüler rutscht und
            # die Kette neu durchzählt. Läuft die Neuanlage zuerst, ist die
            # Adresse noch belegt und das Anlegen scheitert.
            if cs.changed:
                phase = f"apply_changes ({len(cs.changed)} {label})"
                self._emit(f"Aktualisiere {len(cs.changed)} {label}...")
                ok, fail = self._report_results(self.plugin.apply_changes(cs.changed))
                total_ok += ok
                total_fail += fail

            if cs.new:
                phase = f"apply_new ({len(cs.new)} {label})"
                self._emit(f"Lege {len(cs.new)} neue {label} an...")
                ok, fail = self._report_results(self.plugin.apply_new(cs.new))
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
