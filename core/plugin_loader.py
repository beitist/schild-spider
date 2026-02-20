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


# --- Settings I/O ---


def load_settings(settings_path: str | Path = "settings.json") -> dict:
    path = Path(settings_path)
    if not path.exists():
        raise FileNotFoundError(f"Settings nicht gefunden: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_settings(
    settings: dict, settings_path: str | Path = "settings.json"
) -> None:
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)
