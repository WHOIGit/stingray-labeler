"""Background image preview adjustment worker."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage
from PIL import Image, ImageEnhance


class WorkerSignals(QObject):
    finished = Signal(int, object, float)


class ImageAdjustmentWorker(QRunnable):
    """Adjust a reduced image preview off the UI thread."""

    def __init__(self, generation: int, image: Image.Image, brightness: float,
                 contrast: float, cancelled: threading.Event):
        super().__init__()
        self.generation = generation
        self.image = image
        self.brightness = brightness
        self.contrast = contrast
        self.cancelled = cancelled
        self.signals = WorkerSignals()

    def run(self) -> None:
        preview = self.image.copy()
        preview.thumbnail((2200, 2200), Image.Resampling.BILINEAR)
        scale = self.image.width / preview.width
        if self.cancelled.is_set():
            self.signals.finished.emit(self.generation, None, scale)
            return
        preview = ImageEnhance.Brightness(preview).enhance(self.brightness)
        if self.cancelled.is_set():
            self.signals.finished.emit(self.generation, None, scale)
            return
        preview = ImageEnhance.Contrast(preview).enhance(self.contrast)
        raw = preview.tobytes("raw", "RGB")
        adjusted = QImage(
            raw, preview.width, preview.height, preview.width * 3,
            QImage.Format.Format_RGB888,
        ).copy()
        self.signals.finished.emit(self.generation, adjusted, scale)
