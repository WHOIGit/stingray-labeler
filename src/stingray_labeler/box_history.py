"""Box history: earlier versions, deleted boxes, restoring and purging.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGraphicsRectItem, QLabel, QListWidget, QMessageBox, QVBoxLayout,
)

from .dataset import is_verified
from .history import box_note, same_content
from .merge import image_key
from .graphics import BoxItem


class BoxHistoryMixin:
    """Box history and deleted boxes: the saved index they are read from, listing and restoring them."""

    # ---------- box history ----------

    def _history_index(self) -> tuple[dict[Any, list[dict[str, Any]]], dict[Any, set[Any]]]:
        """Box history by image and the images each user has saved versions on; rebuilt only when the
        history list is replaced or changed (see _history_changed), never per edit."""
        history = self.annotation_data.get("annotation_history") or []
        key = (id(history), len(history), self._history_generation)
        if key != self._history_index_key:
            by_image: dict[Any, list[dict[str, Any]]] = {}
            by_user: dict[Any, set[Any]] = {}
            for record in history:
                by_image.setdefault(record.get("image_id"), []).append(record)
                by_user.setdefault(record.get("annotator_id"), set()).add(record.get("image_id"))
            self._history_by_image, self._history_images_by_user = by_image, by_user
            self._history_index_key = key
        return self._history_by_image, self._history_images_by_user

    def _history_changed(self) -> None:
        """Box history was changed in place or replaced outside a save (purge, merge, user or category
        merge, compaction): the index is rebuilt and the next backup carries the history."""
        self._history_generation += 1
        self._history_unsaved = True

    def _history_on_image(self, image_id: Any) -> list[dict[str, Any]]:
        return self._history_index()[0].get(image_id, [])

    def _deleted_boxes(self, image_id: Any) -> list[dict[str, Any]]:
        """The deletion record of each box deleted on this image and not brought back, newest first."""
        present = {item.annotation.get("box_id") for item in self.current_items}
        latest: dict[Any, dict[str, Any]] = {}
        for record in self._history_on_image(image_id):
            box_id = record.get("box_id")
            if box_id not in present and (record.get("rev") or 1) >= (latest.get(box_id, {}).get("rev") or 0):
                latest[box_id] = record
        deleted = [record for record in latest.values() if record.get("deleted")]
        return sorted(deleted, key=lambda record: str(record.get("time") or ""), reverse=True)

    def _deleted_rows(self, image_id: Any) -> list[tuple[dict[str, Any], bool]]:
        """Deleted boxes of this image whose last version is kept: (record, deleted since the last save)."""
        present = {item.annotation.get("box_id") for item in self.current_items}
        self._ensure_saved_cache()
        key = image_key(self.image_by_id[image_id]["file_name"])
        # Deleted since the last save: the row is the box as last saved, whatever older deletion records
        # its history holds (e.g. a deletion a merge kept the box over).
        unsaved = [
            (box, True) for box in self._saved_by_image.get(key, [])
            if box.get("box_id") not in present and (key, box.get("box_id")) not in self._purged_boxes
        ]
        listed = {box.get("box_id") for box, _unsaved in unsaved}
        rows = [(record, False) for record in self._deleted_boxes(image_id) if record.get("box_id") not in listed]
        return unsaved + rows

    def _purge_box(self, box_id: Any) -> None:
        """Remove a box from the project for good: the box itself, all its saved versions and its
        deletion record. Asks first; cannot be undone."""
        image_id = self.current_image_id
        if image_id is None or not self._editing_allowed():
            return
        history = self.annotation_data.get("annotation_history") or []
        versions = sum(1 for record in history if record.get("image_id") == image_id and record.get("box_id") == box_id)
        item = next((item for item in self.current_items if item.annotation.get("box_id") == box_id), None)
        answer = QMessageBox.warning(
            self, "Delete box permanently",
            f"Remove this box and its {versions:,} earlier version(s) from the project for good?\n\n"
            "Unlike Delete, nothing is kept: no grey row, no history, no restore. This cannot be undone. "
            "It leaves the project file at the next save.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._purged_boxes.add((image_key(self.image_by_id[image_id]["file_name"]), box_id))
        self._history_changed()
        self.annotation_data["annotation_history"] = [
            record for record in history if not (record.get("image_id") == image_id and record.get("box_id") == box_id)
        ]
        # Undo must not bring back a box that is gone for good.
        for stack in (self._undo_history, self._redo_history):
            for _snapshot_image, state in stack:
                if _snapshot_image == image_id:
                    state[:] = [annotation for annotation in state if annotation.get("box_id") != box_id]
        self._selected_deleted = None
        if item is not None:
            self.current_items.remove(item)
            self.scene.removeItem(item)
            self._sync_frame_controls()
            self._mark_dirty(counts_changed=True)
            self._selection_changed()
            self._enforce_image_verification(image_id)
        else:
            self._mark_dirty()
            self._refresh_annotation_list(self._selected_item())
        self.status_label.setText("Box deleted permanently — unsaved changes")

    def _version_text(self, record: dict[str, Any], *, unsaved: bool = False) -> str:
        time_text = str(record.get("time") or "")
        parts = [] if unsaved else [
            f"version {record.get('rev') or 1}",
            f"{time_text[:16].replace('T', ' ')} UTC" if time_text else "time unknown",
        ]
        parts += [
            self._annotation_user_name(record),
            self._category_name(record.get("category_id")),
            "verified" if is_verified(record.get("verified", True)) else "unverified",
        ]
        if self._model_score(record) is not None:
            parts.append(f"score {self._model_score(record):.2f}")
        if record.get("deleted"):
            parts.append("deleted")
        if box_note(record):
            parts.append("note: " + box_note(record).splitlines()[0][:40])
        return " · ".join(parts) + ("*" if unsaved else "")

    def _show_box_history(self, box_id: Any) -> None:
        item = next((item for item in self.current_items if item.annotation.get("box_id") == box_id), None)
        if item is None:
            return
        current = dict(item.annotation, bbox=item.scene_bbox())
        earlier = sorted(
            (record for record in self._history_on_image(self.current_image_id) if record.get("box_id") == box_id),
            key=lambda record: (record.get("rev") or 1, str(record.get("time") or "")), reverse=True,
        )
        # Versions form at save: an edit since then is listed above the saved version it will follow.
        saved = self._saved_version(self.current_image_id, item.annotation)
        if saved is None:
            rows = [(self._version_text(current, unsaved=True) + "  (current)", current, False)]
        elif saved is not None and not same_content(saved, current):
            rows = [
                (self._version_text(current, unsaved=True) + "  (current)", current, False),
                (self._version_text(saved) + "  (last saved; kept in history at save)", saved, True),
            ]
        else:
            rows = [(self._version_text(current) + "  (current)", current, False)]
        rows += [(self._version_text(record), record, not record.get("deleted")) for record in earlier]
        self._show_versions(
            f"Box history — {item.category_name}", rows,
            lambda record: self._restore_box_version(box_id, record),
        )

    def _show_deleted_box_history(self, record: dict[str, Any]) -> None:
        """History of a grey (deleted) box; restoring a version brings the box back as that version."""
        box_id = record.get("box_id")
        earlier = sorted(
            (version for version in self._history_on_image(self.current_image_id) if version.get("box_id") == box_id),
            key=lambda version: (version.get("rev") or 1, str(version.get("time") or "")), reverse=True,
        )
        rows = []
        if not record.get("deleted"):
            # Deleted since the last save: the row is the saved box, which joins the history at save.
            rows.append((self._version_text(record) + " · deleted*", record, True))
        rows += [(self._version_text(version), version, True) for version in earlier]
        category_name = self._category_name(record.get("category_id"))

        def restore(version: dict[str, Any]) -> None:
            self._selected_deleted = None
            self._restore_deleted_box(version)

        self._show_versions(f"Box history — {category_name} (deleted)", rows, restore)

    def _show_deleted_boxes(self) -> None:
        rows = [
            (self._version_text(record)
             + (" · deleted*" if unsaved else ""), record, True)
            for record, unsaved in self._deleted_rows(self.current_image_id)
        ]
        self._show_versions("Deleted boxes on this image", rows, self._restore_deleted_box)

    def _show_versions(self, title: str, rows: list[tuple[str, dict[str, Any], bool]], restore) -> None:
        """List versions; selecting one outlines it on the image, and Restore applies it as an edit."""
        self._close_history_dialog()
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        for text, _record, _restorable in rows:
            listing.addItem(text)
        layout.addWidget(listing)
        hint = QLabel("Selected version is outlined in magenta on the image.")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        restore_button = buttons.addButton("Restore this version", QDialogButtonBox.ButtonRole.ActionRole)
        restore_button.setEnabled(False)
        layout.addWidget(buttons)

        def selected(row: int) -> None:
            self._clear_history_preview()
            if 0 <= row < len(rows):
                _text, record, restorable = rows[row]
                self._history_preview = self._outline(record)
                restore_button.setEnabled(restorable and self._editing_allowed())

        def restore_selected() -> None:
            row = listing.currentRow()
            if 0 <= row < len(rows):
                record = rows[row][1]
                self._close_history_dialog()
                restore(record)

        listing.currentRowChanged.connect(selected)
        restore_button.clicked.connect(restore_selected)
        buttons.rejected.connect(self._close_history_dialog)
        dialog.finished.connect(lambda _result: self._clear_history_preview())
        self._history_dialog = dialog
        dialog.resize(560, 300)
        dialog.show()
        listing.setCurrentRow(0)

    def _outline(self, record: dict[str, Any]) -> QGraphicsRectItem:
        """Draw a version's box as a dashed magenta outline on the image (not a box you can edit)."""
        x, y, width, height = (float(value) for value in record["bbox"])
        pen = QPen(QColor(255, 0, 255), 2.0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)  # same width at any zoom
        preview = QGraphicsRectItem(QRectF(x, y, width, height))
        preview.setPen(pen)
        preview.setZValue(20)
        preview.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.scene.addItem(preview)
        return preview

    def _clear_history_preview(self) -> None:
        preview, self._history_preview = self._history_preview, None
        try:
            if preview is not None and preview.scene() is self.scene:
                self.scene.removeItem(preview)
        except RuntimeError:
            pass  # already deleted with the scene

    def _close_history_dialog(self) -> None:
        dialog, self._history_dialog = self._history_dialog, None
        self._clear_history_preview()
        if dialog is not None:
            try:
                dialog.close()
                dialog.deleteLater()
            except RuntimeError:
                pass

    def _restore_box_version(self, box_id: Any, record: dict[str, Any]) -> None:
        """Apply an earlier version to the box as an edit by the current user (undoable)."""
        item = next((item for item in self.current_items if item.annotation.get("box_id") == box_id), None)
        if item is None or not self._editing_allowed() or not self._version_category_exists(record):
            return
        self._push_undo()
        x, y, width, height = (float(value) for value in record["bbox"])
        item.setPos(0, 0)
        item.setRect(QRectF(x, y, width, height))
        item.update_handles()
        item.annotation["category_id"] = record["category_id"]
        item.annotation["verified"] = is_verified(record.get("verified", True))
        if self._model_score(record) is not None:  # a version is restored as it was, model score included
            item.annotation["score"] = record["score"]
        else:
            item.annotation.pop("score", None)
        if box_note(record):
            item.annotation["note"] = box_note(record)
        else:
            item.annotation.pop("note", None)
        item.category_name = self.categories[record["category_id"]]
        item.update()
        self._annotation_changed(item.annotation, counts_changed=True)
        self._selection_changed()
        self._enforce_image_verification(self.current_image_id)
        self.status_label.setText(f"Restored version {record.get('rev') or 1} of the box — unsaved changes")

    def _restore_deleted_box(self, record: dict[str, Any]) -> None:
        """Bring a deleted box back under its own box id, as an edit by the current user (undoable)."""
        if self.current_image_id is None or not self._editing_allowed() or not self._version_category_exists(record):
            return
        self._push_undo()
        x, y, width, height = (float(value) for value in record["bbox"])
        annotation = {
            "id": self.next_annotation_id,
            "image_id": self.current_image_id,
            "category_id": record["category_id"],
            "bbox": [x, y, width, height],
            "area": width * height,
            "iscrowd": 0,
            "verified": is_verified(record.get("verified", True)),
            "annotator_id": self._annotator_id(self.current_annotator),
            "box_id": record.get("box_id"),  # its next rev follows the deletion record at save
        }
        if self._model_score(record) is not None:
            annotation["score"] = record["score"]
        if box_note(record):
            annotation["note"] = box_note(record)
        self.next_annotation_id += 1
        item = BoxItem(annotation, self.categories[annotation["category_id"]], self._annotation_changed,
                       self._push_undo)
        self.scene.addItem(item)
        self.current_items.append(item)
        self.background_by_image[self.current_image_id] = False
        self.scene.clearSelection()
        item.setSelected(True)
        self._sync_frame_controls()
        self._annotation_changed(annotation, counts_changed=True)
        self.status_label.setText("Restored deleted box — unsaved changes")
        self._enforce_image_verification(self.current_image_id)  # the restored box may be unverified

    def _version_category_exists(self, record: dict[str, Any]) -> bool:
        if record.get("category_id") in self.categories:
            return True
        kept = self._merged_into(record.get("category_id"))
        QMessageBox.information(
            self, "Category no longer exists",
            f"This version's category, {(self._retired_category(record.get('category_id')) or {}).get('name')!r}, "
            "was merged into another"
            + (f" ({self.categories[kept]!r})" if kept is not None else "")
            + ". Add the category again to restore this version.",
        )
        return False
