"""Exports and reports: training dataset, favorites, image, crops, activity, summary.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

import csv
import math
import os
import re
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QColorDialog, QDoubleSpinBox, QDialog, QDateEdit, QDialogButtonBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
)
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

from .avi import AviReader, VideoFrameRef
from .dataset import OperationCancelled, is_verified, directory_names, path_key
from .history import activity, as_of, canonical_time, box_note
from .graphics import format_length, scale_bar_lengths_mm
from .image_processing import apply_image_adjustments, to_rgb


class ExportsMixin:
    """Everything under File → Export, the scale bar drawn on exports, and Data → Summary."""

    def _draw_scale_bar(
        self, image: Image.Image, length_mm: float | None = None,
        corner: str = "bottom-right", *, fixed: bool = False, show_label: bool = True,
        color: str | tuple[int, int, int] = "black",
    ) -> bool:
        layout = self._scale_bar_layout(image, length_mm, corner, fixed=fixed, show_label=show_label)
        if layout is None:
            return False
        draw = ImageDraw.Draw(image)
        draw.rectangle(layout["bar"], fill=color)
        if layout["label"]:
            draw.text(layout["label_position"], layout["label"], fill=color, font=layout["font"])
        return True

    def _place_scale_bar(
        self, image: Image.Image, length_mm: float, extend, *,
        fixed: bool = False, show_label: bool = True, color: str | tuple[int, int, int] = "black",
    ) -> Image.Image | None:
        """Draw the bar where it covers the least of the object; None if it does not fit.

        Pixels that differ clearly from the background (estimated from the crop's edges) count as
        object. When every corner would cover some, ``extend(side, rows)`` returns the crop grown by
        ``rows`` source-image rows at the "bottom" or "top" (None where the source image ends), and the
        bar goes in that new band. With no room either way, the least-covered corner is used.
        """
        options = {"fixed": fixed, "show_label": show_label}
        best = self._best_scale_bar_corner(
            image, length_mm, ("bottom-right", "bottom-left", "top-right", "top-left"), **options
        )
        if best is None:
            return None
        if best[0] > 0.005:
            layout = self._scale_bar_layout(image, length_mm, "top-left", **options)
            top = min(layout["label_box"][1], layout["bar"][1]) - layout["margin"]  # from the image top
            rows = math.ceil(layout["bar"][3] + layout["margin"] - top)
            for side in ("bottom", "top"):
                extended = extend(side, rows)
                if extended is None:
                    continue
                corners = (f"{side}-right", f"{side}-left")
                side_best = self._best_scale_bar_corner(extended, length_mm, corners, **options)
                if side_best is not None and side_best[0] <= 0.005:
                    self._draw_scale_bar(extended, length_mm, side_best[1], **options, color=color)
                    return extended
        self._draw_scale_bar(image, length_mm, best[1], **options, color=color)
        return image

    def _best_scale_bar_corner(
        self, image: Image.Image, length_mm: float, corners: tuple[str, ...], **options,
    ) -> tuple[float, str] | None:
        """(share of object pixels under the bar and label, corner) for the clearest corner."""
        foreground = self._foreground_mask(image)
        best = None
        for corner in corners:
            layout = self._scale_bar_layout(image, length_mm, corner, **options)
            if layout is None:
                return None
            gap = layout["margin"] / 2
            bar, label = layout["bar"], layout["label_box"]
            area = (
                max(0, math.floor(min(bar[0], label[0]) - gap)),
                max(0, math.floor(min(bar[1], label[1]) - gap)),
                min(image.width, math.ceil(max(bar[2], label[2]) + gap)),
                min(image.height, math.ceil(max(bar[3], label[3]) + gap)),
            )
            if area[2] <= area[0] or area[3] <= area[1]:
                continue
            covered = ImageStat.Stat(foreground.crop(area)).mean[0] / 255
            if best is None or covered < best[0]:
                best = (covered, corner)
        return best

    @staticmethod
    def _foreground_mask(image: Image.Image) -> Image.Image:
        """255 where a pixel differs clearly from the background level along the image's edges."""
        grey = image.convert("L").filter(ImageFilter.MedianFilter(3))  # drops single-pixel noise
        width, height = grey.size
        edge = max(1, min(width, height) // 50)
        histogram = [0] * 256
        for strip in ((0, 0, width, edge), (0, height - edge, width, height),
                      (0, 0, edge, height), (width - edge, 0, width, height)):
            for value, count in enumerate(grey.crop(strip).histogram()):
                histogram[value] += count
        total = sum(histogram) or 1
        running, background = 0, 0
        for value, count in enumerate(histogram):
            running += count
            if running * 2 >= total:
                background = value
                break
        spread = math.sqrt(sum(count * (value - background) ** 2
                               for value, count in enumerate(histogram)) / total)
        threshold = max(20, 2.5 * spread)
        return grey.point(lambda value: 255 if abs(value - background) > threshold else 0)

    def _scale_bar_layout(
        self, image: Image.Image, length_mm: float | None, corner: str,
        *, fixed: bool = False, show_label: bool = True,
    ) -> dict[str, Any] | None:
        """Where the bar and its label go. A ``fixed`` length is never shortened: None if it does not fit."""
        pixel_resolution = self.pixel_resolution.value()
        if pixel_resolution <= 0:
            return None
        width, height = image.size
        margin = max(16, width / 80)
        display_length_mm = length_mm or self.scale_bar_length.value()
        available_width = width - 2 * margin if fixed else min(width - 2 * margin, width * 0.5)
        bar_width = display_length_mm * 1000 / pixel_resolution
        if bar_width > available_width and fixed:
            return None
        if bar_width > available_width:
            display_length_mm = next((length for length in scale_bar_lengths_mm()
                                      if length <= display_length_mm
                                      and length * 1000 / pixel_resolution <= available_width), 0)
            if not display_length_mm:
                return None
            bar_width = display_length_mm * 1000 / pixel_resolution
        if bar_width <= 0 or available_width <= 0:
            return None
        bar_height = max(5, width / 450)
        label = format_length(display_length_mm) if show_label else ""
        font_size = max(12, min(40, round(width / 50)))
        font = self._label_font(font_size)
        draw = ImageDraw.Draw(image)
        text_box = draw.textbbox((0, 0), label, font=font) if label else (0, 0, 0, 0)
        label_width = text_box[2] - text_box[0]
        label_height = text_box[3] - text_box[1]
        right_corner = corner.endswith("right")
        bottom_corner = corner.startswith("bottom")
        left = width - margin - bar_width if right_corner else margin
        top = height - margin - bar_height if bottom_corner else margin + (label_height + 5 if label else 0)
        label_left = round(left + bar_width - label_width) if right_corner else round(left)
        label_top = round(top - label_height - 5) if bottom_corner else round(margin)
        bar = (left, top, left + bar_width, top + bar_height)
        return {
            "bar": bar,
            "label": label,
            "font": font,
            "label_position": (label_left, label_top),
            "label_box": (label_left + text_box[0], label_top + text_box[1],
                          label_left + text_box[2], label_top + text_box[3]) if label else bar,
            "margin": margin,
        }

    @staticmethod
    def _label_font(size: int):
        """Standard sans-serif font at ``size``: Arial on Windows, DejaVu/Liberation on Linux."""
        for name in ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        try:
            return ImageFont.load_default(size)  # Pillow >= 10.1 renders a scalable default
        except TypeError:
            return ImageFont.load_default()

    def _scale_bar_export_options(self, layout: QFormLayout, length_row: bool = True):
        """Corner and length inputs; with ``length_row`` False the caller places the length input."""
        corner = QComboBox()
        for label, value in (
            ("Bottom right", "bottom-right"),
            ("Bottom left", "bottom-left"),
            ("Top right", "top-right"),
            ("Top left", "top-left"),
        ):
            corner.addItem(label, value)
        length = QDoubleSpinBox()
        length.setRange(0.001, 100000)
        length.setDecimals(3)
        length.setValue(self.scale_bar_length.value())
        length.setSuffix(" mm")
        layout.addRow("Scale bar corner", corner)
        if length_row:
            layout.addRow("Scale bar length", length)
        return corner, length

    def _roi_scale_length(self, roi_width: int) -> float:
        """The longest 1-2-5 length whose bar spans at most a quarter of the crop."""
        resolution = self.pixel_resolution.value()
        if resolution <= 0:
            return 0.000001  # not calibrated: no bar is drawn anyway
        return next((length for length in scale_bar_lengths_mm()
                     if length * 1000 / resolution <= roi_width * 0.25), 0.000001)

    # Fields a plain COCO training export keeps. Boxes only: a segmentation from an opened file would no
    # longer match a box edited here, so it is left out.
    COCO_IMAGE_FIELDS = ("id", "file_name", "width", "height", "license", "flickr_url", "coco_url", "date_captured")

    COCO_ANNOTATION_FIELDS = ("id", "image_id", "category_id", "area", "bbox", "iscrowd")

    def _export_favorites(self) -> None:
        """File → Export → Favorites…: copy the favorite images (whatever their state) to a folder, with a plain
        COCO JSON (favorites.json, indented) of their boxes as they are now: e.g. examples for a report."""
        if self.root is None:
            return
        self._commit_current_scene()
        favorites = [image for image in self.images if image.get("favorite")]
        if not favorites:
            QMessageBox.information(self, "Export Favorites", "No image is marked as a favorite yet"
                                    + (f" ({self._key('favorite')} marks the image shown)." if self._key("favorite")
                                       else "."))
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder for the favorite images",
                                                  str(self.root.parent))
        if not folder:
            return
        destination = Path(folder).resolve()
        save_path = destination / "favorites.json"
        copies, missing = [], []
        for image in favorites:
            source = self._image_source(image)
            if source is None:
                missing.append(Path(image["file_name"]).name)
                continue
            target = destination / Path(image["file_name"]).name
            copies.append((source, target, target.exists()))
        existing = [target.name for _source, target, overwrite in copies if overwrite]
        if save_path.exists():
            existing.append(save_path.name)
        if existing and not self._confirm(
                "Export Favorites", f"{len(existing):,} file(s) already in {destination.name} are replaced, e.g. "
                f"{existing[0]}. Go on?"):
            return
        exported_ids = {image["id"] for image in favorites if Path(image["file_name"]).name not in missing}
        boxes = [{name: annotation[name] for name in self.COCO_ANNOTATION_FIELDS if name in annotation}
                 for image_id in exported_ids for annotation in self.annotations_by_image.get(image_id, [])]
        used = {box["category_id"] for box in boxes}
        data = {
            "images": [{name: image[name] for name in self.COCO_IMAGE_FIELDS if name in image}
                       | {"file_name": Path(image["file_name"]).name}
                       for image in favorites if image["id"] in exported_ids],
            "annotations": boxes,
            "categories": [{"id": category_id, "name": self._category_name(category_id)}
                           for category_id in sorted(used, key=str)],
        }
        try:
            self._run_in_background(
                "Export Favorites", f"Exporting to {destination}…", len(copies) + 1,
                lambda report, cancelled: self._copy_then_write_json(copies, data, save_path, report, cancelled,
                                                                     indented=True))
        except OperationCancelled:
            self.status_label.setText("Export cancelled; files written by it were removed.")
            return
        except OSError as error:
            QMessageBox.critical(self, "Export Favorites", str(error))
            return
        self.status_label.setText(f"Exported {len(exported_ids):,} favorite image(s) to {destination}"
                                  + (f"; {len(missing):,} not found" if missing else ""))

    def _show_summary(self) -> None:
        """Data → Summary…: the project in numbers, for all images or those the filters show: images and boxes
        per image state, per category (by image state) and per user. Counts are of the project as it is now,
        unsaved changes included."""
        if self.root is None:
            return
        self._commit_current_scene()
        dialog = QDialog(self)
        dialog.setWindowTitle("Summary")
        layout = QVBoxLayout(dialog)
        scope = QComboBox()
        scope.addItem("All images", "all")
        scope.addItem("Images shown by the current filters", "shown")
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Count"))
        scope_row.addWidget(scope, 1)
        layout.addLayout(scope_row)
        overview = QLabel()
        overview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(overview)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        state_table, category_table, user_table = QTableWidget(), QTableWidget(), QTableWidget()
        for table in (state_table, category_table, user_table):
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSortingEnabled(True)
        tabs.addTab(state_table, "Image states")
        tabs.addTab(category_table, "Categories")
        tabs.addTab(user_table, "Users")
        text_parts: list[str] = []

        def fill(table: QTableWidget, headers: list[str], rows: list[list[Any]], sortable: bool = True) -> None:
            table.setSortingEnabled(False)
            table.clear()
            table.setColumnCount(len(headers))
            table.setRowCount(len(rows))
            table.setHorizontalHeaderLabels(headers)
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    item = QTableWidgetItem()
                    item.setData(Qt.ItemDataRole.DisplayRole, value)  # numbers sort as numbers
                    table.setItem(row, column, item)
            table.resizeColumnsToContents()
            table.setSortingEnabled(sortable)

        def table_text(headers: list[str], rows: list[list[Any]]) -> list[str]:
            return ["\t".join(headers), *("\t".join(str(value) for value in row) for row in rows), ""]

        state_names = ("verified", "review", "unverified")
        state_headers = ["Image state", "Images", "Background", "With boxes", "Boxes", "Verified boxes",
                         "Unverified boxes", "Images with an unverified box"]
        category_headers = ["Category", "Boxes", "Verified boxes", "Unverified boxes", "Images",
                            "Verified images", "Review images", "Unverified images"]
        user_headers = ["User", "Boxes (last changed by)", "Verified", "Unverified", "Images"]

        def refresh() -> None:
            ids = (list(self._shown_ids) if scope.currentData() == "shown"
                   else [image["id"] for image in self.images])
            unsaved = sum(1 for image_id in ids if image_id in self.touched_image_ids)
            favorites = sum(1 for image_id in ids if self.image_by_id[image_id].get("favorite"))
            # state -> [images, background, with boxes, boxes, verified boxes, unverified boxes,
            #           images with an unverified box]
            per_state = {state: [0] * 7 for state in state_names}
            # category id -> [verified boxes, unverified boxes, images, verified / review / unverified images]
            per_category: dict[Any, list] = {}
            per_user: dict[Any, list] = {}  # id -> [verified, unverified, images]
            scored = noted = 0
            for image_id in ids:
                boxes = self.annotations_by_image.get(image_id, [])
                state = self._frame_state(image_id)
                verified_here = sum(is_verified(box.get("verified", True)) for box in boxes)
                counts = per_state[state]
                counts[0] += 1
                counts[1] += self.background_by_image.get(image_id, False)
                counts[2] += bool(boxes)
                counts[3] += len(boxes)
                counts[4] += verified_here
                counts[5] += len(boxes) - verified_here
                counts[6] += verified_here < len(boxes)
                seen_categories, seen_users = set(), set()
                for box in boxes:
                    verified = is_verified(box.get("verified", True))
                    noted += bool(box_note(box))
                    scored += self._model_score(box) is not None
                    category = per_category.setdefault(box.get("category_id"), [0] * 6)
                    category[0 if verified else 1] += 1
                    if box.get("category_id") not in seen_categories:
                        seen_categories.add(box.get("category_id"))
                        category[2] += 1
                        category[3 + state_names.index(state)] += 1
                    credited = self._annotation_user_name(box)  # a user, "model" or "–"
                    user = per_user.setdefault(credited, [0, 0, 0])
                    user[0 if verified else 1] += 1
                    if credited not in seen_users:
                        seen_users.add(credited)
                        user[2] += 1
            totals = [sum(per_state[state][column] for state in state_names) for column in range(7)]
            state_rows = [[state.capitalize(), *per_state[state]] for state in state_names] + [["All", *totals]]
            versions = self.annotation_data.get("dataset_versions") or []
            current = max(versions, key=lambda tag: canonical_time(tag.get("time")), default=None)
            lines = [
                f"Images: {totals[0]:,} — Verified {per_state['verified'][0]:,} · Review {per_state['review'][0]:,} · "
                f"Unverified {per_state['unverified'][0]:,} · favorites {favorites:,}"
                + (f" · unsaved {unsaved:,}" if unsaved else ""),
                f"Boxes: {totals[3]:,} — verified {totals[4]:,} · unverified {totals[5]:,} · "
                f"with a model score {scored:,} · with a note {noted:,}"
                + (f" · {totals[3] / totals[2]:.1f} per image with boxes" if totals[2] else ""),
                f"Categories in use: {len(per_category):,} of {len(self.categories):,} · Users: {len(per_user):,} · "
                f"Data versions: {len(versions):,}" + (f" (current: {current.get('name')})" if current else "")
                + f" · Saved earlier box versions: {len(self.annotation_data.get('annotation_history') or []):,}",
            ]
            overview.setText("\n".join(lines))
            fill(state_table, state_headers, state_rows, sortable=False)
            # Rarest first, as in the model review: categories listed but not used here come first.
            category_rows = [[name, 0, 0, 0, 0, 0, 0, 0] for category_id, name in self.categories.items()
                             if category_id not in per_category]
            category_rows += sorted(
                ([self._category_name(category_id), counts[0] + counts[1], *counts]
                 for category_id, counts in per_category.items()), key=lambda row: row[1])
            fill(category_table, category_headers, category_rows)
            user_rows = sorted(
                ([name, counts[0] + counts[1], counts[0], counts[1], counts[2]]
                 for name, counts in per_user.items()), key=lambda row: -row[1])
            fill(user_table, user_headers, user_rows)
            text_parts[:] = [*lines, "", *table_text(state_headers, state_rows),
                             *table_text(category_headers, category_rows), *table_text(user_headers, user_rows)]

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_button = buttons.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        copy_button.setToolTip("Copy the summary and the tables (tab-separated, pastes into a spreadsheet)")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(text_parts)))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        scope.currentIndexChanged.connect(lambda _index: refresh())
        refresh()
        dialog.resize(760, 560)
        dialog.exec()

    # ---------- activity ----------

    def _export_activity(self) -> None:
        """Write per-day, per-user activity (from the saved history) between two dates to a CSV file."""
        if self.root is None or not self._save_first("Export Activity"):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Export Activity (CSV)")
        form = QFormLayout(dialog)
        today = QDate.currentDate()
        start, end = QDateEdit(today.addDays(-30)), QDateEdit(today)
        for edit in (start, end):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
        form.addRow("From (UTC day)", start)
        form.addRow("To (UTC day)", end)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        first, last = start.date().toString("yyyy-MM-dd"), end.date().toString("yyyy-MM-dd")
        if first > last:
            first, last = last, first
        path, _ = QFileDialog.getSaveFileName(
            self, "Save activity as", str((self.annotations_path or self.root).parent / f"activity_{first}_{last}.csv"),
            "CSV files (*.csv)")
        if not path:
            return
        data = self._as_of_input()
        rows = self._run_in_background("Export Activity", "Counting…", 0, lambda _r, _c: activity(data, first, last))
        columns = ["date", "user", "boxes_drawn", "boxes_edited", "boxes_verified", "boxes_deleted",
                   "boxes_restored", "images_verified", "images_review", "images_unverified"]
        try:
            with open(path, "w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns, restval=0, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        except OSError as error:
            QMessageBox.critical(self, "Export Activity", f"Could not write {path}:\n\n{error}")
            return
        self.status_label.setText(f"Activity {first} to {last}: {len(rows):,} row(s) written to {path}")

    def export_training_dataset(self) -> None:
        if self.root is None:
            QMessageBox.information(self, "No project", "Open a project before exporting training data.")
            return
        chosen = self._choose_as_of("Export Training Dataset", allow_current=True,
                                    preselect=self._viewing["tag"] if self._viewing else None)
        if chosen is None:
            return
        as_of_label, as_of_tag, _scope = chosen
        if as_of_tag is not None and self._version_end(as_of_tag) is None:
            as_of_tag = None  # the newest version is the current project
        self._commit_current_scene()
        if as_of_tag is None:  # the project as it is now
            entries = [(image, self._frame_state(image["id"]), self.background_by_image.get(image["id"], False),
                        self.annotations_by_image[image["id"]]) for image in self.images]
        else:  # as it was saved at a data version or date
            if not self._save_first("Exporting an earlier version"):
                return
            data = self._as_of_input()
            boxes, states = self._run_in_background(
                "Export Training Dataset", f"Working out the dataset as of {as_of_label}…", 0,
                lambda _report, _cancelled: as_of(data, self._version_end(as_of_tag), as_of_tag.get("exclude")))
            entries = [(image, *states.get(image["id"], ("unverified", False)), boxes.get(image["id"], []))
                       for image in self.images]
        training_images = []
        training_annotations = []
        partly_verified = []
        backgrounds = {}
        for image, state, background, annotations in entries:
            image_id = image["id"]
            backgrounds[image_id] = background
            if state != "verified":
                continue
            verified_annotations = [
                annotation for annotation in annotations
                if is_verified(annotation.get("verified", True))
            ]
            if len(verified_annotations) < len(annotations):
                # Its unverified boxes would be left out, teaching the model those objects are background.
                partly_verified.append(Path(image["file_name"]).name)
                continue
            if verified_annotations:
                training_images.append(image)
                training_annotations.extend(verified_annotations)
            elif background and not annotations:
                training_images.append(image)

        if not training_images:
            QMessageBox.information(
                self, "No verified training images",
                "There are no verified images with only verified boxes, or with an explicit background mark."
                + (f"\n\n{len(partly_verified):,} Verified image(s) still have unverified boxes." if partly_verified
                   else ""),
            )
            return
        if partly_verified:
            answer = QMessageBox.question(
                self, "Images left out",
                f"{len(partly_verified):,} Verified image(s) still have unverified boxes and are left out: "
                "exporting only their verified boxes would teach the model that the other objects are "
                f"background.\n\n{', '.join(partly_verified[:10])}{' …' if len(partly_verified) > 10 else ''}"
                f"\n\nExport the other {len(training_images):,} image(s)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        output_folder = QFileDialog.getExistingDirectory(
            self, "Choose training image folder", str(self.root.parent / "training")
        )
        if not output_folder:
            return
        destination = Path(output_folder).resolve()
        save_path = self._choose_json_save_path(
            "Save training annotations as", destination.parent / f"{destination.name}.json"
        )
        if save_path is None:
            return
        if self.annotations_path is not None and path_key(save_path) == path_key(self.annotations_path):
            QMessageBox.warning(
                self, "Choose another file",
                "The training export cannot replace the open project's JSON file.",
            )
            return

        existing_names = directory_names(destination)
        plan: list[tuple[dict[str, Any], Path, Path, bool]] = []
        planned_names: set[str] = set()
        conflicts = []
        missing = []
        already_there = []
        for image in training_images:
            source = self._image_source(image)
            if source is None:
                missing.append(Path(image["file_name"]))
                continue
            self._fill_image_size(image)
            name = Path(image["file_name"]).name
            target = destination / name
            key = name.casefold()
            same_path = isinstance(source, Path) and path_key(source) == path_key(target)
            if not same_path and key in planned_names:
                conflicts.append((source, target))
                continue
            if not same_path and key in existing_names:
                already_there.append(target)
            if same_path and key not in existing_names:
                missing.append(source)
                continue
            if not same_path:
                planned_names.add(key)
            plan.append((image, source, target, not same_path))

        if conflicts:
            self._show_duplicate_summary(conflicts, 0)
            return
        if already_there:
            # Exporting again into the same folder: its images are replaced from the project.
            answer = QMessageBox.question(
                self, "Images already in the folder",
                f"{len(already_there):,} of the images to export are already in {destination}. "
                "Replace them with the project's images?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        if missing:
            QMessageBox.warning(
                self, "Training images not found",
                "Could not package these image paths:\n\n" + "\n".join(str(path) for path in missing[:20]),
            )
            return

        # Plain COCO only: the app's own fields (states, users, verification, history) stay in the project.
        exported_images = []
        for image, _source, target, _needs_copy in plan:
            record = {name: image[name] for name in self.COCO_IMAGE_FIELDS if name in image}
            try:
                record["file_name"] = Path(os.path.relpath(target, save_path.parent)).as_posix()
            except ValueError:  # Different Windows drives have no relative path.
                record["file_name"] = target.as_posix()
            exported_images.append(record)
        exported_ids = {image["id"] for image in exported_images}
        source_info = self.annotation_data.get("info")
        info = dict(source_info) if isinstance(source_info, dict) else {}
        info["description"] = "Verified training subset exported from Stingray Labeler"
        # Box ids are numbered for this file; an earlier version's boxes may have none of their own.
        exported_annotations = []
        for annotation in training_annotations:
            if annotation["image_id"] not in exported_ids:
                continue
            record = {name: annotation[name] for name in self.COCO_ANNOTATION_FIELDS if name in annotation}
            record["id"] = len(exported_annotations) + 1
            record["area"] = float(annotation["bbox"][2]) * float(annotation["bbox"][3])
            record.setdefault("iscrowd", 0)
            exported_annotations.append(record)
        used_categories = {annotation["category_id"] for annotation in exported_annotations}
        listed = [*self.annotation_data.get("categories", []),
                  *(category for category in self.annotation_data.get("retired_categories") or []
                    if category.get("id") in used_categories)]  # a category an earlier version still used
        if as_of_tag is not None:
            info["description"] += f" (as of {as_of_label})"
        export_data = {
            "info": info,
            "licenses": self.annotation_data.get("licenses", []),
            "images": exported_images,
            "annotations": exported_annotations,
            "categories": [
                {name: category[name] for name in ("id", "name", "supercategory") if name in category}
                for category in listed
            ],
        }
        replace = {path_key(target) for target in already_there}
        copies = [(source, target, path_key(target) in replace)
                  for _image, source, target, needs_copy in plan if needs_copy]
        try:
            self._run_in_background(
                "Exporting training dataset", f"Exporting to {destination}…", len(copies) + 1,
                lambda report, cancelled: self._copy_then_write_json(
                    copies, export_data, save_path, report, cancelled, indented=True
                ),
            )
        except OperationCancelled:
            self.status_label.setText("Training export cancelled; copied images were removed.")
            return
        except OSError as error:
            QMessageBox.critical(
                self, "Training export failed",
                f"{error}\n\nImages copied during this export were removed.",
            )
            return
        self.status_label.setText(
            f"Exported {len(exported_images):,} verified image(s) and "
            f"{len(export_data['annotations']):,} verified annotation boxes to {save_path}"
        )

    def export_frame(self) -> None:
        if self.current_image_id is None or self.base_image is None:
            return
        scale_options = None
        if self.show_scale_bar.isChecked():
            options = QDialog(self)
            options.setWindowTitle("Export Image Scale Bar")
            options_layout = QFormLayout(options)
            scale_options = self._scale_bar_export_options(options_layout)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(options.accept)
            buttons.rejected.connect(options.reject)
            options_layout.addRow(buttons)
            if options.exec() != QDialog.DialogCode.Accepted:
                return
        self._commit_current_scene()
        image_record = self.image_by_id[self.current_image_id]
        default_name = f"{Path(image_record['file_name']).stem}.png"
        output_path, _ = QFileDialog.getSaveFileName(
            self, "Export image", str(self.root / default_name), "PNG image (*.png)"
        )
        if not output_path:
            return
        exported = self._apply_display_adjustments(self.base_image)
        draw = ImageDraw.Draw(exported)
        for item in self.current_items:
            x, y, width, height = item.scene_bbox()
            color = item.color().toTuple()[:3]
            draw.rectangle(
                (x, y, x + width, y + height),
                outline=color,
                width=max(4, exported.width // 700),
            )
        if scale_options is not None:
            scale_corner, scale_length = scale_options
            self._draw_scale_bar(exported, scale_length.value(), scale_corner.currentData())
        try:
            exported.save(output_path, format="PNG")
        except OSError as error:
            QMessageBox.critical(self, "Export failed", str(error))
            return
        self.status_label.setText(f"Exported image: {output_path}")

    def export_rois(self) -> None:
        if self.current_image_id is None or self.base_image is None:
            return
        self._commit_current_scene()
        remembered = self._crop_export_settings  # this session's last choices

        options = QDialog(self)
        options.setWindowTitle("Export Crops")
        options_layout = QFormLayout(options)
        scope = QComboBox()
        scope.addItem("Current image", "current")
        scope.addItem("All images", "all")
        options_layout.addRow("Images", scope)
        verification = QComboBox()
        for label, value in (("All boxes", "all"), ("Verified boxes", "verified"),
                             ("Unverified boxes", "unverified")):
            verification.addItem(label, value)
        verification.setCurrentIndex(max(0, verification.findData(self.verification_filter.currentData())))
        options_layout.addRow("Boxes", verification)
        class_list = QListWidget()
        selected_classes = remembered.get("classes", self._selected_category_ids())
        for category_id, name in sorted(self.categories.items(), key=lambda entry: str(entry[1]).lower()):
            class_item = QListWidgetItem(str(name))
            class_item.setData(Qt.ItemDataRole.UserRole, category_id)
            class_item.setFlags(class_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            class_item.setCheckState(
                Qt.CheckState.Checked if category_id in selected_classes else Qt.CheckState.Unchecked
            )
            class_list.addItem(class_item)
        class_list.setMaximumHeight(160)
        class_buttons = QHBoxLayout()
        for label, state in (("All", Qt.CheckState.Checked), ("None", Qt.CheckState.Unchecked)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, state=state: [
                class_list.item(row).setCheckState(state) for row in range(class_list.count())
            ])
            class_buttons.addWidget(button)
        class_buttons.addStretch(1)
        class_column = QVBoxLayout()
        class_column.addWidget(class_list)
        class_column.addLayout(class_buttons)
        options_layout.addRow("Classes", class_column)
        minimum_size = QSpinBox()
        minimum_size.setRange(0, 100000)
        minimum_size.setSuffix(" px")
        minimum_size.setSpecialValueText("Export all sizes")
        minimum_size.setToolTip("Skip boxes whose longer side is shorter than this.")
        options_layout.addRow("Minimum box size", minimum_size)

        padding_mode = QComboBox()
        padding_mode.addItem("% of box", "percent")
        padding_mode.addItem("px", "pixels")
        padding_input = QSpinBox()
        padding_input.setToolTip(
            "Added on every side. % is of the box's longer side, so every crop is framed alike. "
            "At image edges, padding is reduced to the source pixels available."
        )

        def limit_padding() -> None:
            if padding_mode.currentData() == "percent":
                padding_input.setRange(0, 100)
                padding_input.setSuffix(" % per side")
                return
            if scope.currentData() == "current":
                largest = max(self.base_image.size)
            else:
                largest = max((max(image.get("width") or 0, image.get("height") or 0)
                               for image in self.images), default=0) or max(self.base_image.size)
            padding_input.setRange(0, largest)  # never more than the image size
            padding_input.setSuffix(" px per side")

        padding_row = QHBoxLayout()
        padding_row.addWidget(padding_input, 1)
        padding_row.addWidget(padding_mode)
        options_layout.addRow("Padding", padding_row)
        square = QCheckBox("Square crops")
        square.setToolTip(
            "Widen the shorter side from the source image so each crop is square, with the box centred "
            "where the image allows."
        )
        options_layout.addRow("", square)
        class_folders = QCheckBox("Save each class in its own subfolder")
        options_layout.addRow("Output", class_folders)

        add_scale_bar = QCheckBox("Draw scale bar")
        add_scale_bar.setChecked(self.show_scale_bar.isChecked())
        if self.pixel_resolution.value() <= 0:
            add_scale_bar.setChecked(False)
            add_scale_bar.setEnabled(False)
            add_scale_bar.setToolTip("Calibrate the scale first (View → Scale)")
        options_layout.addRow("Scale bar", add_scale_bar)
        scale_corner, scale_length = self._scale_bar_export_options(options_layout, length_row=False)
        scale_corner.insertItem(0, "Auto (avoid object)", "auto")
        scale_corner.setCurrentIndex(0)
        scale_corner.setToolTip(
            "Auto puts the bar in the corner covering the fewest object pixels (pixels that differ "
            "clearly from the background at the crop's edges); if every corner covers some, the crop "
            "is extended below (or above) into the source image to make room for the bar."
        )
        length_mode = QComboBox()
        length_mode.addItem("Auto (fit each crop)", "auto")
        length_mode.addItem("Fixed", "fixed")
        length_mode.setToolTip(
            "Auto picks the longest 1-2-5 length spanning at most a quarter of each crop. "
            "Fixed keeps the length below on every crop and is never shortened; crops too narrow "
            "for it are exported without a bar."
        )
        length_row = QHBoxLayout()
        length_row.addWidget(length_mode)
        length_row.addWidget(scale_length, 1)
        options_layout.addRow("Scale bar length", length_row)
        show_label = QCheckBox("Show length label")
        show_label.setChecked(True)
        options_layout.addRow("", show_label)
        bar_color = QColor(remembered.get("bar_color", "#000000"))
        color_button = QPushButton()
        color_button.setToolTip("Colour of the scale bar and its label")

        def show_color() -> None:
            color_button.setText(bar_color.name().upper())
            color_button.setStyleSheet(
                f"background-color: {bar_color.name()}; "
                f"color: {'black' if bar_color.lightness() > 127 else 'white'};"
            )

        def pick_color() -> None:
            chosen = QColorDialog.getColor(bar_color, options, "Scale bar colour")
            if chosen.isValid():
                bar_color.setRgb(chosen.rgb())
                show_color()

        show_color()
        color_button.clicked.connect(pick_color)
        options_layout.addRow("Scale bar colour", color_button)

        def update_scale_widgets() -> None:
            enabled = add_scale_bar.isChecked()
            for widget in (scale_corner, length_mode, show_label, color_button):
                widget.setEnabled(enabled)
            scale_length.setEnabled(enabled and length_mode.currentData() == "fixed")

        # Restore this session's last choices; the first export starts from the sidebar filters.
        for combo, key in ((scope, "scope"), (verification, "verification"), (padding_mode, "padding_mode"),
                           (scale_corner, "corner"), (length_mode, "length_mode")):
            if key in remembered:
                combo.setCurrentIndex(max(0, combo.findData(remembered[key])))
        limit_padding()
        padding_input.setValue(remembered.get("padding", 0))
        minimum_size.setValue(remembered.get("minimum_size", 0))
        square.setChecked(remembered.get("square", False))
        class_folders.setChecked(remembered.get("class_folders", False))
        add_scale_bar.setChecked(remembered.get("scale_bar", add_scale_bar.isChecked()))
        show_label.setChecked(remembered.get("show_label", True))
        if "length" in remembered:
            scale_length.setValue(remembered["length"])
        update_scale_widgets()
        scope.currentIndexChanged.connect(limit_padding)
        padding_mode.currentIndexChanged.connect(limit_padding)
        add_scale_bar.toggled.connect(update_scale_widgets)
        length_mode.currentIndexChanged.connect(update_scale_widgets)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(options.accept)
        buttons.rejected.connect(options.reject)
        options_layout.addRow(buttons)
        if options.exec() != QDialog.DialogCode.Accepted:
            return

        chosen_classes = {
            class_list.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(class_list.count())
            if class_list.item(row).checkState() == Qt.CheckState.Checked
        }
        self._crop_export_settings = {
            "scope": scope.currentData(), "verification": verification.currentData(),
            "classes": chosen_classes, "minimum_size": minimum_size.value(),
            "padding_mode": padding_mode.currentData(), "padding": padding_input.value(),
            "square": square.isChecked(), "class_folders": class_folders.isChecked(),
            "scale_bar": add_scale_bar.isChecked(), "corner": scale_corner.currentData(),
            "length_mode": length_mode.currentData(), "length": scale_length.value(),
            "show_label": show_label.isChecked(), "bar_color": bar_color.name(),
        }
        status = verification.currentData()
        smallest = minimum_size.value()
        image_ids = ([self.current_image_id] if scope.currentData() == "current"
                     else [image["id"] for image in self.images])
        jobs, too_small = [], 0
        for image_id in image_ids:
            annotations = []
            for annotation in self.annotations_by_image.get(image_id, []):
                if not self._annotation_matches_filter(annotation, chosen_classes, status):
                    continue
                if max(annotation["bbox"][2], annotation["bbox"][3]) < smallest:
                    too_small += 1
                    continue
                annotations.append(annotation)
            if annotations:
                jobs.append((image_id, annotations))
        if not jobs:
            QMessageBox.information(
                self, "No boxes", "No boxes match the chosen images, status, classes and minimum size."
            )
            return

        output_dir = QFileDialog.getExistingDirectory(self, "Choose box output folder", str(self.root))
        if not output_dir:
            return
        padding_value = padding_input.value()
        padding_percent = padding_mode.currentData() == "percent"
        make_square = square.isChecked()
        per_class = class_folders.isChecked()
        scale_options = None
        if add_scale_bar.isChecked():
            fixed_length = scale_length.value() if length_mode.currentData() == "fixed" else None
            scale_options = (scale_corner.currentData(), fixed_length, show_label.isChecked(),
                             bar_color.getRgb()[:3])
        adjustments = self._current_adjustments()
        current_id, current_image = self.current_image_id, self.base_image
        sources = {image_id: self._image_source(self.image_by_id[image_id]) for image_id, _ in jobs}
        total_boxes = sum(len(annotations) for _, annotations in jobs)

        def work(report, cancelled: threading.Event) -> tuple[int, list[str], bool, int]:
            saved, failed, no_bar = 0, [], 0
            used_names: set[Path] = set()
            made_folders: set[Path] = set()  # each output folder created once, not per crop
            readers: dict[Any, AviReader] = {}
            for index, (image_id, annotations) in enumerate(jobs, start=1):
                if cancelled.is_set():
                    break
                source = sources[image_id]
                report(saved, f"Exporting boxes from image {index:,} of {len(jobs):,}…")
                if image_id == current_id:
                    image = current_image
                else:
                    try:
                        if isinstance(source, VideoFrameRef):
                            reader = readers.get(source.video)
                            if reader is None:
                                reader = readers[source.video] = AviReader(source.video)
                            image = reader.read_frame(source.index).convert("RGB")
                        elif source is not None:
                            with Image.open(source) as opened:
                                image = to_rgb(ImageOps.exif_transpose(opened))  # as shown in the app
                        else:
                            raise OSError("image file not found")
                    except Exception as error:  # noqa: BLE001 - listed for the user afterwards
                        failed.append(f"{source}: {error}")
                        continue
                image_base = Path(self.image_by_id[image_id]["file_name"]).stem
                for annotation in annotations:
                    x, y, width, height = annotation["bbox"]
                    left = max(0, math.floor(x))
                    top = max(0, math.floor(y))
                    right = min(image.width, math.ceil(x + width))
                    bottom = min(image.height, math.ceil(y + height))
                    if right <= left or bottom <= top:
                        continue
                    padding = (round(padding_value / 100 * max(right - left, bottom - top))
                               if padding_percent else padding_value)
                    crop_left = max(0, left - padding)
                    crop_top = max(0, top - padding)
                    crop_right = min(image.width, right + padding)
                    crop_bottom = min(image.height, bottom + padding)
                    if make_square:
                        crop_left, crop_right = self._widen_span(
                            crop_left, crop_right, crop_bottom - crop_top, image.width
                        )
                        crop_top, crop_bottom = self._widen_span(
                            crop_top, crop_bottom, crop_right - crop_left, image.height
                        )
                    roi = image.crop((crop_left, crop_top, crop_right, crop_bottom))
                    # Only the contrast step uses the crop's histogram.
                    histogram = roi.histogram() if not adjustments.is_identity and adjustments.contrast != 1.0 else None
                    roi = apply_image_adjustments(roi, adjustments, histogram)
                    crop_width, crop_height = roi.size  # named before any room is added for the scale bar
                    if scale_options is not None:
                        corner, fixed_length, labelled, color = scale_options
                        length = fixed_length or self._roi_scale_length(roi.width)
                        bar_options = {"fixed": bool(fixed_length), "show_label": labelled, "color": color}
                        if corner == "auto":
                            def extend(side: str, rows: int, image=image, box=(crop_left, crop_top, crop_right,
                                                                               crop_bottom), histogram=histogram):
                                """The crop grown by ``rows`` source rows, adjusted like the crop itself."""
                                grown_top, grown_bottom = box[1], box[3]
                                if side == "bottom":
                                    grown_bottom += rows
                                else:
                                    grown_top -= rows
                                if grown_top < 0 or grown_bottom > image.height:
                                    return None
                                grown = image.crop((box[0], grown_top, box[2], grown_bottom))
                                return apply_image_adjustments(grown, adjustments, histogram)

                            placed = self._place_scale_bar(roi, length, extend, **bar_options)
                        else:
                            placed = roi if self._draw_scale_bar(roi, length, corner, **bar_options) else None
                        if placed is None:
                            no_bar += 1
                        else:
                            roi = placed
                    category = self.categories.get(annotation["category_id"], "unknown")
                    safe_class = re.sub(r"[^a-zA-Z0-9_.-]+", "_", category)
                    folder = Path(output_dir) / safe_class if per_class else Path(output_dir)
                    if folder not in made_folders:
                        folder.mkdir(exist_ok=True)
                        made_folders.add(folder)
                    stem = f"{image_base}_{left}_{top}_{crop_width}_{crop_height}_{safe_class}"
                    target, copy = folder / f"{stem}.png", 1
                    while target in used_names:  # duplicate boxes in this export
                        copy += 1
                        target = folder / f"{stem}_{copy}.png"
                    used_names.add(target)
                    roi.save(target, format="PNG")
                    saved += 1
                    report(saved)
            return saved, failed, cancelled.is_set(), no_bar

        try:
            saved, failed, cancelled, no_bar = self._run_in_background(
                "Exporting crops", "Exporting boxes…", total_boxes, work
            )
        except OSError as error:
            QMessageBox.critical(self, "Box export failed", str(error))
            return
        if failed:
            QMessageBox.warning(
                self, "Some images could not be read",
                f"Boxes from {len(failed):,} image(s) were skipped:\n\n" + "\n".join(failed[:20])
                + ("\n…" if len(failed) > 20 else ""),
            )
        if no_bar:
            QMessageBox.information(
                self, "Scale bar left out",
                f"{no_bar:,} crop(s) were too narrow for a {scale_length.value():g} mm bar and were "
                "exported without one." if length_mode.currentData() == "fixed" else
                f"{no_bar:,} crop(s) were too small for a scale bar and were exported without one.",
            )
        stopped = " before Cancel" if cancelled else ""
        skipped = f"; {too_small:,} box(es) below the minimum size skipped" if too_small else ""
        self.status_label.setText(f"Exported {saved:,} box image(s) to {output_dir}{stopped}{skipped}")

    @staticmethod
    def _widen_span(start: int, end: int, size: int, limit: int) -> tuple[int, int]:
        """Grow [start, end) to ``size`` around its centre, shifted to stay within [0, limit)."""
        if end - start >= size:
            return start, end
        size = min(size, limit)
        start = max(0, min(limit - size, start - (size - (end - start)) // 2))
        return start, start + size
