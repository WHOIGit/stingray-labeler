"""Background image preview adjustment worker."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage
from PIL import Image, ImageEnhance, ImageOps


class WorkerSignals(QObject):
    finished = Signal(int, object, float)
    levels_ready = Signal(int, int, int)


def apply_image_adjustments(image: Image.Image, brightness: float, contrast: float,
                            gamma: float, invert: bool, black_point: int,
                            white_point: int) -> Image.Image:
    """Apply the same non-destructive display adjustments to previews and exports."""
    if black_point > 0 or white_point < 255:
        span = max(1, white_point - black_point)
        levels = [round(max(0, min(255, (value - black_point) * 255 / span)))
                  for value in range(256)]
        image = image.point(levels * 3)
    image = ImageEnhance.Brightness(image).enhance(brightness)
    image = ImageEnhance.Contrast(image).enhance(contrast)
    if gamma != 1.0:
        gamma_table = [round(255 * (value / 255) ** (1 / gamma)) for value in range(256)]
        image = image.point(gamma_table * 3)
    if invert:
        image = ImageOps.invert(image)
    return image


class AutoLevelsWorker(QRunnable):
    """Estimate black and white points from one image only when requested."""

    def __init__(self, generation: int, image: Image.Image):
        super().__init__()
        self.generation = generation
        self.image = image
        self.signals = WorkerSignals()

    def run(self) -> None:
        scale = min(1.0, 2200 / self.image.width, 2200 / self.image.height)
        size = (max(1, round(self.image.width * scale)), max(1, round(self.image.height * scale)))
        preview = self.image.resize(size, Image.Resampling.BILINEAR) if size != self.image.size else self.image
        histogram = preview.convert("L").histogram()
        count = sum(histogram)

        def percentile(target: int) -> int:
            running = 0
            for value, frequency in enumerate(histogram):
                running += frequency
                if running >= target:
                    return value
            return 255

        black = percentile(max(1, round(count * 0.01)))
        white = percentile(max(1, round(count * 0.99)))
        if white <= black:
            white = min(255, black + 1)
            black = min(black, white - 1)
        self.signals.levels_ready.emit(self.generation, black, white)


class ImageAdjustmentWorker(QRunnable):
    """Adjust a reduced image preview off the UI thread."""

    def __init__(self, generation: int, image: Image.Image, brightness: float,
                 contrast: float, gamma: float, invert: bool, black_point: int,
                 white_point: int, cancelled: threading.Event):
        super().__init__()
        self.generation = generation
        self.image = image
        self.brightness = brightness
        self.contrast = contrast
        self.gamma = gamma
        self.invert = invert
        self.black_point = black_point
        self.white_point = white_point
        self.cancelled = cancelled
        self.signals = WorkerSignals()

    def run(self) -> None:
        scale = min(1.0, 2200 / self.image.width, 2200 / self.image.height)
        size = (max(1, round(self.image.width * scale)), max(1, round(self.image.height * scale)))
        preview = self.image.resize(size, Image.Resampling.BILINEAR) if size != self.image.size else self.image
        scale = self.image.width / preview.width
        if self.cancelled.is_set():
            self.signals.finished.emit(self.generation, None, scale)
            return
        preview = apply_image_adjustments(
            preview, self.brightness, self.contrast, self.gamma, self.invert,
            self.black_point, self.white_point,
        )
        if self.cancelled.is_set():
            self.signals.finished.emit(self.generation, None, scale)
            return
        raw = preview.tobytes("raw", "RGB")
        adjusted = QImage(
            raw, preview.width, preview.height, preview.width * 3,
            QImage.Format.Format_RGB888,
        ).copy()
        self.signals.finished.emit(self.generation, adjusted, scale)
