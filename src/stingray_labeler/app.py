"""Application startup shared by command-line and GUI launchers."""

import sys
from PySide6.QtWidgets import QApplication

from .window import ImageAnnotator


def main() -> int:
    app = QApplication(sys.argv[:1])
    window = ImageAnnotator()
    window.show()
    app.installEventFilter(window)
    return app.exec()

