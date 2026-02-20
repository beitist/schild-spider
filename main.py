"""Schild Spider — Startpunkt der Anwendung."""

import sys

from PySide6.QtWidgets import QApplication

from gui.mainwindow import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Schild Spider")
    app.setOrganizationName("SchildSpider")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
