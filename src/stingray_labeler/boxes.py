"""Boxes on the image: drawing, editing, undo, the box list and its menus.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import QInputDialog, QListWidgetItem, QMenu

from .dataset import is_verified
from .history import new_box_id, box_note, same_content
from .merge import image_key
from .graphics import BoxItem


class BoxesMixin:
    """Drawing and editing boxes, undo / redo, the box list and the box right-click menus."""

    def _snapshot(self) -> tuple[Any, list[dict[str, Any]]]:
        if self.current_image_id is None:
            raise ValueError("No image is selected")
        self._commit_current_scene()
        return self.current_image_id, self._copy_annotations(
            self.annotations_by_image[self.current_image_id]
        )

    def _push_undo(self) -> None:
        if self.current_image_id is None:
            return
        self._undo_history.append(self._snapshot())
        if len(self._undo_history) > 100:
            self._undo_history.pop(0)
        self._redo_history.clear()

    def undo(self) -> None:
        if not self._editing_allowed():
            return
        if not self._undo_history:
            self._undo_to_saved()
            return
        image_id, state = self._undo_history.pop()
        before = self._snapshot_image_annotations(image_id)
        self._redo_history.append(before)
        self._restore_image_annotations(image_id, state)
        self._after_undo(image_id, before[1])
        self._undo_status("Undo", image_id)

    def redo(self) -> None:
        if not self._editing_allowed() or not self._redo_history:
            return
        image_id, state = self._redo_history.pop()
        before = self._snapshot_image_annotations(image_id)
        self._undo_history.append(before)
        self._restore_image_annotations(image_id, state)
        self._after_undo(image_id, before[1])
        self._undo_status("Redo", image_id)

    def _undo_to_saved(self) -> None:
        """Ctrl+Z with no undo steps left (e.g. back on an image edited earlier): put the image's boxes back as
        last saved, as one step that Ctrl+Y redoes. An image new since the save has no saved boxes to go back to."""
        image_id = self.current_image_id
        if image_id not in self.image_by_id or image_id not in self.touched_image_ids:
            return
        self._ensure_saved_cache()
        key = image_key(self.image_by_id[image_id]["file_name"])
        if key not in self._saved_states:
            self.status_label.setText("Nothing to undo: this image is new since the last save")
            return
        saved = []
        for box in self._saved_by_image.get(key, []):
            box = dict(box, image_id=image_id, bbox=list(box["bbox"]))
            if box.get("category_id") not in self.categories:  # merged into another since: as the project is now
                box["category_id"] = self._merged_into(box.get("category_id")) or box.get("category_id")
            saved.append(box)
        current = self._snapshot_image_annotations(image_id)
        if self._box_lists_match(current[1], saved):
            return  # the boxes are as saved; what is unsaved here is not a box edit
        self._redo_history.append(current)
        self._restore_image_annotations(image_id, saved)
        self._after_undo(image_id, current[1])
        self._undo_status("Undo: boxes back as last saved", image_id)

    @staticmethod
    def _box_lists_match(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> bool:
        by_id = {box.get("box_id"): box for box in second}
        return len(first) == len(second) and all(
            box.get("box_id") in by_id and same_content(box, by_id[box.get("box_id")])
            and box_note(box) == box_note(by_id[box.get("box_id")]) for box in first)

    def _undo_status(self, action: str, image_id: Any) -> None:
        self.status_label.setText(action + (" — unsaved changes" if image_id in self.touched_image_ids
                                            else " — back as saved"))

    def _after_undo(self, image_id: Any, before: list[dict[str, Any]]) -> None:
        """Undo and redo are changes by the current user: a box that now differs from its saved version
        is credited to them (not to whoever made the state brought back), boxes it removes are noted as
        deleted by them, and the image is marked unsaved."""
        self._note_deletions(image_id, before, self.annotations_by_image.get(image_id, []))
        for annotation in self.annotations_by_image.get(image_id, []):
            saved = self._saved_version(image_id, annotation)
            if saved is None or not same_content(saved, annotation):
                self._set_annotation_annotator(annotation)
        if self._matches_saved(image_id):
            self.touched_image_ids.discard(image_id)  # undone back to the saved image: nothing new to version
            self._update_frame_item_text(image_id)
        else:
            self._mark_images_unsaved({image_id})
        self.dirty = True
        if image_id == self.current_image_id:
            self._refresh_annotation_list(self._selected_item())

    def _matches_saved(self, image_id: Any) -> bool:
        """True when an image is exactly as last saved: the same boxes (box, category, verification, note),
        state and Background mark. An image new since the save never matches."""
        if image_id not in self.image_by_id:
            return False
        self._ensure_saved_cache()
        key = image_key(self.image_by_id[image_id]["file_name"])
        saved_state = self._saved_states.get(key)
        if saved_state is None or (saved_state["frame_state"], bool(saved_state["background"]),
                                   bool(saved_state.get("favorite"))) != (
                self._frame_state(image_id), bool(self.background_by_image.get(image_id, False)),
                bool(self.image_by_id[image_id].get("favorite"))):
            return False
        saved = {box.get("box_id"): box for box in self._saved_by_image.get(key, [])}
        current = self.annotations_by_image.get(image_id, [])
        return len(saved) == len(current) and all(
            annotation.get("box_id") in saved and same_content(saved[annotation.get("box_id")], annotation)
            and box_note(saved[annotation.get("box_id")]) == box_note(annotation) for annotation in current)

    def _note_deletions(self, image_id: Any, before: Iterable[dict[str, Any]], after: Iterable[dict[str, Any]]) -> None:
        """Remember who removed boxes, so the deletion records written at save credit them."""
        if not self.current_annotator or image_id not in self.image_by_id:
            return
        remaining = {annotation.get("box_id") for annotation in after}
        key = image_key(self.image_by_id[image_id]["file_name"])
        user_id = self._annotator_id(self.current_annotator)
        for annotation in before:
            if annotation.get("box_id") not in remaining:
                self._deleted_by[(key, annotation.get("box_id"))] = user_id

    def _snapshot_image_annotations(self, image_id: Any) -> tuple[Any, list[dict[str, Any]]]:
        if image_id == self.current_image_id:
            self._commit_current_scene()
        return image_id, self._copy_annotations(self.annotations_by_image.get(image_id, []))

    def _restore_image_annotations(self, image_id: Any, annotations: list[dict[str, Any]]) -> None:
        if image_id not in self.image_by_id:
            return
        restored = self._copy_annotations(annotations)
        self.annotations_by_image[image_id] = restored
        self._set_image_roi_counts(image_id, restored)
        if image_id == self.current_image_id:
            selected = self._selected_item()
            selected_id = selected.annotation.get("id") if selected else None
            # Empty the list first: removing a selected box fires selectionChanged, which reads it.
            old_items, self.current_items = self.current_items, []
            for item in old_items:
                self.scene.removeItem(item)
            selected_item = None
            for annotation in restored:
                category_name = self.categories.get(annotation["category_id"], "unknown")
                item = BoxItem(annotation, category_name, self._annotation_changed, self._push_undo)
                self.scene.addItem(item)
                self.current_items.append(item)
                if annotation.get("id") == selected_id:
                    item.setSelected(True)
                    selected_item = item
            self._refresh_annotation_list(selected_item)
            self._selection_changed()
        self._commit_current_scene()
        self._update_frame_visibility(image_id)
        self._update_frame_stats(image_id)
        self._enforce_image_verification(image_id)
        self._update_image_info()

    @staticmethod
    def _copy_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Copy box records for undo history; bbox lists are copied, other values are never edited in place."""
        copies = []
        for annotation in annotations:
            copy = dict(annotation)
            if isinstance(copy.get("bbox"), list):
                copy["bbox"] = list(copy["bbox"])
            copies.append(copy)
        return copies

    def _step_box(self, step: int) -> None:
        """Select the next (or previous) box in box list order, skipping rows hidden by the box search,
        and scroll it into view."""
        rows = [
            item for row, item in enumerate(self.current_items)
            if self.annotation_list.item(row) is None or not self.annotation_list.item(row).isHidden()
        ]
        if not rows:
            return
        current = self._selected_item()
        if current in rows:
            index = (rows.index(current) + step) % len(rows)
        else:
            index = 0 if step > 0 else len(rows) - 1
        item = rows[index]
        self.scene.clearSelection()
        item.setSelected(True)
        self.view.ensureVisible(item.sceneBoundingRect(), 40, 40)

    def _shown_boxes(self, image_id: Any) -> list[dict[str, Any]]:
        """The boxes the image shows: the project's, or (copies of) the data version's being shown."""
        if self._viewing is not None:
            return [dict(record) for record in self._viewing["boxes"].get(image_id, [])]
        return self.annotations_by_image[image_id]

    def _add_box_items(self, image_id: Any) -> None:
        for annotation in self._shown_boxes(image_id):
            category_name = (self.categories.get(annotation["category_id"])
                             or self._category_name(annotation["category_id"]))
            item = BoxItem(annotation, category_name, self._annotation_changed, self._push_undo)
            self.scene.addItem(item)
            self.current_items.append(item)

    def _commit_current_scene(self) -> None:
        if self.current_image_id is None or self._viewing is not None:
            return  # a data version's boxes are only shown
        # Only a box moved or resized here gets new coordinates: those stay on the image while edited. A box
        # that came in reaching past the image edge (or with no size) is kept exactly as it came.
        annotations = []
        for item in self.current_items:
            annotation = item.annotation
            bbox = item.scene_bbox()
            if [round(float(value), 6) for value in annotation["bbox"]] != [round(value, 6) for value in bbox]:
                annotation["bbox"] = bbox
                annotation["area"] = bbox[2] * bbox[3]
            annotations.append(annotation)
        self.annotations_by_image[self.current_image_id] = annotations

    def _refresh_current_roi_counts(self) -> None:
        if self.current_image_id is None:
            return
        self._set_image_roi_counts(
            self.current_image_id,
            [item.annotation for item in self.current_items],
        )

    def _set_image_roi_counts(self, image_id: Any, annotations: list[dict[str, Any]]) -> None:
        for counts in self.roi_counts_by_image.get(image_id, {}).values():
            self.roi_totals[0] -= counts[0]
            self.roi_totals[1] -= counts[1]
        updated: dict[Any, list[int]] = {}
        for annotation in annotations:
            status_index = 0 if is_verified(annotation.get("verified", True)) else 1
            counts = updated.setdefault(annotation.get("category_id"), [0, 0])
            counts[status_index] += 1
            self.roi_totals[status_index] += 1
        self.roi_counts_by_image[image_id] = updated

    def _annotation_changed(
        self, annotation: dict[str, Any], *, counts_changed: bool = False,
    ) -> None:
        if self.current_annotator:
            self._set_annotation_annotator(annotation)
        self._mark_dirty(counts_changed=counts_changed)
        self._refresh_annotation_list(self._selected_item())

    def _set_draw_mode(self, enabled: bool) -> None:
        if enabled and (self.current_annotator is None or self.current_image_id is None
                        or self.background_by_image.get(self.current_image_id, False)):
            self.draw_button.setChecked(False)
            return
        self.view.set_draw_mode(enabled)
        self.draw_button.setText("Cancel drawing" if enabled else "Draw box")

    def _add_rectangle(self, rect: QRectF) -> None:
        if not self._editing_allowed() or self.current_image_id is None:
            return
        self.draw_button.setChecked(False)
        image = self.image_by_id[self.current_image_id]
        width, height = float(image["width"]), float(image["height"])
        left = max(0.0, min(width, rect.left()))
        top = max(0.0, min(height, rect.top()))
        right = max(0.0, min(width, rect.right()))
        bottom = max(0.0, min(height, rect.bottom()))
        if right - left < 2 or bottom - top < 2:
            return
        if self.repeat_category_check.isChecked() and self._last_category_id in self.categories:
            category_id = self._last_category_id
        else:
            category_id = self._ask_category("Choose category", self._last_category_id)
        if category_id is None:
            return
        self._last_category_id = category_id  # offered first the next time a box is drawn
        self._push_undo()
        annotation = {
            "id": self.next_annotation_id,
            "image_id": self.current_image_id,
            "category_id": category_id,
            "bbox": [left, top, right - left, bottom - top],
            "area": (right - left) * (bottom - top),
            "iscrowd": 0,
            "verified": False,
            "annotator_id": self._annotator_id(self.current_annotator),
        }
        annotation["box_id"] = new_box_id()
        self.next_annotation_id += 1
        item = BoxItem(annotation, self.categories[category_id], self._annotation_changed, self._push_undo)
        self.scene.addItem(item)
        self.current_items.append(item)
        self.background_by_image[self.current_image_id] = False
        self.scene.clearSelection()
        item.setSelected(True)
        self._refresh_annotation_list(item)
        self._sync_frame_controls()
        self._annotation_changed(annotation, counts_changed=True)
        self.status_label.setText(f"New box added — {self.image_by_id[self.current_image_id]['file_name']}")
        self._enforce_image_verification(self.current_image_id)  # a new box is unverified

    def _selected_item(self) -> BoxItem | None:
        selected = self.scene.selectedItems()
        return selected[0] if selected and isinstance(selected[0], BoxItem) else None

    def _selection_changed(self) -> None:
        item = self._selected_item()
        self._refresh_annotation_list(item)
        self.edit_status.blockSignals(True)
        enabled = item is not None and self.current_annotator is not None
        self.edit_status.setEnabled(enabled)
        self.change_category_action.setEnabled(enabled and bool(self.categories))
        self.edit_status.setChecked(bool(item) and is_verified(item.annotation.get("verified", True)))
        self.edit_status.blockSignals(False)
        self._sync_frame_controls()

    def _box_list_signature(self) -> tuple:
        """Everything the box list shows depends on; while it is unchanged, only the highlight moves."""
        return (
            self.current_image_id, id(self.current_items), self.annotation_list.count(),
            self.annotation_search.text(), self._history_index_key, id(self._saved_cache_source),
            len(self._purged_boxes), self.current_annotator,
            tuple(self._user_name_by_id.items()), tuple(self.categories.items()),
            len(self.annotation_data.get("retired_categories") or []),
            tuple(
                (id(item), item.category_name, item.color().rgba(), tuple(item.scene_bbox()),
                 item.annotation.get("verified"), item.annotation.get("annotator_id"),
                 item.annotation.get("box_id"), item.annotation.get("category_id"),
                 item.annotation.get("note"), item.annotation.get("score"))
                for item in self.current_items
            ),
        )

    def _sync_annotation_selection(self, selected_item: BoxItem | None) -> None:
        """Move the box list's highlight (and a grey row's outline) without rebuilding the list."""
        self.annotation_list.blockSignals(True)
        self._clear_deleted_preview()
        selected_row = -1
        if selected_item is not None and selected_item in self.current_items:
            selected_row = self.current_items.index(selected_item)
            self._selected_deleted = None
        elif selected_item is None:
            for index, record in enumerate(self._deleted_list_rows):
                if self._selected_deleted == (self.current_image_id, record.get("box_id")):
                    selected_row = len(self.current_items) + index
                    self._deleted_preview = self._outline(record)
                    break
        self.annotation_list.setCurrentRow(selected_row)
        self.annotation_list.blockSignals(False)

    @classmethod
    def _best_ious(cls, boxes: list[list[float]]) -> list[float]:
        """Each box's highest IoU with another box, comparing only boxes whose spans cross (sorted by x)."""
        best = [0.0] * len(boxes)
        order = sorted(range(len(boxes)), key=lambda index: boxes[index][0])
        for position, index in enumerate(order):
            right = boxes[index][0] + boxes[index][2]
            for other in order[position + 1:]:
                if boxes[other][0] >= right:
                    break
                value = cls._box_iou(boxes[index], boxes[other])
                if value > best[index]:
                    best[index] = value
                if value > best[other]:
                    best[other] = value
        return best

    def _swatch(self, color: QColor | None) -> QIcon:
        key = color.rgba() if color is not None else -1
        if key not in self._swatches:
            pixmap = QPixmap(12, 12)
            pixmap.fill(color if color is not None else Qt.GlobalColor.transparent)
            self._swatches[key] = QIcon(pixmap)
        return self._swatches[key]

    def _refresh_annotation_list(self, selected_item: BoxItem | None = None) -> None:
        if not hasattr(self, "annotation_list"):
            return
        self._history_index()
        self._ensure_saved_cache()
        signature = self._box_list_signature()
        if signature == self._box_list_last:
            self._sync_annotation_selection(selected_item)  # nothing shown changed: only the highlight moves
            return
        self.annotation_list.blockSignals(True)
        self.annotation_list.clear()
        selected_row = -1
        boxes = [item.scene_bbox() for item in self.current_items]  # live coordinates while editing
        best_ious = self._best_ious(boxes)
        search = self.annotation_search.text().strip().casefold()
        earlier_versions = Counter(record.get("box_id") for record in self._history_on_image(self.current_image_id))
        key = image_key(self.image_by_id[self.current_image_id]["file_name"]) if self.current_image_id in self.image_by_id else None
        by_box_id, by_number = self._saved_cache

        def short_note(note: str) -> str | None:
            if not note:
                return None
            first = note.splitlines()[0]
            return "“" + first[:30] + ("…" if len(first) > 30 or "\n" in note else "") + "”"

        # Each row's fields, by column; the header (above the list) names only the columns this image uses.
        live = []
        for row, item in enumerate(self.current_items):
            annotation = item.annotation
            versions = earlier_versions[annotation.get("box_id")] + 1  # saved versions; right-click → Box History…
            score = self._model_score(annotation)
            # Changes since the last save, marked like an unsaved deletion.
            saved = by_box_id.get((key, annotation.get("box_id"))) or by_number.get((key, annotation.get("id")))
            unsaved = saved is None or not same_content(saved, dict(annotation, bbox=boxes[row]))
            live.append(({
                "class": item.category_name,
                "verified": "✓" if is_verified(annotation.get("verified", True)) else None,
                "user": self._annotation_user_name(annotation),
                "score": f"{score:.2f}" if score is not None else None,
                "ver": str(versions) if versions > 1 else None,
                "iou": f"{best_ious[row]:.2f}" if best_ious[row] > 0 else None,
                "note": short_note(box_note(annotation)),
            }, unsaved))
        deleted = []
        if self.current_image_id is not None:
            for record, unsaved in self._deleted_rows(self.current_image_id):
                deleted.append(({
                    "class": self._category_name(record.get("category_id")),
                    "verified": "✓" if is_verified(record.get("verified", True)) else None,
                    "user": self.current_annotator if unsaved else self._annotation_user_name(record),
                    "note": short_note(box_note(record)),
                }, unsaved, record))
        every = [fields for fields, _unsaved in live] + [fields for fields, _unsaved, _record in deleted]
        columns = ["class", "verified", "user",
                   *(name for name in ("score", "ver", "iou", "note") if any(fields.get(name) for fields in every))]
        titles = {"class": "Class", "verified": "✓", "user": "User", "score": "Score", "ver": "Ver", "iou": "IoU",
                  "note": "Note"}
        self.box_list_header.setText(" · ".join(titles[name] for name in columns))
        self.box_list_header.setVisible(bool(every))

        def row_text(fields: dict[str, Any]) -> str:
            return " · ".join(fields.get(name) or "–" for name in columns)

        for row, (item, (fields, unsaved)) in enumerate(zip(self.current_items, live)):
            list_item = QListWidgetItem(row_text(fields) + ("*" if unsaved else ""))
            note = box_note(item.annotation)
            tips = ["Unsaved changes"] if unsaved else []
            if note:
                tips.append("Note: " + note)
            if tips:
                list_item.setToolTip("\n".join(tips))
            list_item.setIcon(self._swatch(item.color()))
            self.annotation_list.addItem(list_item)
            # Rows stay in place (row = box), so a hidden row keeps selection by row working.
            list_item.setHidden(bool(search) and search not in item.category_name.casefold())
            if item is selected_item:
                selected_row = row
        # Deleted boxes follow, greyed out and without a colour: their last version, not drawn.
        self._clear_deleted_preview()
        self._deleted_list_rows = []
        grey = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        blank = self._swatch(None)
        for fields, unsaved, record in deleted:
            list_item = QListWidgetItem(f"{row_text(fields)} · deleted{'*' if unsaved else ''}")
            list_item.setIcon(blank)
            list_item.setForeground(grey)
            list_item.setToolTip(
                ("Deleted box (unsaved changes)." if unsaved else "Deleted box.")
                + " Select to outline where it was; right-click to restore it."
            )
            self.annotation_list.addItem(list_item)
            list_item.setHidden(bool(search) and search not in fields["class"].casefold())
            self._deleted_list_rows.append(record)
            if selected_item is None and self._selected_deleted == (self.current_image_id, record.get("box_id")):
                selected_row = self.annotation_list.count() - 1
                self._deleted_preview = self._outline(record)
        if selected_item is not None:
            self._selected_deleted = None  # a box was chosen instead
        self.annotation_list.setCurrentRow(selected_row)
        self.annotation_list.blockSignals(False)
        self._box_list_last = self._box_list_signature()

    def _clear_deleted_preview(self) -> None:
        preview, self._deleted_preview = self._deleted_preview, None
        try:
            if preview is not None and preview.scene() is self.scene:
                self.scene.removeItem(preview)
        except RuntimeError:
            pass  # already deleted with the scene

    def _annotation_row_changed(self, row: int) -> None:
        deleted_row = row - len(self.current_items)
        if 0 <= deleted_row < len(self._deleted_list_rows):
            # A deleted box: no box is selected; the list re-selects this row and outlines it.
            self._selected_deleted = (self.current_image_id, self._deleted_list_rows[deleted_row].get("box_id"))
            if self.scene.selectedItems():
                self.scene.clearSelection()  # refreshes the list through _selection_changed
            else:
                self._refresh_annotation_list()
            if self._deleted_preview is not None:
                self.view.ensureVisible(self._deleted_preview)
            return
        if row < 0 or row >= len(self.current_items):
            return
        item = self.current_items[row]
        self.scene.clearSelection()
        item.setSelected(True)
        self.view.ensureVisible(item)

    def _status_changed(self, checked: bool) -> None:
        if not self._editing_allowed():
            return
        item = self._selected_item()
        if item is None:
            return
        if is_verified(item.annotation.get("verified", True)) == checked:
            return
        self._push_undo()
        item.annotation["verified"] = checked
        if checked and self._frame_state(self.current_image_id) == "verified":
            self._drop_reviewed_scores(push_undo=False)  # this undo step already covers it
        item.update()
        self._refresh_annotation_list(item)
        self._annotation_changed(item.annotation, counts_changed=True)
        self._enforce_image_verification(self.current_image_id)

    def _drop_reviewed_scores(self, push_undo: bool) -> int:
        """Remove the model score from verified boxes of the current, verified image.

        Once both the box and its image are verified, the box is a reviewed annotation rather than
        model output. This is a new version by the user verifying it: the scored version is kept in box history
        at the next save. Undo brings the score back.
        """
        scored = [
            item.annotation for item in self.current_items
            if "score" in item.annotation and is_verified(item.annotation.get("verified", True))
        ]
        if scored and push_undo:
            self._push_undo()
        for annotation in scored:
            # A new version by whoever verified; the scored one stays in history at save.
            del annotation["score"]
            self._set_annotation_annotator(annotation)
        return len(scored)

    def _annotation_double_clicked(self, list_item: QListWidgetItem) -> None:
        """Double-clicking a box in the list changes its category (grey deleted rows do nothing)."""
        row = self.annotation_list.row(list_item)
        if 0 <= row < len(self.current_items):
            if not self.current_items[row].isSelected():
                self.scene.clearSelection()
                self.current_items[row].setSelected(True)
            self._change_category()

    def _annotation_list_menu(self, position) -> None:
        row = self.annotation_list.indexAt(position).row()
        deleted_row = row - len(self.current_items)
        if 0 <= deleted_row < len(self._deleted_list_rows):
            record = self._deleted_list_rows[deleted_row]
            menu = QMenu(self)
            restore_action = menu.addAction("Restore Deleted Box")
            restore_action.setEnabled(self._editing_allowed())
            history_action = menu.addAction("Box History…")
            purge_action = self._add_purge_action(menu, self._editing_allowed())
            chosen = menu.exec(self.annotation_list.viewport().mapToGlobal(position))
            if chosen is restore_action:
                self._selected_deleted = None
                self._restore_deleted_box(record)
            elif chosen is history_action:
                self._show_deleted_box_history(record)
            elif chosen is purge_action:
                self._purge_box(record.get("box_id"))
            return
        if not 0 <= row < len(self.current_items):
            return  # empty space: the deleted boxes are already listed as grey rows
        item = self.current_items[row]
        self.scene.clearSelection()
        item.setSelected(True)
        self._history_menu(item, self.annotation_list.viewport().mapToGlobal(position))

    @staticmethod
    def _add_purge_action(menu: QMenu, enabled: bool) -> QAction:
        """Put "Delete permanently…" in its own submenu at the bottom, away from the everyday actions,
        so it takes a deliberate hover and click rather than a slip of the mouse."""
        menu.addSeparator()
        submenu = menu.addMenu("Advanced")
        submenu.setEnabled(enabled)
        purge_action = submenu.addAction("Delete Permanently…")
        purge_action.setEnabled(enabled)
        return purge_action

    def _view_menu(self, position, global_position) -> None:
        item = self.view.box_at(position)
        if item is not None and not item.isSelected():
            self.scene.clearSelection()
            item.setSelected(True)
        self._history_menu(item, global_position)

    def _history_menu(self, item: BoxItem | None, global_position) -> None:
        if self.current_image_id is None:
            return
        menu = QMenu(self)
        if item is None:
            # Empty space: only the image's deleted boxes, and no menu at all when there are none.
            deleted = self._deleted_rows(self.current_image_id)
            if not deleted:
                return
            deleted_action = menu.addAction(f"Deleted Boxes… ({len(deleted)})")
            if menu.exec(global_position) is deleted_action:
                self._show_deleted_boxes()
            return
        category_action = menu.addAction("Change Category…"
                                         + (f" ({self._key('change_category')})" if self._key("change_category") else ""))
        category_action.setEnabled(self._editing_allowed() and bool(self.categories))
        note_action = menu.addAction("Edit Note…" if box_note(item.annotation) else "Add Note…")
        note_action.setEnabled(self._editing_allowed())
        history_action = menu.addAction("Box History…")
        menu.addSeparator()
        delete_action = menu.addAction(f"Delete ({' / '.join(key for key in (self._key('delete_box'), 'Del') if key)})")
        delete_action.setEnabled(self._editing_allowed())
        purge_action = self._add_purge_action(menu, self._editing_allowed())
        chosen = menu.exec(global_position)
        if chosen is delete_action:
            self._delete_selected()
        elif chosen is purge_action:
            self._purge_box(item.annotation.get("box_id"))
        elif chosen is category_action:
            self._change_category()
        elif chosen is history_action:
            self._show_box_history(item.annotation.get("box_id"))
        elif chosen is note_action:
            self._edit_box_note(item)

    def _edit_box_note(self, item: BoxItem) -> None:
        """Write or change a box's note; a note alone is not a new version."""
        note, accepted = QInputDialog.getMultiLineText(
            self, "Box note", f"Note for this {item.category_name} box (empty removes it):", box_note(item.annotation))
        if not accepted or note.strip() == box_note(item.annotation):
            return
        self._push_undo()
        if note.strip():
            item.annotation["note"] = note.strip()
        else:
            item.annotation.pop("note", None)
        self._annotation_changed(item.annotation)
        self.status_label.setText("Box note " + ("saved" if note.strip() else "removed") + " — unsaved changes")

    def _delete_selected(self) -> None:
        if not self._editing_allowed():
            return
        item = self._selected_item()
        if item is None:
            return
        self._push_undo()
        self._note_deletions(self.current_image_id, [item.annotation], [])
        self.current_items.remove(item)
        self.scene.removeItem(item)
        self._refresh_annotation_list()
        self._sync_frame_controls()
        self._mark_dirty(counts_changed=True)
        self._selection_changed()
        self._enforce_image_verification(self.current_image_id)
