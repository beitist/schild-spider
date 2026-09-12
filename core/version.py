"""App-Metadaten an einer Stelle.

Eigenes Modul, damit sowohl ``main.py`` als auch die GUI darauf zugreifen
können. Würde das Hauptfenster die Version aus ``main.py`` importieren,
entstünde ein Import-Zirkel (main importiert die GUI).
"""

from __future__ import annotations

APP_NAME = "Schild Spider"
APP_VERSION = "0.7.6-dev"
APP_COPYRIGHT = "© 2025–2026"
APP_LICENSE = "GPL v3"


def version_label() -> str:
    """Kurzform für Fenstertitel und Logzeilen: 'Schild Spider 0.7.6-dev'."""
    return f"{APP_NAME} {APP_VERSION}"
