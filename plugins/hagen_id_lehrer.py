from __future__ import annotations

import warnings
from pathlib import Path

from core.models import ChangeSet, ConfigField, StudentRecord, TeacherRecord
from plugins.hagen_id import TEACHER_CLASS, HagenIdPlugin, _dedupe_ids

# Lehrer-IDs bekommen einen eigenen Namespace, damit sie nie mit einer
# SchILD-Schüler-ID kollidieren (beide landen im selben ID-Raum des
# Ausweis-Systems).
ID_PREFIX = "L-"

# Sicherheits-Riegel für Abmeldungen. Das Kollegium ist klein, deshalb
# reicht eine reine Prozent-Schwelle nicht — bei 20 Lehrkräften wären 15%
# gerade mal 3 Konten. Darum zusätzlich eine absolute Untergrenze.
_SUSPEND_MIN_ABSOLUTE = 3
_SUSPEND_MAX_RATIO = 0.3

_PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png")


class HagenIdLehrerPlugin(HagenIdPlugin):
    """Output-Plugin für die Lehrer-Ausweise im Hagen-ID System.

    Nutzt dieselbe API wie der Schüler-Sync, arbeitet aber ausschließlich
    auf den Datensätzen der Pseudo-Klasse ``Lehrerkollegium``.
    """

    def __init__(self, api_url: str, api_key: str, photo_dir: str = "") -> None:
        super().__init__(api_url, api_key)
        self.photo_dir = Path(photo_dir) if photo_dir else None
        # Zählwerte für den Sicherheits-Riegel, gefüllt in build_source()
        # bzw. get_manifest().
        self._source_count = 0
        self._server_active_count = 0
        # IDs, für die im Zielsystem bereits ein Foto liegt — diese Fotos
        # fasst der Sync nicht mehr an (die Lehrkraft pflegt sie selbst).
        self._server_has_photo: set[str] = set()

    # --- Metadaten ---

    @classmethod
    def plugin_name(cls) -> str:
        return "Hagen-ID (Lehrkräfte)"

    @classmethod
    def config_schema(cls) -> list[ConfigField]:
        return [
            ConfigField(
                key="api_url",
                label="API URL",
                field_type="url",
                placeholder="https://ausweisapi.example.com",
            ),
            ConfigField(
                key="api_key",
                label="API Key",
                field_type="password",
                placeholder="hgn_sk_...",
            ),
            ConfigField(
                key="photo_dir",
                label="Foto-Ordner (optional)",
                field_type="dir",
                required=False,
                placeholder="Dateiname = Kürzel, z.B. WOL.jpg",
            ),
        ]

    @classmethod
    def from_config(cls, config: dict) -> HagenIdLehrerPlugin:
        return cls(
            api_url=config.get("api_url", ""),
            api_key=config.get("api_key", ""),
            photo_dir=config.get("photo_dir", ""),
        )

    def test_connection(self) -> tuple[bool, str]:
        ok, message = super().test_connection()
        if not ok:
            return ok, message
        try:
            count = len(self.get_manifest())
        except Exception as exc:
            return False, f"Fehler: {exc}"
        return True, f"Verbunden: {count} Lehrkräfte im Zielsystem"

    # --- Quelldaten ---

    def source_label(self) -> str:
        return "Lehrkräfte"

    def build_source(
        self, students: list[StudentRecord], teachers: list[TeacherRecord]
    ) -> list[StudentRecord]:
        """Baut aus den TeacherRecords die SOLL-Datensätze für den Diff.

        Schüler werden hier bewusst ignoriert — dieses Plugin sieht
        ausschließlich das Kollegium.
        """
        records: dict[str, StudentRecord] = {}
        without_id = 0

        for teacher in teachers:
            if not teacher.teacher_id:
                # Ohne stabile SchILD-ID gibt es keinen kollisionsfreien
                # Namespace — Datensatz überspringen statt zu raten.
                without_id += 1
                continue

            sid = f"{ID_PREFIX}{teacher.teacher_id}"
            if sid in records:
                warnings.warn(
                    f"Doppelte Lehrer-ID {sid} in den Quelldaten — "
                    f"nur der letzte Datensatz wird synchronisiert."
                )

            records[sid] = StudentRecord(
                school_internal_id=sid,
                first_name=teacher.first_name,
                last_name=teacher.last_name,
                dob=teacher.dob,
                email=teacher.email,
                class_name=TEACHER_CLASS,
                photo_path=self._find_photo(teacher.kuerzel),
            )

        if without_id:
            warnings.warn(
                f"{without_id} Lehrkräfte ohne SchILD-ID übersprungen. "
                f"Der CSV-Adapter liefert keine Lehrer-IDs — für den "
                f"Lehrer-Sync wird der Datenbank-Adapter benötigt."
            )

        self._source_count = len(records)
        return list(records.values())

    def _find_photo(self, kuerzel: str) -> str | None:
        """Sucht ein Foto im konfigurierten Ordner anhand des Kürzels."""
        if not kuerzel or self.photo_dir is None or not self.photo_dir.is_dir():
            return None

        for ext in _PHOTO_EXTENSIONS:
            photo = self.photo_dir / f"{kuerzel}{ext}"
            if photo.exists():
                return str(photo)

        # Fallback: Groß-/Kleinschreibung im Dateinamen ignorieren
        wanted = kuerzel.lower()
        for photo in self.photo_dir.iterdir():
            if (
                photo.is_file()
                and photo.stem.lower() == wanted
                and photo.suffix.lower() in _PHOTO_EXTENSIONS
            ):
                return str(photo)

        return None

    # --- Zielsystem ---

    def get_manifest(self) -> list[dict]:
        """IST-Zustand — ausschließlich die Datensätze des Kollegiums.

        Ohne diesen Filter stünden hier auch alle Schüler, die nicht in den
        Lehrer-Quelldaten vorkommen — das Plugin würde sie abmelden.
        """
        entries = [
            s for s in self._fetch_manifest() if s.get("class_name") == TEACHER_CLASS
        ]

        self._server_active_count = sum(1 for e in entries if e.get("is_active", True))
        self._server_has_photo = {
            e["school_internal_id"] for e in entries if e.get("has_photo")
        }

        return entries

    # --- Fotos: nur Erst-Befüllung ---

    def needs_photo_update(self, student: dict, target: dict) -> bool:
        """Fotos nur setzen, solange im Zielsystem noch keines liegt.

        Lehrkräfte laden ihr Foto selbst hoch. Würde der Sync wie beim
        Schüler-Plugin auf Hash-Gleichheit bestehen, überschriebe er jeden
        Selbst-Upload beim nächsten Lauf wieder mit dem Ordner-Bild.
        """
        if target.get("has_photo"):
            return False
        return super().needs_photo_update(student, target)

    def _include_photo(self, student: dict) -> bool:
        # Gilt auch für reine Datenänderungen: ein /change-Payload darf kein
        # Foto mitschleppen, wenn im Zielsystem schon eines liegt.
        return student["school_internal_id"] not in self._server_has_photo

    # --- Senden ---

    def _prepare_student(self, student: dict) -> dict:
        entry = super()._prepare_student(student)
        # Immer setzen: der Server leitet aus der Klasse die Lehrer-
        # Gültigkeit ab (heute + 5 Jahre, 31.07.).
        entry["class_name"] = TEACHER_CLASS
        return entry

    def enrich_preview(self, changeset: ChangeSet) -> None:
        """Warnt schon in der Vorschau, wenn der Riegel greifen würde."""
        problem = self._suspend_blocker(changeset.suspended)
        if problem:
            warnings.warn(problem)

    def apply_suspend(self, school_internal_ids: list[str]) -> list[dict]:
        ids = _dedupe_ids(school_internal_ids)
        if not ids:
            return []

        problem = self._suspend_blocker(ids)
        if problem:
            raise RuntimeError(problem)

        return super().apply_suspend(ids)

    def _suspend_blocker(self, school_internal_ids: list[str]) -> str | None:
        """Prüft die Sicherheits-Riegel. Gibt den Grund zurück, sonst None."""
        count = len(school_internal_ids)
        if count == 0:
            return None

        if self._source_count == 0:
            return (
                "0 Lehrkräfte aus SchILD geladen — es werden keine Konten "
                "abgemeldet. Bitte Quelldaten und PersonTyp-Filter prüfen."
            )

        limit = max(
            _SUSPEND_MIN_ABSOLUTE, _SUSPEND_MAX_RATIO * self._server_active_count
        )
        if count > limit:
            return (
                f"{count} von {self._server_active_count} Lehrer-Konten sollen "
                f"abgemeldet werden (Grenze: {limit:.0f}). Das deutet auf "
                f"unvollständige Quelldaten hin — bitte prüfen. Einzelne "
                f"Abmeldungen lassen sich in der Vorschau abwählen."
            )

        return None
