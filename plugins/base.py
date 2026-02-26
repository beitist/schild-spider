from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import ChangeSet, ConfigField


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
