"""Image decoding, preview, and display-adjustment pipeline.

Each image is decoded once on a worker thread into a full-resolution copy and a
reduced preview. Display adjustments are folded into one 256-entry lookup table
and applied in a single pass, to the preview for the main view, to a cropped
full-resolution patch when zoomed in past the preview, and to exports.
"""

from __future__ import annotations

import struct
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage
from PIL import Image

PREVIEW_MAX_SIDE = 2200


@dataclass(frozen=True)
class Adjustments:
    brightness: float = 1.0
    contrast: float = 1.0
    gamma: float = 1.0
    invert: bool = False
    black_point: int = 0
    white_point: int = 255

    @property
    def is_identity(self) -> bool:
        return self == Adjustments()


@dataclass
class LoadedImage:
    """Decoded image data shared by the view, the detail patch, and exports."""

    full: Image.Image
    preview: Image.Image
    preview_scale: float
    preview_histogram: list[int]
    preview_qimage: QImage


def to_qimage(image: Image.Image) -> QImage:
    raw = image.tobytes("raw", "RGB")
    return QImage(raw, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888).copy()


def make_preview(image: Image.Image) -> tuple[Image.Image, float]:
    scale = min(1.0, PREVIEW_MAX_SIDE / image.width, PREVIEW_MAX_SIDE / image.height)
    if scale >= 1.0:
        return image, 1.0
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    preview = image.resize(size, Image.Resampling.BILINEAR)
    return preview, image.width / preview.width


def _f32(value: float) -> float:
    return struct.unpack("f", struct.pack("f", value))[0]


def _blend(base: int, value: int, factor: float) -> int:
    """Match Pillow's ImagingBlend: float32 math, truncated and clipped to 0..255."""
    result = _f32(base + _f32(_f32(factor) * (value - base)))
    if result <= 0:
        return 0
    return int(result) if result < 255 else 255


def _pre_contrast_lut(settings: Adjustments) -> list[int]:
    """Levels then brightness: the stages before contrast needs the image mean."""
    black, white = settings.black_point, settings.white_point
    span = max(1, white - black)
    table = []
    for value in range(256):
        if black > 0 or white < 255:
            value = round(max(0, min(255, (value - black) * 255 / span)))
        table.append(_blend(0, value, settings.brightness))
    return table


def contrast_mean(settings: Adjustments, histogram: list[int]) -> int:
    """Grey mean the contrast stage pivots on, computed from an RGB histogram."""
    pre = _pre_contrast_lut(settings)
    means = []
    for channel in range(3):
        counts = histogram[channel * 256:(channel + 1) * 256]
        total = sum(counts) or 1
        means.append(sum(pre[value] * count for value, count in enumerate(counts)) / total)
    # Pillow's RGB->L weights, as used by ImageEnhance.Contrast.
    grey = (means[0] * 19595 + means[1] * 38470 + means[2] * 7471) / 65536
    return int(grey + 0.5)


def adjustment_lut(settings: Adjustments, mean: int) -> list[int]:
    table = []
    for value in _pre_contrast_lut(settings):
        value = _blend(mean, value, settings.contrast)
        if settings.gamma != 1.0:
            value = round(255 * (value / 255) ** (1 / settings.gamma))
        if settings.invert:
            value = 255 - value
        table.append(value)
    return table


def apply_image_adjustments(image: Image.Image, settings: Adjustments,
                            histogram: list[int] | None = None) -> Image.Image:
    """Apply display adjustments in one pass.

    ``histogram`` sets the contrast pivot; pass the preview histogram so a crop
    is adjusted exactly like the whole image around it.
    """
    if settings.is_identity:
        return image
    mean = 0
    if settings.contrast != 1.0:
        mean = contrast_mean(settings, histogram if histogram is not None else image.histogram())
    return image.point(adjustment_lut(settings, mean) * 3)


def auto_levels(histogram_l: list[int]) -> tuple[int, int]:
    count = sum(histogram_l)

    def percentile(target: int) -> int:
        running = 0
        for value, frequency in enumerate(histogram_l):
            running += frequency
            if running >= target:
                return value
        return 255

    black = percentile(max(1, round(count * 0.01)))
    white = percentile(max(1, round(count * 0.99)))
    if white <= black:
        white = min(255, black + 1)
        black = min(black, white - 1)
    return black, white


class PipelineSignals(QObject):
    """Owned by the window so results never arrive from a deleted sender."""

    image_loaded = Signal(object, object)  # image_id, LoadedImage
    image_failed = Signal(object, str)  # image_id, message
    preview_adjusted = Signal(int, object)  # generation, QImage | None
    detail_ready = Signal(int, object, int, int)  # generation, QImage, left, top
    levels_ready = Signal(int, int, int)  # generation, black, white


class ImageLoadWorker(QRunnable):
    """Decode an image and build its preview off the UI thread."""

    def __init__(self, image_id, path: Path, signals: PipelineSignals):
        super().__init__()
        self.image_id = image_id
        self.path = path
        self.signals = signals

    def run(self) -> None:
        try:
            with Image.open(self.path) as source:
                full = source.convert("RGB")
            full.load()
            preview, scale = make_preview(full)
            loaded = LoadedImage(full, preview, scale, preview.histogram(), to_qimage(preview))
        except Exception as error:  # noqa: BLE001 - reported to the user as a load failure
            self.signals.image_failed.emit(self.image_id, f"Could not open image:\n{self.path}\n\n{error}")
            return
        self.signals.image_loaded.emit(self.image_id, loaded)


class AutoLevelsWorker(QRunnable):
    """Estimate black and white points from the cached preview."""

    def __init__(self, generation: int, preview: Image.Image, signals: PipelineSignals):
        super().__init__()
        self.generation = generation
        self.preview = preview
        self.signals = signals

    def run(self) -> None:
        black, white = auto_levels(self.preview.convert("L").histogram())
        self.signals.levels_ready.emit(self.generation, black, white)


class PreviewAdjustmentWorker(QRunnable):
    """Adjust the already-reduced preview; no per-change resize."""

    def __init__(self, generation: int, loaded: LoadedImage, settings: Adjustments,
                 cancelled: threading.Event, signals: PipelineSignals):
        super().__init__()
        self.generation = generation
        self.loaded = loaded
        self.settings = settings
        self.cancelled = cancelled
        self.signals = signals

    def run(self) -> None:
        if self.cancelled.is_set():
            self.signals.preview_adjusted.emit(self.generation, None)
            return
        if self.settings.is_identity:
            self.signals.preview_adjusted.emit(self.generation, self.loaded.preview_qimage)
            return
        adjusted = apply_image_adjustments(
            self.loaded.preview, self.settings, self.loaded.preview_histogram
        )
        if self.cancelled.is_set():
            self.signals.preview_adjusted.emit(self.generation, None)
            return
        self.signals.preview_adjusted.emit(self.generation, to_qimage(adjusted))


class DetailWorker(QRunnable):
    """Render a full-resolution patch of the visible area when zoomed past the preview."""

    def __init__(self, generation: int, loaded: LoadedImage, box: tuple[int, int, int, int],
                 settings: Adjustments, signals: PipelineSignals):
        super().__init__()
        self.generation = generation
        self.loaded = loaded
        self.box = box
        self.settings = settings
        self.signals = signals

    def run(self) -> None:
        patch = self.loaded.full.crop(self.box)
        patch = apply_image_adjustments(patch, self.settings, self.loaded.preview_histogram)
        self.signals.detail_ready.emit(self.generation, to_qimage(patch), self.box[0], self.box[1])
