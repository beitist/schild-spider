"""Hilfsfunktionen für Dateipfade — kompatibel mit PyInstaller-Bundles.

PyInstaller entpackt gebundelte Dateien (--add-data) in ein temporäres
Verzeichnis, dessen Pfad in ``sys._MEIPASS`` gespeichert wird.
Bei normaler Ausführung wird stattdessen das Projektverzeichnis verwendet.

Zusätzlich: ``data_dir()`` liefert das Verzeichnis für settings.json und
Logs — neben der EXE, oder (falls dort nicht schreibbar, z.B. unter
"Program Files") im User-Datenverzeichnis.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _base_dir() -> Path:
    """Gibt das Basisverzeichnis zurück — entweder das PyInstaller-
    Temp-Verzeichnis (``sys._MEIPASS``) oder das Projektverzeichnis."""
    if getattr(sys, "frozen", False):
        # PyInstaller-Bundle: Dateien liegen in _MEIPASS
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    # Normale Ausführung: Projektverzeichnis (Elternverzeichnis von core/)
    return Path(__file__).resolve().parent.parent


def asset_path(filename: str) -> Path:
    """Löst einen Dateinamen im ``assets/``-Ordner zu einem absoluten Pfad auf.

    Funktioniert sowohl bei normaler Ausführung als auch innerhalb einer
    PyInstaller-gepackten EXE (``--add-data``).
    """
    return _base_dir() / "assets" / filename


def app_dir() -> Path:
    """Verzeichnis der EXE (frozen) bzw. des Projekts (Entwicklung).

    Bewusst NICHT das Arbeitsverzeichnis — das hängt davon ab, von wo
    die EXE gestartet wurde (Doppelklick vs. Verknüpfung vs. Terminal).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_writable(directory: Path) -> bool:
    """Echte Schreibprobe — os.access() ist unter Windows unzuverlässig."""
    probe = directory / ".spider-write-test"
    try:
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False


_data_dir_cache: Path | None = None


def data_dir() -> Path:
    """Beschreibbares Verzeichnis für settings.json, spider.log etc.

    Normalfall: das App-Verzeichnis (entpackter Ordner neben der EXE).
    Fallback: User-Datenverzeichnis, wenn das App-Verzeichnis
    schreibgeschützt ist (z.B. EXE unter ``C:\\Program Files``).
    """
    global _data_dir_cache
    if _data_dir_cache is not None:
        return _data_dir_cache

    base = app_dir()
    if _is_writable(base):
        _data_dir_cache = base
        return base

    if sys.platform == "win32":
        root = Path(
            os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        )
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(
            os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
        )

    fallback = root / "SchildSpider"
    fallback.mkdir(parents=True, exist_ok=True)
    _data_dir_cache = fallback
    return fallback


def settings_path() -> Path:
    """Absoluter Pfad zur settings.json."""
    return data_dir() / "settings.json"
