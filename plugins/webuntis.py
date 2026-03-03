"""WebUntis Output-Plugin — CSV-basierte Schüler-Synchronisation.

IST-Zustand: WebUntis User-CSV-Export (tab-separiert, manuell exportiert).
SOLL-Zustand: SchILD-Daten vom Adapter.
Schreib-Zugriff: Erzeugt Import-CSVs zum manuellen Hochladen in WebUntis.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from datetime import datetime
from pathlib import Path

from core.models import ConfigField
from plugins.base import PluginBase

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Geschlecht-Mapping: SchILD → WebUntis CSV-Import
# SchILD: "3" = männlich, "4" = weiblich, "5" = divers, "6" = ohne Angabe
# ---------------------------------------------------------------------------

_SCHILD_GENDER_CSV: dict[str, str] = {
    "3": "m",
    "4": "w",
    "5": "d",
    "6": "",
}


class WebUntisPlugin(PluginBase):
    """Output-Plugin für WebUntis (CSV-Export ↔ CSV-Import)."""

    def __init__(self, export_dir: str) -> None:
        self._export_dir = Path(export_dir) if export_dir else Path(".")
        # Wird vom Filepicker gesetzt (vor get_manifest)
        self._webuntis_csv_path: str = ""
        # Cache: school_internal_id → WebUntis-CSV-Zeile
        self._untis_students: dict[str, dict] = {}

    # --- Metadaten ---

    @classmethod
    def plugin_name(cls) -> str:
        return "WebUntis"

    @classmethod
    def config_schema(cls) -> list[ConfigField]:
        return [
            ConfigField(
                key="export_dir",
                label="Export-Ordner (Import-CSVs)",
                field_type="dir",
                placeholder="C:\\WebUntis-Import",
                required=False,
            ),
        ]

    @classmethod
    def from_config(cls, config: dict) -> WebUntisPlugin:
        return cls(export_dir=config.get("export_dir", ""))

    def pre_compute_files(self) -> list[dict]:
        return [
            {
                "key": "_webuntis_csv_path",
                "label": "WebUntis User-Export (CSV/TXT) auswählen",
                "filter": "CSV/TXT (*.csv *.txt);;Alle Dateien (*)",
            }
        ]

    def test_connection(self) -> tuple[bool, str]:
        if self._export_dir and not self._export_dir.exists():
            return False, f"Export-Ordner existiert nicht: {self._export_dir}"
        return True, "OK. Export-Ordner erreichbar."

    # --- Sync-Interface ---

    def get_manifest(self) -> list[dict]:
        """Liest WebUntis User-CSV-Export als IST-Zustand.

        CSV-Format (tab-separiert):
        ID  Benutzer  Gruppe  E-Mail Adresse  Person  Sprache  Max. Buch.

        ID = SchILD interne ID (Schlüssel extern).
        Nur Zeilen mit Gruppe == "Schüler" werden berücksichtigt.
        """
        if not self._webuntis_csv_path:
            log.warning("Kein WebUntis-CSV ausgewählt — leeres Manifest.")
            return []

        csv_path = Path(self._webuntis_csv_path)
        if not csv_path.exists():
            log.error("WebUntis-CSV nicht gefunden: %s", csv_path)
            return []

        content = self._read_csv(csv_path)
        if content is None:
            return []

        self._untis_students.clear()
        manifest: list[dict] = []

        reader = csv.DictReader(io.StringIO(content), delimiter="\t")
        for row in reader:
            gruppe = (row.get("Gruppe") or "").strip()
            if gruppe != "Schüler":
                continue

            sid = (row.get("ID") or "").strip()
            if not sid:
                continue

            self._untis_students[sid] = row
            data_hash = self._compute_remote_hash(row)

            manifest.append(
                {
                    "school_internal_id": sid,
                    "data_hash": data_hash,
                    "is_active": True,
                }
            )

        log.info(
            "WebUntis Manifest: %d Schüler aus CSV geladen (%s)",
            len(manifest),
            csv_path.name,
        )
        return manifest

    def compute_data_hash(self, student: dict) -> str:
        """Berechnet Hash über SOLL-Daten (aus SchILD).

        Hash über E-Mail + Nachname — Felder die auch im WebUntis-CSV
        verfügbar sind. E-Mail enthält die Klasse (z.B. aav25e.nachname@...)
        → Klassenwechsel wird erkannt.
        """
        parts = "|".join(
            [
                student.get("email", "").strip().lower(),
                student.get("last_name", "").strip().lower(),
            ]
        )
        return hashlib.sha256(parts.encode()).hexdigest()

    def apply_new(self, students: list[dict]) -> list[dict]:
        """Erzeugt Import-CSV für neue Schüler."""
        if not students:
            return []
        return self._write_import_csv("neue_schueler", students)

    def apply_changes(self, students: list[dict]) -> list[dict]:
        """Erzeugt Import-CSV für geänderte Schüler."""
        if not students:
            return []
        return self._write_import_csv("geaenderte_schueler", students)

    def apply_suspend(self, school_internal_ids: list[str]) -> list[dict]:
        """Erzeugt CSV mit zu entfernenden Schülern."""
        if not school_internal_ids:
            return []

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"webuntis_zu_entfernen_{timestamp}.csv"
        filepath = self._export_dir / filename
        self._export_dir.mkdir(parents=True, exist_ok=True)

        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["id", "familienname", "vorname", "aktion"])
            for sid in school_internal_ids:
                cached = self._untis_students.get(sid, {})
                writer.writerow(
                    [
                        sid,
                        (cached.get("Person") or "").strip(),
                        "",  # Vorname nicht im User-Export
                        "ENTFERNEN",
                    ]
                )

        log.info(
            "WebUntis: %d zu entfernende Schüler → %s",
            len(school_internal_ids),
            filepath,
        )

        return [
            {
                "school_internal_id": sid,
                "success": True,
                "message": f"In CSV exportiert: {filename}",
            }
            for sid in school_internal_ids
        ]

    # --- CSV-Export ---

    def _write_import_csv(self, prefix: str, students: list[dict]) -> list[dict]:
        """Schreibt Import-CSV für WebUntis.

        Format (;-getrennt, UTF-8 mit BOM):
        id;vorname;familienname;geburtstag;geschlecht;klasse
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"webuntis_{prefix}_{timestamp}.csv"
        filepath = self._export_dir / filename
        self._export_dir.mkdir(parents=True, exist_ok=True)

        results: list[dict] = []

        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(
                ["id", "vorname", "familienname", "geburtstag", "geschlecht", "klasse"]
            )

            for student in students:
                sid = student.get("school_internal_id", "")
                gender_csv = _SCHILD_GENDER_CSV.get(
                    str(student.get("gender", "")).strip(), ""
                )

                # Geburtsdatum: ISO → deutsches Format (DD.MM.YYYY)
                dob = student.get("dob", "")
                if dob and "-" in dob:
                    parts = dob.split("-")
                    if len(parts) == 3:
                        dob = f"{parts[2]}.{parts[1]}.{parts[0]}"

                writer.writerow(
                    [
                        sid,
                        student.get("first_name", "").strip(),
                        student.get("last_name", "").strip().upper(),
                        dob,
                        gender_csv,
                        student.get("class_name", "").strip(),
                    ]
                )

                results.append(
                    {
                        "school_internal_id": sid,
                        "success": True,
                        "message": f"In CSV exportiert: {filename}",
                    }
                )

        log.info("WebUntis: %d Schüler → %s", len(students), filepath)
        return results

    # --- Hilfsmethoden ---

    def _compute_remote_hash(self, csv_row: dict) -> str:
        """Berechnet Hash über IST-Daten aus WebUntis User-CSV.

        Gleiche Felder wie compute_data_hash: E-Mail + Nachname (Person).
        """
        parts = "|".join(
            [
                (csv_row.get("E-Mail Adresse") or "").strip().lower(),
                (csv_row.get("Person") or "").strip().lower(),
            ]
        )
        return hashlib.sha256(parts.encode()).hexdigest()

    def _read_csv(self, path: Path) -> str | None:
        """Liest CSV-Inhalt mit Encoding-Erkennung."""
        for encoding in ("utf-8-sig", "utf-8", "iso-8859-1"):
            try:
                return path.read_text(encoding=encoding)
            except (UnicodeDecodeError, ValueError):
                continue
        log.error("CSV konnte nicht gelesen werden: %s", path)
        return None
