from __future__ import annotations

import csv
from pathlib import Path

from adapters.base import AdapterBase
from core.models import ConfigField, StudentRecord

# Mapping: CSV-Spaltenname → StudentRecord-Feld
_CSV_FIELD_MAP = {
    "Interne ID-Nummer": "school_internal_id",
    "Vorname": "first_name",
    "Nachname": "last_name",
    "Geburtsdatum": "dob",
    "E-Mail (Schule)": "email",
    "Klasse": "class_name",
}


class SchildCsvAdapter(AdapterBase):
    """Liest SchILD-CSV-Export (;-getrennt, ISO-8859-1) + Fotos aus lokalem Ordner."""

    def __init__(self, csv_path: str, photos_dir: str | None = None) -> None:
        self.csv_path = Path(csv_path)
        self.photos_dir = Path(photos_dir) if photos_dir else None

    # --- Metadaten ---

    @classmethod
    def adapter_name(cls) -> str:
        return "SchILD CSV-Export"

    @classmethod
    def config_schema(cls) -> list[ConfigField]:
        return [
            ConfigField(
                key="csv_path",
                label="CSV-Datei",
                field_type="path",
                placeholder="C:\\SchILD\\Export\\schueler.csv",
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
        )

    # --- Interface ---

    def load(self) -> list[StudentRecord]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV-Datei nicht gefunden: {self.csv_path}")

        # Encoding-Erkennung: UTF-8-sig zuerst (handhabt BOM automatisch),
        # Fallback auf ISO-8859-1 (ältere SchILD-Versionen).
        # Ohne das würde ein UTF-8-BOM als "ï»¿" vor dem ersten Header
        # landen und alle Key-Lookups still fehlschlagen.
        content = None
        for encoding in ("utf-8-sig", "iso-8859-1"):
            try:
                content = self.csv_path.read_text(encoding=encoding)
                break
            except (UnicodeDecodeError, ValueError):
                continue

        if content is None:
            raise ValueError(
                f"CSV-Datei konnte weder als UTF-8 noch als ISO-8859-1 "
                f"gelesen werden: {self.csv_path}"
            )

        import io

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
            # Info-Meldung statt stilles Schlucken — hilft bei CSV-Problemen
            import warnings

            warnings.warn(
                f"{skipped} von {skipped + len(students)} CSV-Zeilen "
                f"konnten nicht geparst werden."
            )

        return students

    def _parse_row(self, row: dict, row_num: int = 0) -> StudentRecord | None:
        """Parst eine CSV-Zeile in ein StudentRecord.

        Gibt None zurück bei fehlenden Pflichtfeldern, loggt aber den Grund
        statt das Problem still zu verschlucken.
        """
        try:
            sid = row[_CSV_FIELD_MAP_KEY_FOR("school_internal_id", row)].strip()
            if not sid:
                return None

            dob_raw = row.get(_find_csv_key("Geburtsdatum", row), "").strip()
            dob = self._normalize_date(dob_raw)

            photo_path = self._find_photo(sid)

            return StudentRecord(
                school_internal_id=sid,
                first_name=row.get(_find_csv_key("Vorname", row), "").strip(),
                last_name=row.get(_find_csv_key("Nachname", row), "").strip(),
                dob=dob,
                email=row.get(_find_csv_key("E-Mail (Schule)", row), "").strip(),
                class_name=row.get(_find_csv_key("Klasse", row), "").strip(),
                photo_path=str(photo_path) if photo_path else None,
            )
        except (KeyError, ValueError) as exc:
            # Fehler loggen statt still verschlucken — erleichtert Debugging
            import warnings

            warnings.warn(f"CSV Zeile {row_num} übersprungen: {exc}")
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


def _find_csv_key(display_name: str, row: dict) -> str:
    """Findet den passenden Schlüssel im CSV-Row-Dict (exakter Match)."""
    if display_name in row:
        return display_name
    # Fallback: case-insensitive
    for key in row:
        if key.strip().lower() == display_name.lower():
            return key
    return display_name


def _CSV_FIELD_MAP_KEY_FOR(field_name: str, row: dict) -> str:
    """Findet den CSV-Spaltennamen für ein StudentRecord-Feld."""
    for csv_col, record_field in _CSV_FIELD_MAP.items():
        if record_field == field_name:
            return _find_csv_key(csv_col, row)
    raise KeyError(f"Kein CSV-Mapping für Feld: {field_name}")
