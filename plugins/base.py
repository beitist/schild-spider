from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import ChangeSet, ConfigField, StudentRecord, TeacherRecord

# Status-Werte, die ein Zielsystem statt eines booleschen "success" liefern kann.
_SUCCESS_STATES = {
    "ok",
    "success",
    "created",
    "updated",
    "changed",
    "suspended",
    "unchanged",
    "skipped",
}

_ERROR_KEYS = ("error", "errors", "detail", "message")


def is_success(result: dict) -> bool:
    """Wertet ein Ergebnis-Objekt eines Zielsystems aus.

    Maßgeblich ist ``success``, wenn das Feld existiert. Sonst wird ein
    Status-Feld herangezogen — nicht jede API antwortet mit einem Boolean.
    Fehlt beides, gilt der Datensatz nur dann als erfolgreich, wenn auch
    kein Fehlerfeld gefüllt ist.
    """
    if "success" in result:
        return bool(result["success"])

    for key in ("status", "result", "action"):
        if key in result:
            return str(result[key]).lower() in _SUCCESS_STATES

    return not any(result.get(key) for key in _ERROR_KEYS)


def failure_reason(result: dict) -> str:
    """Liest die Fehlerursache aus — notfalls als Rohantwort.

    Ein nacktes "Unbekannter Fehler" hilft bei der Diagnose nicht weiter;
    lieber die tatsächliche Antwort des Servers zeigen.
    """
    for key in _ERROR_KEYS:
        value = result.get(key)
        if value:
            return str(value)
    return f"keine Fehlermeldung, Rohantwort: {result}"


class PluginBase(ABC):
    """Abstrakte Basisklasse für Output-Plugins."""

    # --- Metadaten (jedes Plugin beschreibt sich selbst) ---

    @classmethod
    @abstractmethod
    def plugin_name(cls) -> str:
        """Anzeigename des Plugins (z.B. 'Hagen-ID')."""
        ...

    @classmethod
    @abstractmethod
    def config_schema(cls) -> list[ConfigField]:
        """Liste der Konfigurationsfelder, die dieses Plugin benötigt."""
        ...

    @classmethod
    @abstractmethod
    def from_config(cls, config: dict) -> PluginBase:
        """Erstellt eine Plugin-Instanz aus einem Config-Dict (aus settings.json)."""
        ...

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """Testet die Verbindung zum Zielsystem. Returns: (ok, message)."""
        ...

    # --- Sync-Interface (Schnittstelle nach innen) ---

    @abstractmethod
    def get_manifest(self) -> list[dict]:
        """Holt den IST-Zustand des Zielsystems."""
        ...

    @abstractmethod
    def compute_data_hash(self, student: dict) -> str:
        """Berechnet einen Hash über die relevanten Felder."""
        ...

    @abstractmethod
    def apply_new(self, students: list[dict]) -> list[dict]:
        """Legt neue Schüler an. Returns: Ergebnisse pro Schüler."""
        ...

    @abstractmethod
    def apply_changes(self, students: list[dict]) -> list[dict]:
        """Aktualisiert bestehende Schüler."""
        ...

    @abstractmethod
    def apply_suspend(self, school_internal_ids: list[str]) -> list[dict]:
        """Deaktiviert Schüler."""
        ...

    # --- Optionales Interface ---

    def source_label(self) -> str:
        """Bezeichnung der Datensätze für Log-Ausgaben."""
        return "Schüler"

    def build_source(
        self, students: list[StudentRecord], teachers: list[TeacherRecord]
    ) -> list[StudentRecord]:
        """Bestimmt die SOLL-Datensätze für den Diff.

        Standard: die Schüler. Lehrer-Plugins überschreiben das und bauen
        aus den TeacherRecords passende Datensätze (eigener ID-Namespace).
        """
        return students

    def needs_photo_update(self, student: dict, target: dict) -> bool:
        """Entscheidet, ob das lokale Foto ins Zielsystem geschrieben werden muss.

        Standard: das Plugin kann Foto-Hashes berechnen und der lokale Hash
        weicht vom Zielsystem ab. Plugins ohne ``compute_photo_hash``
        synchronisieren keine Fotos.
        """
        compute = getattr(self, "compute_photo_hash", None)
        if compute is None:
            return False
        local_hash = compute(student["photo_path"])
        return bool(local_hash) and local_hash != target.get("photo_hash", "")

    def pre_compute_files(self) -> list[dict]:
        """Dateien die vor dem Compute per Filepicker gewählt werden müssen.

        Returns: [{"key": "attribut_name", "label": "Dialog-Titel",
                   "filter": "CSV (*.csv *.txt)"}]
        Leere Liste = kein Filepicker nötig (Standard).
        """
        return []

    def enrich_preview(self, changeset: ChangeSet) -> None:
        """Reichert das ChangeSet mit Preview-Daten an (z.B. generierte Emails)."""

    def compute_group_diff(
        self, all_students: list[dict], teachers: list[dict]
    ) -> list[dict]:
        """Berechnet geplante Gruppenänderungen (SOLL vs IST).

        Wird in der Compute-Phase aufgerufen, Ergebnis wird in der Vorschau angezeigt.
        Returns: [{id, group_type, group_name, group_id, action, member_name, member_id, class_name}]
        """
        return []

    def apply_group_changes(self, changes: list[dict]) -> list[dict]:
        """Führt die vom User ausgewählten Gruppenänderungen aus.

        changes: Gefilterte Liste aus compute_group_diff (nur angehakte Einträge).
        Returns: Ergebnisse [{action, group, success, message}]
        """
        return []

    def get_write_back_data(self) -> list[dict]:
        """Gibt Daten zurück die an den Adapter zurückgeschrieben werden sollen.

        Z.B. generierte Email-Adressen: [{"school_internal_id": "123", "email": "..."}]
        Standard: keine Daten.
        """
        return []
