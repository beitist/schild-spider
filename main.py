"""Schild Spider — Startpunkt der Anwendung."""

import faulthandler
import logging
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from core.paths import asset_path
from gui.mainwindow import MainWindow

# --- App-Metadaten ---
APP_NAME = "Schild Spider"
APP_VERSION = "0.4.3"
APP_COPYRIGHT = "© 2025–2026"
APP_LICENSE = "GPL v3"


def _build_splash_pixmap() -> QPixmap | None:
    """Erzeugt das Splash-Pixmap: Logo + Name, Version, Copyright, Lizenz.

    Zeichnet den Text mit QPainter unterhalb des Logos auf eine
    erweiterte Pixmap, damit alles sauber zentriert ist.
    """
    logo = QPixmap(str(asset_path("schild-spider.png")))
    if logo.isNull():
        return None

    # Logo auf maximal 400px skalieren (etwas Platz für den Text lassen)
    logo = logo.scaledToWidth(400, Qt.TransformationMode.SmoothTransformation)

    # Text-Zeilen die unter dem Logo erscheinen
    lines = [
        (APP_NAME, QFont("Segoe UI", 18, QFont.Weight.Bold)),
        (f"Version {APP_VERSION}", QFont("Segoe UI", 11)),
        (f"{APP_COPYRIGHT}  •  Lizenz: {APP_LICENSE}", QFont("Segoe UI", 9)),
    ]

    # Vertikalen Platzbedarf für den Textblock berechnen
    line_spacing = 6  # Pixel zwischen Zeilen
    text_height = sum(
        # Grobe Schätzung: Schriftgröße × 1.5 als Zeilenhöhe
        int(font.pointSize() * 1.8)
        for _, font in lines
    ) + line_spacing * (len(lines) - 1)
    padding = 20  # Abstand Logo → Text und unten

    # Neue Pixmap: Logo-Breite × (Logo + Text + Padding)
    total_width = logo.width()
    total_height = logo.height() + padding + text_height + padding
    splash_pm = QPixmap(total_width, total_height)
    splash_pm.fill(QColor("white"))

    painter = QPainter(splash_pm)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    # Logo oben zentriert zeichnen
    logo_x = (total_width - logo.width()) // 2
    painter.drawPixmap(logo_x, 0, logo)

    # Text zeilenweise unter dem Logo zeichnen
    y = logo.height() + padding
    for text, font in lines:
        painter.setFont(font)
        painter.setPen(QColor("#333333"))
        rect = painter.fontMetrics().boundingRect(text)
        x = (total_width - rect.width()) // 2
        painter.drawText(x, y + rect.height(), text)
        y += rect.height() + line_spacing

    painter.end()
    return splash_pm


def _setup_logging() -> None:
    """Konfiguriert Logging: immer in spider.log, bei Console auch auf stdout."""
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Datei-Handler: spider.log im Arbeitsverzeichnis (überschreibt beim Start)
    file_handler = logging.FileHandler("spider.log", mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))

    # Stream-Handler: stdout (nützlich bei Entwicklung, leer bei --windowed)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.setFormatter(logging.Formatter(log_format, datefmt=date_format))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # faulthandler: schreibt nativen Crash-Traceback (Segfault etc.) in eigene Datei.
    # File-Handle wird als Attribut gespeichert damit er nicht vom GC geschlossen wird.
    _setup_logging._crash_fh = open("spider_crash.log", "w")  # noqa: SIM115
    faulthandler.enable(file=_setup_logging._crash_fh)


def _install_exception_hook() -> None:
    """Fängt unbehandelte Exceptions in Main- UND Worker-Threads."""
    original_hook = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):  # noqa: ANN001
        logging.critical(
            "Unbehandelte Exception", exc_info=(exc_type, exc_value, exc_tb)
        )
        original_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = hook

    # Thread-Exceptions (Python 3.8+): fängt Exceptions in Worker-Threads
    def thread_hook(args: threading.ExceptHookArgs) -> None:
        logging.critical(
            "Unbehandelte Exception in Thread '%s'",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = thread_hook


def main() -> None:
    _setup_logging()
    _install_exception_hook()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("SchildSpider")
    app.setApplicationVersion(APP_VERSION)

    # --- App-Icon (Taskleiste + Fenstertitel) ---
    # PyInstaller --icon setzt nur das EXE-Datei-Icon im Explorer.
    # Für Taskleiste und Fenstertitel muss das Icon zur Laufzeit geladen werden.
    icon_file = asset_path("icon.ico")
    if icon_file.exists():
        app.setWindowIcon(QIcon(str(icon_file)))

    # --- Splash Screen ---
    splash_pixmap = _build_splash_pixmap()
    if splash_pixmap is not None:
        splash = QSplashScreen(splash_pixmap)
        splash.show()
        app.processEvents()
    else:
        splash = None

    # --- Erststart: Setup-Wizard wenn keine settings.json vorhanden ---
    if not Path("settings.json").exists():
        # Splash ausblenden bevor der Wizard erscheint
        if splash is not None:
            splash.close()
            splash = None

        from gui.setup_wizard import SetupWizard

        wizard = SetupWizard()
        if wizard.exec() != SetupWizard.DialogCode.Accepted:
            sys.exit(0)

    window = MainWindow()
    window.show()

    # Splash ausblenden sobald das Hauptfenster steht
    if splash is not None:
        splash.finish(window)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
