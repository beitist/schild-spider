from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import ConfigField


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

    def get_write_back_data(self) -> list[dict]:
        """Gibt Daten zurück die an den Adapter zurückgeschrieben werden sollen.

        Z.B. generierte Email-Adressen: [{"school_internal_id": "123", "email": "..."}]
        Standard: keine Daten.
        """
        return []
