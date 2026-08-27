from __future__ import annotations

import importlib
import json
from pathlib import Path

from adapters.base import AdapterBase
from core.paths import settings_path as default_settings_path
from plugins.base import PluginBase

# --- Registries ---
# Neue Adapter/Plugins hier eintragen — den Rest macht from_config().

_ADAPTER_REGISTRY: dict[str, tuple[str, str]] = {
    "schild_csv": ("adapters.schild_csv", "SchildCsvAdapter"),
    "schild_db": ("adapters.schild_db", "SchildDbAdapter"),
}

_PLUGIN_REGISTRY: dict[str, tuple[str, str]] = {
    "hagen_id": ("plugins.hagen_id", "HagenIdPlugin"),
    "hagen_id_lehrer": ("plugins.hagen_id_lehrer", "HagenIdLehrerPlugin"),
    "m365": ("plugins.m365", "M365Plugin"),
    "moodle": ("plugins.moodle", "MoodlePlugin"),
    "webuntis": ("plugins.webuntis", "WebUntisPlugin"),
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
# v10: adapter_configs (Configs ALLER Adapter bleiben beim Wechsel erhalten)
# v11: hagen_id_lehrer Plugin + person_typ im schild_db Adapter
SETTINGS_VERSION = 11


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

    # Defaults für ALLE registrierten Adapter — so überlebt die Config
    # eines Adapters den Wechsel auf einen anderen.
    adapter_configs: dict = {}
    for key in _ADAPTER_REGISTRY:
        adapter_class = get_adapter_class(key)
        if adapter_class is None:
            continue
        cfg: dict = {}
        for field in adapter_class.config_schema():
            cfg.setdefault(field.key, field.default)
        adapter_configs[key] = cfg

    # Aktiver Adapter (flache Sektion, wird von load_adapter() gelesen)
    adapter_cfg: dict = {"type": adapter_type, **adapter_configs.get(adapter_type, {})}

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
        "adapter_configs": adapter_configs,
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
    old_adapter_type = old_settings.get("adapter", {}).get("type", "schild_csv")
    new_settings = generate_default_settings(
        school_name=old_settings.get("school_name", ""),
        adapter_type=old_adapter_type,
    )

    # Top-Level-Felder übernehmen
    if "debug_class_filter" in old_settings:
        new_settings["debug_class_filter"] = old_settings["debug_class_filter"]

    # Adapter-Configs übernehmen: erst gespeicherte adapter_configs (ab v10),
    # dann die flache adapter-Sektion (überschreibt den aktiven Adapter —
    # deckt auch Alt-Settings vor v10 ab).
    old_configs = old_settings.get("adapter_configs", {})
    old_adapter = old_settings.get("adapter", {})
    for a_key, new_cfg in new_settings["adapter_configs"].items():
        source = old_configs.get(a_key, {})
        for field_key in new_cfg:
            if field_key in source:
                new_cfg[field_key] = source[field_key]
        if a_key == old_adapter_type:
            for field_key in new_cfg:
                if field_key in old_adapter:
                    new_cfg[field_key] = old_adapter[field_key]

    # Flache adapter-Sektion aus der Config des aktiven Adapters aufbauen
    new_settings["adapter"] = {
        "type": old_adapter_type,
        **new_settings["adapter_configs"].get(old_adapter_type, {}),
    }

    # Plugin-Configs übernehmen (enabled-Status + Feld-Werte)
    old_plugins = old_settings.get("plugins", {})
    for plugin_key, new_plugin_cfg in new_settings["plugins"].items():
        if plugin_key in old_plugins:
            old_plugin_cfg = old_plugins[plugin_key]
            # Alle im neuen Schema existierenden Felder aus alten Settings
            for field_key in new_plugin_cfg:
                if field_key in old_plugin_cfg:
                    new_plugin_cfg[field_key] = old_plugin_cfg[field_key]

    # Lehrer-Plugin nutzt dieselbe API wie der Schüler-Sync — Zugangsdaten
    # beim ersten Auftauchen übernehmen, statt sie erneut abzutippen.
    lehrer_cfg = new_settings["plugins"].get("hagen_id_lehrer")
    hagen_cfg = new_settings["plugins"].get("hagen_id", {})
    if lehrer_cfg is not None:
        for field_key in ("api_url", "api_key"):
            if not lehrer_cfg.get(field_key) and hagen_cfg.get(field_key):
                lehrer_cfg[field_key] = hagen_cfg[field_key]

    # Failsafe-Werte übernehmen
    old_failsafe = old_settings.get("failsafe", {})
    for key in new_settings["failsafe"]:
        if key in old_failsafe:
            new_settings["failsafe"][key] = old_failsafe[key]

    return new_settings


def load_settings(settings_path: str | Path | None = None) -> dict:
    """Lädt Settings und migriert bei Bedarf auf die aktuelle Version.

    Ohne Pfad wird der Standard-Speicherort verwendet (neben der EXE
    bzw. im Projektverzeichnis, siehe ``core.paths.settings_path``).
    Gibt FileNotFoundError zurück wenn keine settings.json existiert —
    der Aufrufer (main.py) zeigt dann den Setup-Wizard.
    """
    path = Path(settings_path) if settings_path else default_settings_path()
    if not path.exists():
        raise FileNotFoundError(f"Settings nicht gefunden: {path}")
    with open(path, encoding="utf-8") as f:
        settings = json.load(f)

    # Automatische Migration bei veralteter Version
    stored_version = settings.get("settings_version", 0)
    if stored_version < SETTINGS_VERSION:
        settings = migrate_settings(settings)
        save_settings(settings, path)

    return settings


def save_settings(settings: dict, settings_path: str | Path | None = None) -> None:
    """Speichert Settings als JSON. Setzt immer die aktuelle Version."""
    path = Path(settings_path) if settings_path else default_settings_path()
    settings["settings_version"] = SETTINGS_VERSION
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)
