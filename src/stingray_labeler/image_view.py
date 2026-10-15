"""The image view: loading images, display adjustments, rulers and scale.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

import math
import threading
from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QColorDialog, QDoubleSpinBox, QDialog, QDialogButtonBox, QFormLayout, QGraphicsPixmapItem,
    QLabel, QMessageBox,
)
from PIL import Image

from .avi import AviReader
from .graphics import BoxItem, ScaleBarItem
from .image_processing import (
    Adjustments, AutoLevelsWorker, DetailWorker, ImageLoadWorker, LoadedImage, PreviewAdjustmentWorker,
    apply_image_adjustments, auto_levels,
)


class ImageViewMixin:
    """Loading and showing images, brightness / contrast / levels, rulers, scale and selection color."""

    def _update_rulers(self, *_args) -> None:
        resolution = self.pixel_resolution.value()
        self.top_ruler.resolution = resolution
        self.left_ruler.resolution = resolution
        self.top_ruler.update()
        self.left_ruler.update()

    def _set_calibration_mode(self, enabled: bool) -> None:
        if enabled and self.base_image is None:
            self.calibrate_scale_button.setChecked(False)
            QMessageBox.information(self, "Calibrate scale", "Open an image before calibrating its scale.")
            return
        if enabled and self.draw_button.isChecked():
            self.draw_button.setChecked(False)
        self.view.set_calibration_mode(enabled)
        if enabled:
            self.status_label.setText("Draw a line across a known dimension. Press Esc to cancel.")

    def _calibrate_scale_from_line(self, pixel_length: float) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Calibrate scale")
        form = QFormLayout(dialog)
        form.addRow("Measured line", QLabel(f"{pixel_length:.2f} pixels"))
        length_input = QDoubleSpinBox()
        length_input.setRange(0.000001, 100000000.0)
        length_input.setDecimals(6)
        length_input.setValue(1.0)
        form.addRow("Known length", length_input)
        unit_input = QComboBox()
        unit_input.addItems(["µm", "mm", "cm", "m"])
        unit_input.setCurrentText("mm")
        form.addRow("Unit", unit_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.status_label.setText("Scale calibration canceled.")
            return
        known_length = length_input.value()
        unit = unit_input.currentText()
        micrometres_per_unit = {"µm": 1.0, "mm": 1000.0, "cm": 10000.0, "m": 1000000.0}
        resolution = known_length * micrometres_per_unit[unit] / pixel_length
        self.pixel_resolution.setValue(resolution)
        self.status_label.setText(
            f"Scale calibrated: {resolution:.3f} µm/px from {pixel_length:.2f} pixels = "
            f"{known_length:g} {unit}. You can adjust Pixel resolution manually."
        )

    def _set_ruler_visible(self, visible: bool) -> None:
        self.coordinate_readout.setVisible(visible)
        self.top_ruler.setVisible(visible)
        self.left_ruler.setVisible(visible)

    def _brightness_changed(self, value: int) -> None:
        self.brightness_value.setText(f"{value}%")
        self.adjustment_timer.start()

    def _contrast_changed(self, value: int) -> None:
        self.contrast_value.setText(f"{value}%")
        self.adjustment_timer.start()

    def _gamma_changed(self, value: int) -> None:
        self.gamma_value.setText(f"{value / 100:.2f}")
        self.adjustment_timer.start()

    def _levels_changed(self, value: int) -> None:
        if self.sender() is self.black_point and value >= self.white_point.value():
            self.white_point.setValue(min(255, value + 1))
        elif self.sender() is self.white_point and value <= self.black_point.value():
            self.black_point.setValue(max(0, value - 1))
        self.adjustment_timer.start()

    def _adjustment_changed(self, _checked: bool) -> None:
        self.adjustment_timer.start()

    def _auto_levels_requested(self) -> None:
        if self._loaded is None:
            return
        self._auto_level_generation += 1
        generation = self._auto_level_generation
        worker = AutoLevelsWorker(generation, self._loaded.preview, self.pipeline_signals)
        self._auto_level_workers[generation] = worker
        self.auto_levels_button.setEnabled(False)
        self.status_label.setText("Estimating levels for current image…")
        self.adjustment_pool.start(worker)

    def _toggle_auto_levels(self) -> None:
        """L: with the default levels (black 0, white 255), set automatic black/white levels for this image;
        with any other levels, go back to the defaults."""
        if (self.black_point.value(), self.white_point.value()) != (0, 255):
            self._set_levels(0, 255)
            self.status_label.setText("Levels back to the defaults (black 0, white 255)")
            return
        if self._loaded is None:
            return
        black, white = auto_levels(self._loaded.preview.convert("L").histogram())
        self._set_levels(black, white)
        again = f" ({self._key('auto_levels')} again: defaults)" if self._key("auto_levels") else ""
        self.status_label.setText(f"Automatic levels for this image: black {black}, white {white}{again}")

    def _set_levels(self, black: int, white: int) -> None:
        for box in (self.black_point, self.white_point):
            box.blockSignals(True)
        self.black_point.setValue(black)
        self.white_point.setValue(white)
        for box in (self.black_point, self.white_point):
            box.blockSignals(False)
        self.adjustment_timer.start()

    def _auto_levels_finished(self, generation: int, black: int, white: int) -> None:
        self._auto_level_workers.pop(generation, None)
        if generation != self._auto_level_generation:
            return
        self.white_point.setValue(white)
        self.black_point.setValue(black)
        self.auto_levels_button.setEnabled(self._loaded is not None)
        self.status_label.setText(f"Auto levels set for this image: black {black}, white {white}.")

    def _current_adjustments(self) -> Adjustments:
        return Adjustments(
            self.brightness_slider.value() / 100,
            self.contrast_slider.value() / 100,
            self.gamma_slider.value() / 100,
            self.invert_check.isChecked(),
            self.black_point.value(),
            self.white_point.value(),
        )

    def _update_display_image(self) -> None:
        """Re-adjust the cached preview (no resize) and the zoomed-in detail patch."""
        if self._loaded is None or self.background_item is None:
            return
        if self._adjust_cancel is not None:
            self._adjust_cancel.set()
        self._adjust_generation += 1
        generation = self._adjust_generation
        cancelled = threading.Event()
        self._adjust_cancel = cancelled
        worker = PreviewAdjustmentWorker(
            generation, self._loaded, self._current_adjustments(), cancelled, self.pipeline_signals
        )
        self._adjust_workers[generation] = worker
        self.adjustment_pool.start(worker)
        self.detail_timer.start()

    def _apply_display_adjustments(self, image: Image.Image) -> Image.Image:
        return apply_image_adjustments(image, self._current_adjustments())

    def _adjustment_finished(self, generation: int, image: QImage | None) -> None:
        self._adjust_workers.pop(generation, None)
        if (generation != self._adjust_generation or image is None
                or self.background_item is None or self._loaded is None):
            return
        self.background_item.setPixmap(QPixmap.fromImage(image))
        self.background_item.setScale(self._loaded.preview_scale)

    def _update_detail(self) -> None:
        """Show full-resolution pixels for the visible area once the preview is magnified."""
        self._detail_generation += 1
        loaded, detail = self._loaded, self.detail_item
        if loaded is None or detail is None:
            return
        zoom = abs(self.view.transform().m11())
        if loaded.preview_scale <= 1.0 or zoom * loaded.preview_scale <= 1.0:
            detail.hide()
            return
        visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        area = visible.intersected(QRectF(0, 0, loaded.full.width, loaded.full.height))
        if area.isEmpty():
            detail.hide()
            return
        pad_x, pad_y = area.width() * 0.25, area.height() * 0.25
        box = (
            max(0, math.floor(area.left() - pad_x)),
            max(0, math.floor(area.top() - pad_y)),
            min(loaded.full.width, math.ceil(area.right() + pad_x)),
            min(loaded.full.height, math.ceil(area.bottom() + pad_y)),
        )
        generation = self._detail_generation
        worker = DetailWorker(generation, loaded, box, self._current_adjustments(), self.pipeline_signals)
        self._detail_workers[generation] = worker
        self.adjustment_pool.start(worker)

    def _detail_finished(self, generation: int, image: QImage, left: int, top: int) -> None:
        self._detail_workers.pop(generation, None)
        if generation != self._detail_generation or self.detail_item is None:
            return
        self.detail_item.setPixmap(QPixmap.fromImage(image))
        self.detail_item.setPos(left, top)
        self.detail_item.show()

    def _update_scale_bar(self, *_args) -> None:
        if self.scale_bar_item is None:
            return
        self.scale_bar_item.visible = self.show_scale_bar.isChecked()
        self.scale_bar_item.pixel_resolution = self.pixel_resolution.value()
        self.scale_bar_item.length_mm = self.scale_bar_length.value()
        self.scale_bar_item.update()

    def _request_image(self, image_id: Any) -> None:
        """Start decoding on a worker unless it is cached or already loading."""
        key = (self._dataset_generation, image_id)
        if key in self._load_workers:
            return
        path = self._image_path(image_id)
        if path is None:
            return
        video_frame = self._video_source(image_id)
        if video_frame is not None:
            video, index = video_frame
            reader = self._video_reader
            if reader is None or reader.info != video:
                reader = AviReader(video)  # opens the file just for this one read
            worker = ImageLoadWorker(
                key, path, self.pipeline_signals, reader, index,
                still_wanted=lambda: key == (self._dataset_generation, self._pending_image_id),
            )
        else:
            worker = ImageLoadWorker(key, path, self.pipeline_signals)
        self._load_workers[key] = worker
        self.image_pool.start(worker)

    def _image_skipped(self, key: tuple[int, Any]) -> None:
        self._load_workers.pop(key, None)
        # The frame became wanted again after its worker gave up; start it afresh.
        if key == (self._dataset_generation, self._pending_image_id):
            self._request_image(key[1])

    def _image_loaded(self, key: tuple[int, Any], loaded: LoadedImage) -> None:
        self._load_workers.pop(key, None)
        generation, image_id = key
        # Only the image the user selected is shown; anything else that finishes late is dropped.
        if generation == self._dataset_generation and image_id == self._pending_image_id:
            self._show_image(image_id, loaded)

    def _image_failed(self, key: tuple[int, Any], message: str) -> None:
        self._load_workers.pop(key, None)
        generation, image_id = key
        if generation != self._dataset_generation or image_id != self._pending_image_id:
            return
        self._pending_image_id = None
        self.image_info.setText("Image could not be loaded")
        QMessageBox.critical(self, "Cannot load image", message)

    def _load_frame(self, image_id: Any) -> None:
        if image_id in (self.current_image_id, self._pending_image_id):
            return
        # Undo covers the image on screen only: leaving it ends its undo steps (data versions cover the rest).
        self._undo_history.clear()
        self._redo_history.clear()
        previous_id = self._pending_image_id if self._pending_image_id is not None else self.current_image_id
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(False)
        if self.view.calibration_mode:
            self.calibrate_scale_button.setChecked(False)
        self._commit_current_scene()
        self.view.clear_drawing_guides()
        self._cancel_image_adjustment()
        self._detail_generation += 1
        self.current_items = []  # before clear(): removing a selected box fires selectionChanged, which reads it
        self._close_history_dialog()  # its preview outline belongs to the image being cleared
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.scale_bar_item = None
        self.background_item = None
        self.detail_item = None
        self.base_image = None
        self._loaded = None
        self.current_image_id = None
        self.annotation_list.clear()
        self.box_list_header.hide()
        if self._image_path(image_id) is None:
            QMessageBox.critical(
                self, "Invalid image path",
                f"Cannot resolve the image file for {self.image_by_id[image_id]['file_name']!r}.",
            )
            self._pending_image_id = None
            self._sync_frame_controls()
            return
        self._pending_image_id = image_id
        self._sync_frame_controls()
        name = self._display_name(image_id)
        self.image_name_label.setText(name)
        self.image_info.setText(f"Loading {name}…")
        self._request_image(image_id)
        if previous_id is not None and previous_id != image_id:
            self._drop_unused_video_frame(previous_id)

    def _show_image(self, image_id: Any, loaded: LoadedImage) -> None:
        self._pending_image_id = None
        self._loaded = loaded
        self.base_image = loaded.full
        self.auto_levels_button.setEnabled(True)
        image = self.image_by_id[image_id]
        width, height = loaded.full.size
        image["width"] = width
        image["height"] = height
        self.background_item = QGraphicsPixmapItem()
        self.background_item.setZValue(-10)
        self.background_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.background_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene.addItem(self.background_item)
        self.detail_item = QGraphicsPixmapItem()
        self.detail_item.setZValue(-9)
        self.detail_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.detail_item.hide()
        self.scene.addItem(self.detail_item)
        self.scene.setSceneRect(QRectF(0, 0, width, height))
        self.scale_bar_item = ScaleBarItem(width, height)
        self.scene.addItem(self.scale_bar_item)
        self._update_scale_bar()
        if self._current_adjustments().is_identity:
            self.background_item.setPixmap(QPixmap.fromImage(loaded.preview_qimage))
            self.background_item.setScale(loaded.preview_scale)
        else:
            # Leave the view blank for the few ms the adjusted preview takes, rather than flash raw pixels.
            self._update_display_image()
        self.current_image_id = image_id
        self._add_box_items(image_id)
        self._refresh_annotation_list()
        self.view._manual_zoom = False
        self.view.resetTransform()
        self.view.fit_image()
        self._update_rulers()
        self._update_image_info()
        self._sync_frame_controls()
        self._selection_changed()
        self._update_image_info()  # an image switch changes no counts

    def _clear_scene(self) -> None:
        if self.view.calibration_mode:
            self.calibrate_scale_button.setChecked(False)
        self._commit_current_scene()
        previous_id = self._pending_image_id if self._pending_image_id is not None else self.current_image_id
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(False)
        self.view.clear_drawing_guides()
        self._cancel_image_adjustment()
        self._detail_generation += 1
        self.current_items = []  # before clear(): removing a selected box fires selectionChanged, which reads it
        self._close_history_dialog()  # its preview outline belongs to the image being cleared
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.base_image = None
        self._loaded = None
        self.background_item = None
        self.detail_item = None
        self.scale_bar_item = None
        self.current_image_id = None
        self._pending_image_id = None
        self.edit_status.setEnabled(False)
        self.annotation_list.clear()
        self.box_list_header.hide()
        if previous_id is not None:
            self._drop_unused_video_frame(previous_id)
        self._sync_frame_controls()
        self._update_rulers()
        self._update_image_info()

    def _cancel_image_adjustment(self) -> None:
        self._adjust_generation += 1
        if self._adjust_cancel is not None:
            self._adjust_cancel.set()
            self._adjust_cancel = None

    def _choose_selection_color(self) -> None:
        color = QColorDialog.getColor(
            BoxItem.selection_color, self, "Selected box outline color"
        )
        if not color.isValid():
            return
        BoxItem.selection_color = color
        self.view.draw_color = color
        for item in self.scene.items():
            if isinstance(item, BoxItem):
                item.update()
        self.view.viewport().update()

    def _reset_image_view(self) -> None:
        self.brightness_slider.setValue(100)
        self.contrast_slider.setValue(100)
        self.gamma_slider.setValue(100)
        self.black_point.setValue(0)
        self.white_point.setValue(255)
        self.invert_check.setChecked(False)
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(self.base_image is not None)
        self.view._manual_zoom = False
        self.view.resetTransform()
        self.view.fit_image()
        self.view.horizontalScrollBar().setValue(0)
        self.view.verticalScrollBar().setValue(0)
        self._update_rulers()
