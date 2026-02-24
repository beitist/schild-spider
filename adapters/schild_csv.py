from __future__ import annotations

import csv
import io
import warnings
from datetime import datetime
from pathlib import Path

from adapters.base import AdapterBase
from core.models import ConfigField, StudentRecord, TeacherRecord

# ---------------------------------------------------------------------------
# CSV-Feld-Varianten: RecordField → mögliche CSV-Spaltennamen
# Erste Übereinstimmung gewinnt (exakt, dann case-insensitive).
# ---------------------------------------------------------------------------

_STUDENT_FIELD_VARIANTS: dict[str, list[str]] = {
    "school_internal_id": ["Interne ID-Nummer", "Interne ID", "ID-Nummer"],
    "first_name": ["Vorname"],
    "last_name": ["Nachname", "Name"],
    "dob": ["Geburtsdatum", "Geb.-Datum", "GebDatum"],
    "email": ["E-Mail (Schule)", "E-Mail", "Email"],
    "class_name": ["Klasse", "Klassenbezeichnung"],
}

_TEACHER_FIELD_VARIANTS: dict[str, list[str]] = {
    "first_name": ["Vorname"],
    "last_name": ["Nachname", "Name"],
    "dob": ["Geburtsdatum", "Geb.-Datum", "GebDatum"],
    "job_title": ["Amtsbezeichnung", "Amtsbez.", "Amtsbez", "Dienstbezeichnung"],
}


# ---------------------------------------------------------------------------
# Generische CSV-Feld-Erkennung
# ---------------------------------------------------------------------------


def _resolve_csv_column(
    field_name: str,
    row: dict,
    field_variants: dict[str, list[str]],
) -> str | None:
    """Findet den tatsächlichen CSV-Spaltennamen für ein Record-Feld.

    Probiert jede Variante: zuerst exakter Match, dann case-insensitive.
    Gibt None zurück wenn keine Variante passt.
    """
    variants = field_variants.get(field_name, [])
    for variant in variants:
        if variant in row:
            return variant
        for key in row:
            if key.strip().lower() == variant.lower():
                return key
    return None


def _get_field_value(
    field_name: str,
    row: dict,
    field_variants: dict[str, list[str]],
    default: str = "",
) -> str:
    """Extrahiert einen Feldwert aus einer CSV-Zeile per Varianten-Lookup."""
    col = _resolve_csv_column(field_name, row, field_variants)
    if col is None:
        return default
    return row.get(col, default).strip()


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class SchildCsvAdapter(AdapterBase):
    """Liest SchILD-CSV-Export (;-getrennt, ISO-8859-1) + Fotos aus lokalem Ordner."""

    def __init__(
        self,
        csv_path: str,
        photos_dir: str | None = None,
        teachers_csv_path: str | None = None,
    ) -> None:
        self.csv_path = Path(csv_path)
        self.photos_dir = Path(photos_dir) if photos_dir else None
        self.teachers_csv_path = Path(teachers_csv_path) if teachers_csv_path else None

    # --- Metadaten ---

    @classmethod
    def adapter_name(cls) -> str:
        return "SchILD CSV-Export"

    @classmethod
    def config_schema(cls) -> list[ConfigField]:
        return [
            ConfigField(
                key="csv_path",
                label="Schüler-CSV-Datei",
                field_type="path",
                placeholder="C:\\SchILD\\Export\\schueler.csv",
            ),
            ConfigField(
                key="teachers_csv_path",
                label="Lehrer-CSV-Datei",
                field_type="path",
                required=False,
                placeholder="C:\\SchILD\\Export\\lehrer.csv",
            ),
            ConfigField(
                key="photos_dir",
                label="Foto-Ordner",
                field_type="dir",
                required=False,
                placeholder="C:\\SchILD\\Export\\Fotos",
            ),
        ]

    @classmethod
    def from_config(cls, config: dict) -> SchildCsvAdapter:
        return cls(
            csv_path=config.get("csv_path", ""),
            photos_dir=config.get("photos_dir") or None,
            teachers_csv_path=config.get("teachers_csv_path") or None,
        )

    # --- Interface ---

    def load(self) -> list[StudentRecord]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV-Datei nicht gefunden: {self.csv_path}")

        content = self._read_csv_content(self.csv_path)
        if content is None:
            raise ValueError(
                f"CSV-Datei konnte weder als UTF-8 noch als ISO-8859-1 "
                f"gelesen werden: {self.csv_path}"
            )

        reader = csv.DictReader(io.StringIO(content), delimiter=";")

        students: list[StudentRecord] = []
        skipped = 0

        for row_num, row in enumerate(reader, start=2):  # Zeile 1 = Header
            record = self._parse_row(row, row_num)
            if record:
                students.append(record)
            else:
                skipped += 1

        if skipped > 0:
            warnings.warn(
                f"{skipped} von {skipped + len(students)} Schüler-CSV-Zeilen "
                f"konnten nicht geparst werden."
            )

        return students

    def load_teachers(self) -> list[TeacherRecord]:
        """Liest Lehrerdaten aus einer separaten CSV-Datei."""
        if self.teachers_csv_path is None or not self.teachers_csv_path.exists():
            return []

        content = self._read_csv_content(self.teachers_csv_path)
        if content is None:
            return []

        reader = csv.DictReader(io.StringIO(content), delimiter=";")

        teachers: list[TeacherRecord] = []
        skipped = 0

        for row_num, row in enumerate(reader, start=2):
            record = self._parse_teacher_row(row, row_num)
            if record:
                teachers.append(record)
            else:
                skipped += 1

        if skipped > 0:
            warnings.warn(
                f"{skipped} von {skipped + len(teachers)} Lehrer-CSV-Zeilen "
                f"konnten nicht geparst werden."
            )

        return teachers

    # --- Parsing ---

    def _parse_row(self, row: dict, row_num: int = 0) -> StudentRecord | None:
        """Parst eine CSV-Zeile in ein StudentRecord.

        Gibt None zurück bei fehlenden Pflichtfeldern, loggt aber den Grund
        statt das Problem still zu verschlucken.
        """
        try:
            sid = _get_field_value("school_internal_id", row, _STUDENT_FIELD_VARIANTS)
            if not sid:
                return None

            dob_raw = _get_field_value("dob", row, _STUDENT_FIELD_VARIANTS)
            dob = self._normalize_date(dob_raw)

            photo_path = self._find_photo(sid)

            return StudentRecord(
                school_internal_id=sid,
                first_name=_get_field_value("first_name", row, _STUDENT_FIELD_VARIANTS),
                last_name=_get_field_value("last_name", row, _STUDENT_FIELD_VARIANTS),
                dob=dob,
                email=_get_field_value("email", row, _STUDENT_FIELD_VARIANTS),
                class_name=_get_field_value("class_name", row, _STUDENT_FIELD_VARIANTS),
                photo_path=str(photo_path) if photo_path else None,
            )
        except (KeyError, ValueError) as exc:
            warnings.warn(f"CSV Zeile {row_num} übersprungen: {exc}")
            return None

    def _parse_teacher_row(self, row: dict, row_num: int = 0) -> TeacherRecord | None:
        """Parst eine CSV-Zeile in ein TeacherRecord.

        Pflichtfelder: last_name + dob (bilden den composite_key).
        """
        try:
            last_name = _get_field_value("last_name", row, _TEACHER_FIELD_VARIANTS)
            dob_raw = _get_field_value("dob", row, _TEACHER_FIELD_VARIANTS)

            if not last_name or not dob_raw:
                return None

            dob = self._normalize_date(dob_raw)

            return TeacherRecord(
                first_name=_get_field_value("first_name", row, _TEACHER_FIELD_VARIANTS),
                last_name=last_name,
                dob=dob,
                job_title=_get_field_value("job_title", row, _TEACHER_FIELD_VARIANTS),
            )
        except (KeyError, ValueError) as exc:
            warnings.warn(f"Lehrer-CSV Zeile {row_num} übersprungen: {exc}")
            return None

    # --- Write-back ---

    def supports_write_back(self) -> bool:
        return True

    def write_back(self, updates: list[dict]) -> list[dict]:
        """Schreibt generierte Emails als CSV-Liste neben die Quelldatei.

        Erzeugt: <quellordner>/email_update_<timestamp>.csv
        Format: Klasse;Vorname;Nachname;SchulEmail
        """
        if not updates:
            return []

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.csv_path.parent / f"email_update_{timestamp}.csv"

        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["Klasse", "Vorname", "Nachname", "SchulEmail"])
            for update in updates:
                writer.writerow(
                    [
                        update.get("class_name", ""),
                        update.get("first_name", ""),
                        update.get("last_name", ""),
                        update.get("email", ""),
                    ]
                )

        return [
            {
                "school_internal_id": u.get("school_internal_id", ""),
                "success": True,
                "message": str(out_path),
            }
            for u in updates
        ]

    # --- Hilfsmethoden ---

    def _read_csv_content(self, path: Path) -> str | None:
        """Liest CSV-Inhalt mit Encoding-Erkennung (UTF-8-sig, ISO-8859-1)."""
        for encoding in ("utf-8-sig", "iso-8859-1"):
            try:
                return path.read_text(encoding=encoding)
            except (UnicodeDecodeError, ValueError):
                continue
        warnings.warn(
            f"CSV-Datei konnte weder als UTF-8 noch als ISO-8859-1 "
            f"gelesen werden: {path}"
        )
        return None

    def _normalize_date(self, date_str: str) -> str:
        """Konvertiert deutsches Datumsformat (DD.MM.YYYY) nach ISO (YYYY-MM-DD)."""
        if not date_str:
            return ""
        if "-" in date_str:
            return date_str  # Bereits ISO
        parts = date_str.split(".")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        return date_str

    def _find_photo(self, school_internal_id: str) -> Path | None:
        """Sucht ein Foto im Bilderordner anhand der SchILD-ID."""
        if not self.photos_dir or not self.photos_dir.exists():
            return None

        for ext in (".jpg", ".jpeg", ".png"):
            photo = self.photos_dir / f"{school_internal_id}{ext}"
            if photo.exists():
                return photo

        return None
