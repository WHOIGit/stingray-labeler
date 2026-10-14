"""Application startup shared by command-line and GUI launchers."""

import faulthandler
import sys
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication, QMessageBox

from .window import ImageAnnotator


def _error_log() -> Path:
    """Where errors are written on this PC, beside the autosave backups (e.g. %LOCALAPPDATA%\\StingrayLabeler)."""
    base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation))
    folder = base / "StingrayLabeler"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "error.log"


def _log_errors() -> None:
    """Write every unexpected error to the error log, so a crash can be traced: Python errors with their
    traceback (and a message, the app keeps running), and hard crashes through faulthandler."""
    log_path = _error_log()
    stream = log_path.open("a", encoding="utf-8")  # kept open for faulthandler, for the life of the app
    faulthandler.enable(stream)

    def report(kind: str, error_type, error, trace) -> None:
        text = "".join(traceback.format_exception(error_type, error, trace))
        stream.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {kind} ===\n{text}")
        stream.flush()
        sys.__stderr__ and sys.__stderr__.write(text)
        if QApplication.instance() is not None and threading.current_thread() is threading.main_thread():
            QMessageBox.critical(None, "Unexpected error",
                                 f"{error_type.__name__}: {error}\n\nThe details were written to {log_path}. "
                                 "Save your work if you can.")

    sys.excepthook = lambda error_type, error, trace: report("error", error_type, error, trace)
    threading.excepthook = lambda args: report(f"error in thread {args.thread.name if args.thread else ''}",
                                               args.exc_type, args.exc_value, args.exc_traceback)


def main() -> int:
    app = QApplication(sys.argv[:1])
    _log_errors()
    window = ImageAnnotator()
    window.show()
    app.installEventFilter(window)
    return app.exec()
