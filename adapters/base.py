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
