"""Schild Spider — Startpunkt der Anwendung."""

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen

from core.paths import asset_path
from gui.mainwindow import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Schild Spider")
    app.setOrganizationName("SchildSpider")

    # --- App-Icon (Taskleiste + Fenstertitel) ---
    # PyInstaller --icon setzt nur das EXE-Datei-Icon im Explorer.
    # Für Taskleiste und Fenstertitel muss das Icon zur Laufzeit geladen werden.
    icon_file = asset_path("icon.ico")
    if icon_file.exists():
        app.setWindowIcon(QIcon(str(icon_file)))

    # --- Splash Screen ---
    # Zeigt das Logo während die App im Hintergrund lädt.
    # Schließt sich automatisch sobald das Hauptfenster sichtbar wird.
    splash_pixmap = QPixmap(str(asset_path("schild-spider.png")))
    if not splash_pixmap.isNull():
        # Skaliert auf eine angenehme Splash-Größe (max 480px breit)
        splash_pixmap = splash_pixmap.scaledToWidth(
            480, Qt.TransformationMode.SmoothTransformation
        )
        splash = QSplashScreen(splash_pixmap)
        splash.show()
        # Sofort zeichnen, damit der Splash sichtbar wird bevor
        # das Hauptfenster (und Plugins etc.) geladen werden
        app.processEvents()
    else:
        splash = None

    window = MainWindow()
    window.show()

    # Splash ausblenden sobald das Hauptfenster steht
    if splash is not None:
        splash.finish(window)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
