from __future__ import annotations

import importlib
import json
from pathlib import Path

from adapters.base import AdapterBase
from plugins.base import PluginBase

# --- Registries ---
# Neue Adapter/Plugins hier eintragen — den Rest macht from_config().

_ADAPTER_REGISTRY: dict[str, tuple[str, str]] = {
    "schild_csv": ("adapters.schild_csv", "SchildCsvAdapter"),
}

_PLUGIN_REGISTRY: dict[str, tuple[str, str]] = {
    "hagen_id": ("plugins.hagen_id", "HagenIdPlugin"),
}


# --- Adapter ---


def get_adapter_registry() -> dict[str, tuple[str, str]]:
    return dict(_ADAPTER_REGISTRY)


def get_adapter_class(name: str) -> type[AdapterBase] | None:
    if name not in _ADAPTER_REGISTRY:
        return None
    module_path, class_name = _ADAPTER_REGISTRY[name]
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def load_adapter(settings: dict) -> AdapterBase:
    """Erstellt den konfigurierten Adapter aus den Settings."""
    adapter_cfg = settings.get("adapter", {})
    adapter_type = adapter_cfg.get("type", "schild_csv")

    adapter_class = get_adapter_class(adapter_type)
    if adapter_class is None:
        raise ValueError(f"Unbekannter Adapter: {adapter_type}")

    return adapter_class.from_config(adapter_cfg)


# --- Plugins ---


def get_plugin_registry() -> dict[str, tuple[str, str]]:
    return dict(_PLUGIN_REGISTRY)


def get_plugin_class(name: str) -> type[PluginBase] | None:
    if name not in _PLUGIN_REGISTRY:
        return None
    module_path, class_name = _PLUGIN_REGISTRY[name]
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def load_plugins(settings: dict) -> list[tuple[str, PluginBase]]:
    """Lädt alle aktiven Plugins. Gibt (name, instance) Tupel zurück."""
    plugins_config = settings.get("plugins", {})
    loaded: list[tuple[str, PluginBase]] = []

    for name, config in plugins_config.items():
        if not config.get("enabled", False):
            continue

        plugin_class = get_plugin_class(name)
        if plugin_class is None:
            continue

        instance = plugin_class.from_config(config)
        loaded.append((name, instance))

    return loaded


# --- Settings I/O + Versionierung ---

# Wird bei jeder strukturellen Änderung am Settings-Schema hochgezählt.
# load_settings() prüft dies und migriert automatisch.
SETTINGS_VERSION = 2


def generate_default_settings(
    school_name: str = "",
    adapter_type: str = "schild_csv",
    enabled_plugins: list[str] | None = None,
) -> dict:
    """Erzeugt ein vollständiges Settings-Dict mit aktuellem Schema.

    Wird beim Erststart (Setup-Wizard) und bei der Migration verwendet.
    Alle registrierten Plugins werden als Einträge angelegt;
    ``enabled_plugins`` steuert welche davon aktiviert sind.
    """
    if enabled_plugins is None:
        enabled_plugins = []

    # Adapter-Defaults aus dem Schema der gewählten Adapter-Klasse
    adapter_cfg: dict = {"type": adapter_type}
    adapter_class = get_adapter_class(adapter_type)
    if adapter_class is not None:
        for field in adapter_class.config_schema():
            adapter_cfg.setdefault(field.key, field.default)

    # Plugin-Defaults aus den Schemata aller registrierten Plugins
    plugins_cfg: dict = {}
    for key in _PLUGIN_REGISTRY:
        plugin_class = get_plugin_class(key)
        if plugin_class is None:
            continue
        plugin_entry: dict = {"enabled": key in enabled_plugins}
        for field in plugin_class.config_schema():
            plugin_entry.setdefault(field.key, field.default)
        plugins_cfg[key] = plugin_entry

    return {
        "settings_version": SETTINGS_VERSION,
        "school_name": school_name,
        "adapter": adapter_cfg,
        "plugins": plugins_cfg,
        "failsafe": {
            "max_suspend_percentage": 15,
            "require_confirmation_above": 50,
        },
    }


def migrate_settings(old_settings: dict) -> dict:
    """Migriert existierende Settings auf das aktuelle Schema.

    Übernimmt Schulname, Adapter-Config und Plugin-Einstellungen.
    Neue Plugins/Felder werden mit Defaults ergänzt, veraltete Einträge
    werden entfernt. Die ``settings_version`` wird hochgesetzt.
    """
    # Neues Default-Skelett erzeugen
    new_settings = generate_default_settings(
        school_name=old_settings.get("school_name", ""),
        adapter_type=old_settings.get("adapter", {}).get("type", "schild_csv"),
    )

    # Adapter-Config übernehmen (nur Felder die im neuen Schema existieren)
    old_adapter = old_settings.get("adapter", {})
    for key in new_settings["adapter"]:
        if key in old_adapter:
            new_settings["adapter"][key] = old_adapter[key]

    # Plugin-Configs übernehmen (enabled-Status + Feld-Werte)
    old_plugins = old_settings.get("plugins", {})
    for plugin_key, new_plugin_cfg in new_settings["plugins"].items():
        if plugin_key in old_plugins:
            old_plugin_cfg = old_plugins[plugin_key]
            # Alle im neuen Schema existierenden Felder aus alten Settings
            for field_key in new_plugin_cfg:
                if field_key in old_plugin_cfg:
                    new_plugin_cfg[field_key] = old_plugin_cfg[field_key]

    # Failsafe-Werte übernehmen
    old_failsafe = old_settings.get("failsafe", {})
    for key in new_settings["failsafe"]:
        if key in old_failsafe:
            new_settings["failsafe"][key] = old_failsafe[key]

    return new_settings


def load_settings(settings_path: str | Path = "settings.json") -> dict:
    """Lädt Settings und migriert bei Bedarf auf die aktuelle Version.

    Gibt FileNotFoundError zurück wenn keine settings.json existiert —
    der Aufrufer (main.py) zeigt dann den Setup-Wizard.
    """
    path = Path(settings_path)
    if not path.exists():
        raise FileNotFoundError(f"Settings nicht gefunden: {path}")
    with open(path, encoding="utf-8") as f:
        settings = json.load(f)

    # Automatische Migration bei veralteter Version
    stored_version = settings.get("settings_version", 0)
    if stored_version < SETTINGS_VERSION:
        settings = migrate_settings(settings)
        save_settings(settings, settings_path)

    return settings


def save_settings(settings: dict, settings_path: str | Path = "settings.json") -> None:
    """Speichert Settings als JSON. Setzt immer die aktuelle Version."""
    settings["settings_version"] = SETTINGS_VERSION
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)
