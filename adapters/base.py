from __future__ import annotations

from abc import ABC, abstractmethod

from core.models import ConfigField, StudentRecord, TeacherRecord


class AdapterBase(ABC):
    """Abstrakte Basisklasse für Input-Adapter."""

    # --- Metadaten ---

    @classmethod
    @abstractmethod
    def adapter_name(cls) -> str:
        """Anzeigename des Adapters (z.B. 'SchILD CSV-Export')."""
        ...

    @classmethod
    @abstractmethod
    def config_schema(cls) -> list[ConfigField]:
        """Liste der Konfigurationsfelder, die dieser Adapter benötigt."""
        ...

    @classmethod
    @abstractmethod
    def from_config(cls, config: dict) -> AdapterBase:
        """Erstellt eine Adapter-Instanz aus einem Config-Dict."""
        ...

    # --- Interface ---

    @abstractmethod
    def load(self) -> list[StudentRecord]:
        """Liest Schülerdaten aus der Quelle und gibt sie im einheitlichen Format zurück."""
        ...

    def load_teachers(self) -> list[TeacherRecord]:
        """Liest Lehrerdaten aus der Quelle. Standard: leere Liste."""
        return []

    # --- Optionales Interface ---

    def test_connection(self) -> tuple[bool, str]:
        """Testet die Verbindung zur Datenquelle. Standard: nicht unterstützt."""
        return (False, "Verbindungstest nicht unterstützt für diesen Adapter.")

    def supports_write_back(self) -> bool:
        """Gibt an ob der Adapter Daten zurückschreiben kann."""
        return False

    def write_back(self, updates: list[dict]) -> list[dict]:
        """Schreibt Daten zurück (z.B. generierte Emails).

        updates: [{"school_internal_id": "123", "email": "m.mueller@schule.de"}]
        returns: [{"school_internal_id": "123", "success": True, "message": ""}]
        """
        raise NotImplementedError("Adapter unterstützt kein Write-back.")
