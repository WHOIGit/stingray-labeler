"""Application startup shared by command-line and GUI launchers."""

import sys
from PySide6.QtWidgets import QApplication

from .window import CocoAnnotator


def main() -> int:
    app = QApplication(sys.argv[:1])
    window = CocoAnnotator()
    window.show()
    app.installEventFilter(window)
    return app.exec()
