"""The image list: rows, filters, counts, navigation and favorites.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

import bisect
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QListWidgetItem, QMenu, QMessageBox, QProgressDialog

from .avi import VideoFrameRef
from .dataset import is_verified, model_score, path_key
from .merge import FRAME_STATES


class ImageListMixin:
    """The image list, its filters and counts line, moving between images, and favorites."""

    def _fill_class_filter(self) -> None:
        self.class_filter.clear()
        background_item = QListWidgetItem("_background_")
        background_item.setData(Qt.ItemDataRole.UserRole, self.BACKGROUND_FILTER_ID)
        background_item.setFlags(background_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        background_item.setCheckState(Qt.CheckState.Checked)
        background_item.setToolTip("Images reviewed and marked as background")
        self.class_filter.addItem(background_item)
        for category_id, name in sorted(self.categories.items(), key=lambda pair: pair[1].lower()):
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, category_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.class_filter.addItem(item)
        self._show_matching_classes()

    def _set_all_classes(self, checked: bool) -> None:
        """Check or uncheck the categories shown (all of them when nothing is searched)."""
        self.class_filter.blockSignals(True)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.class_filter.count()):
            item = self.class_filter.item(row)
            if not item.isHidden():
                item.setCheckState(state)
        self.class_filter.blockSignals(False)
        self._apply_frame_filters()

    def _show_matching_classes(self) -> None:
        """Hide categories whose name does not contain the search text; hidden ones keep their check."""
        text = self.class_search.text().strip().casefold()
        for row in range(self.class_filter.count()):
            item = self.class_filter.item(row)
            item.setHidden(bool(text) and text not in item.text().casefold())

    def _selected_category_ids(self) -> set[Any]:
        return {
            self.class_filter.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(self.class_filter.count())
            if self.class_filter.item(row).checkState() == Qt.CheckState.Checked
        }

    def _annotation_matches_filter(
        self, annotation: dict[str, Any], selected: set[Any] | None = None,
        verification: str | None = None,
    ) -> bool:
        if selected is None:
            selected = self._selected_category_ids()
        if annotation.get("category_id") not in selected:
            return False
        if verification is None:
            verification = self.verification_filter.currentData()
        verified = is_verified(annotation.get("verified", True))
        return verification == "all" or (verification == "verified" and verified) or (
            verification == "unverified" and not verified
        )

    def _rebuild_frame_list(self, *, show_progress: bool = False) -> None:
        """Create one list row per image. Only called when the set of images changes."""
        if not hasattr(self, "frame_list"):
            return
        self._commit_current_scene()
        progress = None
        if show_progress and len(self.images) >= 2000:
            progress = QProgressDialog("Building image list…", "", 0, 0, self)
            progress.setWindowTitle("Loading images")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            progress.setCancelButton(None)
            progress.show()
            QApplication.processEvents()
        self._frame_sort_keys = {}
        listed = []
        for position, image in enumerate(self.images):
            if progress and position % 2000 == 0:
                QApplication.processEvents()
            image_id = image["id"]
            # A video frame is listed once it has work; frames only looked at stay out.
            if (image_id in self.video_frame_by_image and image_id not in self._listed_video_frames
                    and image_id not in self.persisted_image_ids and not self._has_work_or_verified(image_id)):
                continue
            if image_id in self.video_frame_by_image:
                self._listed_video_frames.add(image_id)
            self._frame_sort_keys[image_id] = self._frame_sort_key(image)
            listed.append(image)
        ordered_images = sorted(listed, key=lambda image: self._frame_sort_keys[image["id"]])
        self._refreshing = True
        self.frame_list.blockSignals(True)
        self.frame_list.setUpdatesEnabled(False)
        self.frame_list.clear()
        self._frame_items = {}
        self._frame_names = {}
        self._pair_ious_by_image = {}
        iou_on = self._iou_filter_on()
        for position, image in enumerate(ordered_images):
            image_id = image["id"]
            image_name = image["file_name"].replace("\\", "/").rsplit("/", 1)[-1]
            unsaved = image_id in self.touched_image_ids
            item = QListWidgetItem(self._frame_row_text(image))  # final text at once
            if unsaved:
                item.setToolTip("Unsaved changes")
            item.setData(Qt.ItemDataRole.UserRole, image_id)
            self.frame_list.addItem(item)
            self._frame_items[image_id] = item
            self._frame_names[image_id] = image_name.casefold()
            if iou_on:
                self._refresh_pair_ious(image_id)
            if progress and position % 2000 == 0:
                QApplication.processEvents()  # keep the progress dialog alive
        self._row_ids = [image["id"] for image in ordered_images]
        self._row_of = {image_id: row for row, image_id in enumerate(self._row_ids)}
        self.frame_list.setUpdatesEnabled(True)
        self.frame_list.blockSignals(False)
        self._refreshing = False
        if progress:
            progress.close()
        self._apply_frame_filters()

    @staticmethod
    def _natural_key(text: str) -> list[tuple[int, int, str]]:
        """Numbers compare as numbers, so "<video>_10.png" is followed by "_11", not "_100"."""
        return [
            (0, int(part), "") if part.isdigit() else (1, 0, part)
            for part in re.split(r"(\d+)", text.casefold())
        ]

    def _frame_sort_key(self, image: dict[str, Any]) -> list[tuple[int, int, str]]:
        return self._natural_key(str(
            self.source_paths_by_image.get(image["id"], (self.root or Path()) / image["file_name"])
        ))

    def _add_frame_row(self, image_id: Any) -> QListWidgetItem:
        """Insert a row for a video frame that just got work, in sorted place, without moving the list."""
        image = self.image_by_id[image_id]
        image_name = image["file_name"].replace("\\", "/").rsplit("/", 1)[-1]
        key = self._frame_sort_key(image)
        low, high = 0, self.frame_list.count()
        while low < high:
            middle = (low + high) // 2
            if self._frame_sort_keys[self.frame_list.item(middle).data(Qt.ItemDataRole.UserRole)] <= key:
                low = middle + 1
            else:
                high = middle
        item = QListWidgetItem(self._frame_row_text(image))
        item.setData(Qt.ItemDataRole.UserRole, image_id)
        anchor = self.frame_list.itemAt(QPoint(0, 0))
        anchor_top = self.frame_list.visualItemRect(anchor).top() if anchor is not None else 0
        self.frame_list.blockSignals(True)
        self.frame_list.insertItem(low, item)
        self.frame_list.blockSignals(False)
        self._row_ids.insert(low, image_id)
        self._row_of = {row_image_id: row for row, row_image_id in enumerate(self._row_ids)}
        self._frame_items[image_id] = item
        self._update_frame_item_text(image_id)
        self._frame_names[image_id] = image_name.casefold()
        self._frame_sort_keys[image_id] = key
        self._listed_video_frames.add(image_id)
        item.setHidden(not self._image_matches(image_id, self._frame_filter_context()))
        if anchor is not None:
            # A row inserted above the visible area would push everything down; keep the view still.
            shift = self.frame_list.visualItemRect(anchor).top() - anchor_top
            if shift:
                scroll_bar = self.frame_list.verticalScrollBar()
                scroll_bar.setValue(scroll_bar.value() + shift)
        return item

    def _iou_filter_on(self) -> bool:
        return self.iou_filter_operator.currentData() != "off"

    def _iou_filter_changed(self, _index: int) -> None:
        self.iou_filter_threshold.setEnabled(self._iou_filter_on())
        if self._iou_filter_on():  # worked out only when the filter is turned on
            for image_id in self._frame_items:
                if image_id not in self._pair_ious_by_image:
                    self._refresh_pair_ious(image_id)
        self._apply_frame_filters()

    def _score_filter_on(self) -> bool:
        return self.score_filter_operator.currentData() != "off"

    def _score_filter_changed(self, _index: int) -> None:
        self.score_filter_threshold.setEnabled(self._score_filter_on())
        if self._score_filter_on():  # 0–1, widened to the project's scores when a model's go beyond it
            scores = [score for annotation in self._all_annotations()
                      if (score := self._model_score(annotation)) is not None]
            self.score_filter_threshold.setRange(min([0.0, *scores]), max([1.0, *scores]))
        self._apply_frame_filters()

    @staticmethod
    def _model_score(annotation: dict[str, Any]) -> float | None:
        """A box's model score, or None for a manual box."""
        return model_score(annotation)

    def _frame_filter_context(self) -> dict[str, Any]:
        """Read the filter widgets once, so each row check is plain dictionary work."""
        selected_ids = self._selected_category_ids()
        include_background = self.BACKGROUND_FILTER_ID in selected_ids
        selected_categories = selected_ids - {self.BACKGROUND_FILTER_ID}
        verification = self.verification_filter.currentData()
        annotator = self.annotator_filter.currentData()
        annotator_id = self._user_id_by_name.get(annotator)
        # Images where this user made an earlier, saved version of a box.
        earlier_images = (
            self._history_index()[1].get(annotator_id, set())
            if self.annotator_history_action.isChecked() and annotator_id is not None else set()
        )
        return {
            "search": self.frame_search.text().strip().casefold(),
            "frame_verification": self.frame_verification_filter.currentData(),
            "verification": verification,
            "categories": selected_categories,
            "include_background": include_background,
            "no_filter": (
                len(selected_categories) == len(self.categories)
                and include_background and verification == "all"
            ),
            "annotator": annotator,
            "annotator_id": annotator_id,
            "annotator_earlier_images": earlier_images,
            "iou_enabled": self._iou_filter_on(),
            "iou_operator": self.iou_filter_operator.currentData(),
            "iou_threshold": round(self.iou_filter_threshold.value(), 2),
            "score_enabled": self._score_filter_on(),
            "score_operator": self.score_filter_operator.currentData(),
            "score_threshold": round(self.score_filter_threshold.value(), 2),
            "unsaved_only": self.unsaved_filter_check.isChecked(),
            "changed_since": self._changed_since["ids"] if self._changed_since else None,
            "favorites_only": self.favorites_only_action.isChecked(),
        }

    def _image_matches(self, image_id: Any, context: dict[str, Any]) -> bool:
        if self.visible_image_ids is not None and image_id not in self.visible_image_ids:
            return False
        if context["search"] and context["search"] not in self._frame_names.get(image_id, ""):
            return False
        if context["unsaved_only"] and image_id not in self.touched_image_ids:
            return False
        if context["favorites_only"] and not self.image_by_id[image_id].get("favorite"):
            return False
        if context["changed_since"] is not None and image_id not in context["changed_since"] and not (
                self._changed_since["unsaved"] and image_id in self.touched_image_ids):  # unsaved: current version
            return False
        frame_state = context["frame_verification"]
        if frame_state != "all" and self._frame_state(image_id) != frame_state:
            return False
        annotations = self.annotations_by_image.get(image_id, [])
        background = self.background_by_image.get(image_id, False)
        if not (
            context["no_filter"]
            # A background image has no boxes, so it only matches when boxes are not filtered by verification.
            or (context["include_background"] and background and context["verification"] == "all")
            # From the per-image counts (category -> [verified, unverified]), not box by box.
            or any(
                category_id in context["categories"] and (
                    (context["verification"] == "all" and (counts[0] or counts[1]))
                    or (context["verification"] == "verified" and counts[0])
                    or (context["verification"] == "unverified" and counts[1])
                )
                for category_id, counts in self.roi_counts_by_image.get(image_id, {}).items()
            )
        ):
            return False
        if context["iou_enabled"]:
            # Listed when at least one overlapping pair (IoU > 0) satisfies the comparison.
            threshold, operator = context["iou_threshold"], context["iou_operator"]
            if not any(
                (operator == ">=" and value >= threshold)
                or (operator == ">" and value > threshold)
                or (operator == "<" and value < threshold)
                or (operator == "<=" and value <= threshold)
                or (operator == "==" and round(value, 2) == threshold)
                for value in self._pair_ious_by_image.get(image_id, [])
            ):
                return False
        if context["score_enabled"]:
            # Listed when at least one box with a model score satisfies the comparison.
            threshold, operator = context["score_threshold"], context["score_operator"]
            scores = (self._model_score(annotation) for annotation in annotations)
            if not any(
                score is not None and (
                    (operator == ">=" and score >= threshold)
                    or (operator == ">" and score > threshold)
                    or (operator == "<" and score < threshold)
                    or (operator == "<=" and score <= threshold)
                )
                for score in scores
            ):
                return False
        annotator = context["annotator"]
        if annotator == self.BACKGROUND_ANNOTATOR_FILTER_ID:
            return background and not annotations
        if annotator == self.ALL_ANNOTATORS_FILTER_ID:
            return True
        annotator_id = context["annotator_id"]
        return image_id in context["annotator_earlier_images"] or any(
            annotator_id is not None and annotation.get("annotator_id") == annotator_id
            for annotation in annotations
        )

    def _apply_frame_filters(self, *_args, select_first: bool = True) -> None:
        """Show or hide existing rows after a filter change; rows are never recreated."""
        if not hasattr(self, "frame_list") or self._refreshing:
            return
        self._commit_current_scene()
        self._filter_video_list()
        context = self._frame_filter_context()
        shown = set()
        self.frame_list.setUpdatesEnabled(False)
        for image_id, item in self._frame_items.items():
            matches = self._image_matches(image_id, context)
            if item.isHidden() == matches:
                item.setHidden(not matches)
            if matches:
                shown.add(image_id)
        self.frame_list.setUpdatesEnabled(True)
        self._shown_ids = shown
        self._shown_rows = sorted(self._row_of[image_id] for image_id in shown if image_id in self._row_of)
        active_id = self._pending_image_id if self._pending_image_id is not None else self.current_image_id
        # A frame shown from a video stays on screen; filters only change the image list.
        if select_first and active_id not in shown and self._active_video is None:
            first = self._first_shown_item()
            if first is not None:
                self._select_frame_item(first)
            elif active_id is not None:
                self._clear_scene()
        self.clear_filters_button.setEnabled(self._filters_active(context))
        self._update_navigation_buttons()
        self._update_frame_stats()

    def _filters_active(self, context: dict[str, Any]) -> bool:
        """True when any filter in the panel (not the name search) is set."""
        return not (
            context["no_filter"] and context["frame_verification"] == "all" and not context["iou_enabled"]
            and not context["score_enabled"]
            and context["annotator"] == self.ALL_ANNOTATORS_FILTER_ID and not context["unsaved_only"]
            and not self.annotator_history_action.isChecked()
            and context["changed_since"] is None and not context["favorites_only"]
        )

    def _clear_filters(self) -> None:
        """Reset every filter so all images are listed; the name searches are left as typed."""
        widgets = (self.unsaved_filter_check, self.annotator_filter, self.annotator_history_action,
                   self.class_filter, self.frame_verification_filter, self.verification_filter,
                   self.iou_filter_operator, self.score_filter_operator)
        for widget in widgets:
            widget.blockSignals(True)
        self.unsaved_filter_check.setChecked(False)
        self.annotator_filter.setCurrentIndex(0)  # All users
        self.annotator_history_action.setChecked(False)
        self._changed_since = None
        self.changed_since_action.setChecked(False)
        self.changed_since_action.setText("Changed in Version…")
        self.favorites_only_action.blockSignals(True)
        self.favorites_only_action.setChecked(False)
        self.favorites_only_action.blockSignals(False)
        for row in range(self.class_filter.count()):
            self.class_filter.item(row).setCheckState(Qt.CheckState.Checked)
        self.frame_verification_filter.setCurrentIndex(0)
        self.verification_filter.setCurrentIndex(0)
        self.iou_filter_operator.setCurrentIndex(0)  # Off
        self.iou_filter_threshold.setEnabled(False)
        self.score_filter_operator.setCurrentIndex(0)  # Off
        self.score_filter_threshold.setEnabled(False)
        for widget in widgets:
            widget.blockSignals(False)
        self._apply_frame_filters()

    @staticmethod
    def _box_iou(first: list[float], second: list[float]) -> float:
        """IoU of two COCO [x, y, width, height] boxes."""
        x1, y1, w1, h1 = first
        x2, y2, w2, h2 = second
        overlap_w = min(x1 + w1, x2 + w2) - max(x1, x2)
        overlap_h = min(y1 + h1, y2 + h2) - max(y1, y2)
        if overlap_w <= 0 or overlap_h <= 0:
            return 0.0
        intersection = overlap_w * overlap_h
        union = w1 * h1 + w2 * h2 - intersection
        return intersection / union if union > 0 else 0.0

    @classmethod
    def _pair_ious(cls, annotations: list[dict[str, Any]]) -> list[float]:
        """IoU of every overlapping pair of boxes in one image (pairs that do not overlap are left out)."""
        boxes = [annotation["bbox"] for annotation in annotations]
        ious = (cls._box_iou(box, other) for index, box in enumerate(boxes) for other in boxes[index + 1:])
        return [iou for iou in ious if iou > 0]

    def _refresh_pair_ious(self, image_id: Any) -> None:
        """Recompute one image's overlapping-pair IoUs for the overlap filter; only kept while it is on."""
        if not self._iou_filter_on():
            self._pair_ious_by_image.pop(image_id, None)
            return
        self._pair_ious_by_image[image_id] = self._pair_ious(self.annotations_by_image.get(image_id, []))

    def _update_frame_visibility(self, image_id: Any) -> None:
        """Re-check one image after it was edited; the rest of the list is untouched."""
        item = self._frame_items.get(image_id)
        if item is None and not (image_id in self.video_frame_by_image and self._has_work_or_verified(image_id)):
            return
        self._refresh_pair_ious(image_id)
        added = item is None
        if added:
            item = self._add_frame_row(image_id)
        matches = self._image_matches(image_id, self._frame_filter_context())
        if item.isHidden() == matches:
            item.setHidden(not matches)
        if matches:
            self._shown_ids.add(image_id)
        else:
            self._shown_ids.discard(image_id)
        if added:  # rows below moved down by one
            self._shown_rows = sorted(self._row_of[i] for i in self._shown_ids if i in self._row_of)
        else:
            row = self._row_of.get(image_id)
            position = bisect.bisect_left(self._shown_rows, row) if row is not None else 0
            present = position < len(self._shown_rows) and self._shown_rows[position] == row
            if matches and not present and row is not None:
                self._shown_rows.insert(position, row)
            elif not matches and present:
                del self._shown_rows[position]
        self._update_navigation_buttons()

    def _first_shown_item(self) -> QListWidgetItem | None:
        return self.frame_list.item(self._shown_rows[0]) if self._shown_rows else None

    def _select_frame_item(self, item: QListWidgetItem) -> None:
        self.frame_list.blockSignals(True)
        self.frame_list.setCurrentItem(item)
        self.frame_list.blockSignals(False)
        self._load_frame(item.data(Qt.ItemDataRole.UserRole))

    def _shown_neighbor(self, step: int) -> QListWidgetItem | None:
        """Next visible row in ``step`` direction, wrapping, skipping filtered-out rows."""
        row = self.frame_list.currentRow()
        rows = self._shown_rows
        if row < 0 or not rows:
            return None
        if step > 0:
            target = rows[bisect.bisect_right(rows, row) % len(rows)]
        else:
            target = rows[bisect.bisect_left(rows, row) - 1]
        return None if target == row else self.frame_list.item(target)

    def _update_navigation_buttons(self) -> None:
        enough = len(self.videos) > 1 if self._active_video is not None else len(self._shown_ids) > 1
        self.previous_button.setEnabled(enough)
        self.next_button.setEnabled(enough)

    def _frame_stats_part(self, image_id: Any, selected: set[Any], verification: str) -> tuple:
        """One image's share of the counts line: (listed, shown, state, verified boxes, unverified boxes)."""
        shown = image_id in self._shown_ids
        verified = unverified = 0
        if shown:
            for category_id, counts in self.roi_counts_by_image.get(image_id, {}).items():
                if category_id not in selected:
                    continue
                if verification in ("all", "verified"):
                    verified += counts[0]
                if verification in ("all", "unverified"):
                    unverified += counts[1]
        return image_id in self._frame_items, shown, self._frame_state(image_id), verified, unverified

    def _update_frame_stats(self, image_id: Any = None) -> None:
        """Refresh the counts line. With ``image_id`` only that image's share is redone (an edit); without
        it, or when the box filters changed, everything is counted again."""
        selected = self._selected_category_ids()
        verification = self.verification_filter.currentData()
        key = (frozenset(selected), verification)
        totals = self._stats_totals

        def add(part: tuple, sign: int) -> None:
            listed, shown, state, verified, unverified = part
            if listed:
                totals["listed"][state] += sign
            if shown:
                totals["shown"] += sign
                totals["shown_states"][state] += sign
                totals["verified"] += sign * verified
                totals["unverified"] += sign * unverified

        if image_id is None or key != self._stats_key:
            self._stats_key = key
            totals.clear()
            totals.update(listed={state: 0 for state in FRAME_STATES}, shown_states={state: 0 for state in FRAME_STATES},
                          shown=0, verified=0, unverified=0)
            self._stats_parts = {}
            for each in set(self._frame_items) | self._shown_ids:
                part = self._frame_stats_part(each, selected, verification)
                self._stats_parts[each] = part
                add(part, 1)
        else:
            old = self._stats_parts.pop(image_id, None)
            if old is not None:
                add(old, -1)
            if image_id in self._frame_items or image_id in self._shown_ids:
                part = self._frame_stats_part(image_id, selected, verification)
                self._stats_parts[image_id] = part
                add(part, 1)
        shown_count, shown_states, total_states = totals["shown"], totals["shown_states"], totals["listed"]
        verified_filtered, unverified_filtered = totals["verified"], totals["unverified"]
        if self._frame_items and not shown_count:
            self.frame_stats.setText(f"0/{len(self._frame_items):,} images match")
        else:
            self.frame_stats.setText(
                f"{shown_count:,}/{len(self._frame_items):,} images · "
                f"{shown_states['verified']:,} verified · {shown_states['review']:,} review · "
                f"{verified_filtered + unverified_filtered:,} boxes"
            )
        self.frame_stats.setToolTip(
            "Shown with the current filters / in the project\n"
            f"Images: {shown_count:,} / {len(self._frame_items):,}\n"
            f"Verified images: {shown_states['verified']:,} / {total_states['verified']:,}\n"
            f"Review images: {shown_states['review']:,} / {total_states['review']:,}\n"
            f"Unverified images: {shown_states['unverified']:,} / {total_states['unverified']:,}\n"
            f"Verified boxes: {verified_filtered:,} / {self.roi_totals[0]:,}\n"
            f"Unverified boxes: {unverified_filtered:,} / {self.roi_totals[1]:,}"
        )
        self._update_image_info()

    def _display_name(self, image_id: Any) -> str:
        """While a video is selected, name the video and frame so it is clear what the arrows step."""
        if self._active_video is not None:
            return f"{self._active_video.path.name} — frame {self.frame_box.value()}"
        return Path(self.image_by_id[image_id]["file_name"]).name

    def _update_image_info(self) -> None:
        if self.current_image_id not in self.image_by_id:
            self.image_name_label.setText("")
            self.image_info.setText("No image selected")
            return
        image = self.image_by_id[self.current_image_id]
        self.image_name_label.setText(self._display_name(self.current_image_id))
        self.image_name_label.setToolTip(image["file_name"])
        width, height = self.base_image.size if self.base_image is not None else (
            image.get("width", 0), image.get("height", 0)
        )
        verified = sum(is_verified(item.annotation.get("verified", True)) for item in self.current_items)
        unverified = len(self.current_items) - verified
        # Where this image is among those the filters show (– when the filters hide it).
        row = self._row_of.get(self.current_image_id)
        position = bisect.bisect_left(self._shown_rows, row) if row is not None else len(self._shown_rows)
        shown = position < len(self._shown_rows) and self._shown_rows[position] == row
        self.image_info.setText(
            f"{position + 1 if shown else '–'}/{len(self._shown_rows):,}" + (" ★" if image.get("favorite") else "")
            + f" · {width:,}×{height:,} px · {self._image_size_text(image)}"
            + f" · {len(self.current_items)} box{'' if len(self.current_items) == 1 else 'es'}"
            + (f" ({verified} ✓)" if self.current_items else "")
        )
        self.image_info.setToolTip(f"Image {position + 1 if shown else '–'} of {len(self._shown_rows):,} listed · "
                                   f"{verified} verified and {unverified} unverified box(es)")

    def _image_size_text(self, image: dict[str, Any]) -> str:
        """File size of the image's current source; looked up once per file (metadata only, nothing is read)."""
        source = self._image_source(image)
        if isinstance(source, VideoFrameRef):
            return f"{self._format_bytes(source.video.frame_bytes)} raw"
        if source is None:
            return "—"
        key = path_key(source)
        if key not in self._file_sizes:
            try:
                self._file_sizes[key] = source.stat().st_size
            except OSError:
                self._file_sizes[key] = None
        size = self._file_sizes[key]
        return "—" if size is None else self._format_bytes(size)

    @staticmethod
    def _format_bytes(size: int) -> str:
        for unit in ("bytes", "KB", "MB"):
            if size < 1024 or unit == "MB":
                return f"{size:,} {unit}" if unit == "bytes" else f"{size:,.1f} {unit}"
            size /= 1024
        return f"{size:,.1f} MB"

    def _frame_selection_changed(self, current, _previous) -> None:
        if current is not None and not self._refreshing:
            self._leave_video_mode()  # choosing an image puts the arrow keys back on the image list
            self._load_frame(current.data(Qt.ItemDataRole.UserRole))

    def _jump_to_unverified(self, step: int) -> None:
        """Move to the next (or previous) image in the current list that is not verified."""
        row = self.frame_list.currentRow()
        rows = self._shown_rows
        if rows:
            start = bisect.bisect_right(rows, row) if step > 0 else bisect.bisect_left(rows, row) - 1
            for offset in range(len(rows)):
                target = rows[(start + step * offset) % len(rows)]
                if target == row:
                    continue
                if not self.frame_verified_by_image.get(self._row_ids[target], False):
                    self.frame_list.setCurrentItem(self.frame_list.item(target))
                    return
        self.status_label.setText("No other unverified images in the current list.")

    def _navigate_frames(self, step: int) -> None:
        if self._active_video is not None:  # Previous/Next move between videos; the arrow keys step frames
            self._step_video(step)
            return
        item = self._shown_neighbor(step)
        if item is not None:
            self.frame_list.setCurrentItem(item)

    def _frame_row_text(self, image: dict[str, Any]) -> str:
        """An image list row: "*" while it has unsaved changes and "★" for a favorite, before the name, so both
        show however long the name is."""
        name = image["file_name"].replace("\\", "/").rsplit("/", 1)[-1]
        return (("*" if image["id"] in self.touched_image_ids else "")
                + ("★ " if image.get("favorite") else "") + name)

    def _toggle_favorite(self) -> None:
        """T: mark the image shown as a favorite (e.g. a good example), or unmark it. Saved with the image."""
        image_id = self.current_image_id
        if image_id not in self.image_by_id or not self._editing_allowed():
            return
        image = self.image_by_id[image_id]
        if image.get("favorite"):
            image.pop("favorite", None)
        else:
            image["favorite"] = True
        if self._matches_saved(image_id):
            self.touched_image_ids.discard(image_id)  # toggled back as saved
        else:
            self.touched_image_ids.add(image_id)
        self.dirty = True
        self._update_frame_item_text(image_id)
        self._update_frame_visibility(image_id)
        self._update_image_info()
        self.status_label.setText(("Favorite" if image.get("favorite") else "Not a favorite") + " — unsaved changes")

    def _update_frame_item_text(self, image_id: Any) -> None:
        """An image list row is marked * while that image has changes not saved yet."""
        item = self._frame_items.get(image_id)
        if item is not None and image_id in self.image_by_id:
            unsaved = image_id in self.touched_image_ids
            item.setText(self._frame_row_text(self.image_by_id[image_id]))
            item.setToolTip("Unsaved changes" if unsaved else "")

    def _frame_list_menu(self, position) -> None:
        """Right-click on the image list: Advanced ▸ Delete image permanently…, for an image added by mistake."""
        item = self.frame_list.itemAt(position)
        if item is None:
            return
        menu = QMenu(self)
        delete_action = self._add_purge_action(menu, self.current_annotator is not None and self._viewing is None)
        delete_action.setText("Delete Image Permanently…")
        if menu.exec(self.frame_list.viewport().mapToGlobal(position)) is delete_action:
            self._delete_image_permanently(item.data(Qt.ItemDataRole.UserRole))

    def _delete_image_permanently(self, image_id: Any) -> None:
        """Remove an image from the project for good: its record, its boxes with all their versions, its state
        history, and (at the next save) its file in the project folder, which goes to the Recycle Bin.
        Files elsewhere (added folders, videos) are never touched. Asks first; cannot be undone."""
        if image_id not in self.image_by_id or not self._editing_allowed():
            return
        self._commit_current_scene()
        image = self.image_by_id[image_id]
        name = image["file_name"].replace("\\", "/").rsplit("/", 1)[-1]
        project_file = self.root / name if self.root is not None else None
        has_file = project_file is not None and project_file.is_file()
        boxes = len(self.annotations_by_image.get(image_id, []))
        versions = len(self._history_on_image(image_id))
        answer = QMessageBox.warning(
            self, "Delete image permanently",
            f"Delete {name} from the project for good?\n\n"
            f"Its {boxes:,} box(es), {versions:,} earlier box version(s) and its state history leave the project "
            "at the next save"
            + (", and its file in the project folder goes to the Recycle Bin" if has_file else "")
            + ". Files in added folders and videos are not touched.\n\n"
            "This cannot be undone. A copy of this project that still has the image brings it back if merged later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if image_id in (self.current_image_id, self._pending_image_id):
            self._clear_scene()
        if has_file:
            self._trash_on_save.add(project_file)

        # The record and everything kept about it in this session.
        self.image_by_id.pop(image_id, None)
        for index in range(len(self.images) - 1, -1, -1):
            if self.images[index] is image:
                del self.images[index]
                break
        for table in (self.video_frame_by_image, self.annotations_by_image, self.frame_verified_by_image,
                      self.review_by_image, self.background_by_image, self.roi_counts_by_image,
                      self.source_paths_by_image, self._pair_ious_by_image):
            table.pop(image_id, None)
        for ids in (self.persisted_image_ids, self.touched_image_ids, self._folder_settled,
                    self._listed_video_frames):
            ids.discard(image_id)
        for stack in (self._undo_history, self._redo_history):
            stack[:] = [entry for entry in stack if entry[0] != image_id]
        # Its box and state history; the save keeps history only for the images it writes anyway.
        for field in ("annotation_history", "state_history"):
            if self.annotation_data.get(field):
                self.annotation_data[field] = [record for record in self.annotation_data[field]
                                               if record.get("image_id") != image_id]
        self._history_changed()

        self.dirty = True
        self._rebuild_frame_list()
        self._update_image_info()
        self.status_label.setText(
            f"{name} deleted permanently — it leaves the project file at the next save"
            + (" and its file goes to the Recycle Bin" if has_file else "")
        )
