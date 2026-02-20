from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StudentRecord:
    """Einheitliches Schüler-Format (Output aller Adapter)."""

    school_internal_id: str
    first_name: str
    last_name: str
    dob: str  # YYYY-MM-DD
    email: str
    class_name: str
    photo_path: str | None = None


@dataclass
class ChangeSet:
    """Ergebnis der Diff-Berechnung zwischen Quelle und Zielsystem."""

    new: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)
    suspended: list[str] = field(default_factory=list)  # school_internal_ids
    photo_updates: list[dict] = field(default_factory=list)

    total_in_source: int = 0
    total_in_target: int = 0
    suspend_percentage: float = 0.0
    requires_force: bool = False


@dataclass
class SyncResult:
    """Ergebnis eines Plugin-Apply-Aufrufs."""

    success: bool
    school_internal_id: str
    action: str  # "new", "change", "suspend"
    message: str = ""


@dataclass
class ConfigField:
    """Beschreibt ein Konfigurationsfeld eines Plugins."""

    key: str  # Schlüssel in settings.json (z.B. "api_url")
    label: str  # Anzeigename (z.B. "API URL")
    field_type: str = "text"  # "text", "password", "url", "path", "dir"
    required: bool = True
    placeholder: str = ""
    default: str = ""
