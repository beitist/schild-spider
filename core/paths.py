"""Hilfsfunktionen für Dateipfade — kompatibel mit PyInstaller-Bundles.

PyInstaller entpackt gebundelte Dateien (--add-data) in ein temporäres
Verzeichnis, dessen Pfad in ``sys._MEIPASS`` gespeichert wird.
Bei normaler Ausführung wird stattdessen das Projektverzeichnis verwendet.
"""

from __future__ import annotations

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
