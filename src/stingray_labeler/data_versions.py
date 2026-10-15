"""Data versions: naming, showing, restoring and comparing them.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

import json
import zlib
from collections import Counter
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from .dataset import is_verified
from .history import (
    as_of, canonical_time, changed_images_in, compact_history, compare_versions, later_time, newest_time,
    same_content, utc_now,
)
from .merge import json_copy


class DataVersionsMixin:
    """Data versions, the Changed in Version filter, Compare Data Versions and Compact History."""

    @staticmethod
    def _version_label(tag: dict[str, Any], *, with_note: bool = True) -> str:
        """A data version as listed, like box versions: date, name, who added it, and the first line of its note."""
        time_text = str(tag.get("time") or "")
        parts = [f"{time_text[:16].replace('T', ' ')} UTC" if time_text else "time unknown",
                 str(tag.get("name")), str(tag.get("by") or "–")]
        note = str(tag.get("note") or "").strip().splitlines()
        if with_note and note:
            parts.append(f"{note[0][:60]}{'…' if len(note[0]) > 60 or len(note) > 1 else ''}")
        return " · ".join(parts)

    def _next_time(self) -> str:
        """The time for a save or a new data version: now, but never before the newest time the project
        holds, so times only move forward within a project even on a PC whose clock is behind."""
        if self._newest_time is None:
            self._newest_time = newest_time(self.annotation_data)
        self._newest_time = later_time(self._newest_time, utc_now())
        return self._newest_time

    def _add_import_version(self, annotations_file: Path, annotator: str | None) -> str:
        """Give data opened without a data version a first one, marking it as it was opened. Boxes and
        states in the file keep their saved times (none, for plain COCO: there from the start), so the
        version holds the file as opened and later edits come after it. Returns a status line, or ""."""
        if self.annotation_data.get("dataset_versions"):
            return ""
        tag = {"name": annotations_file.name, "time": self._next_time(),
               "note": f"Created automatically: {annotations_file.name} was opened without a data version."}
        if annotator:
            tag["by"] = annotator
        self.annotation_data["dataset_versions"] = [tag]
        self.dirty = True
        return f"Data version {tag['name']!r} created for the opened data — save to keep it."

    def _edit_dataset_versions(self) -> None:
        """Data → Data Versions…: name points in time, each with a quick note. Adding, renaming, deleting and
        notes are applied only with Save. Clicking a version shows the project as it was then (after asking)."""
        if self.root is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Data Versions")
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        layout.addWidget(listing, 1)
        layout.addWidget(QLabel("Note"))
        note_box = QPlainTextEdit()
        note_box.setMaximumHeight(90)
        note_box.setEnabled(False)
        layout.addWidget(note_box)
        # Working copies, applied with Save: working copy -> the project's version (None for one added here).
        originals: dict[int, dict[str, Any] | None] = {}
        copies: list[dict[str, Any]] = []  # kept alive here, so their ids stay theirs
        for tag in self.annotation_data.get("dataset_versions") or []:
            copy = json_copy(tag)
            copies.append(copy)
            originals[id(copy)] = tag
        # The versions, newest first; the newest (row 0) is the current project.
        shown: list[dict[str, Any] | None] = []
        editable = self.current_annotator is not None  # names and notes are no box edits: allowed while one is shown

        def refresh(select: dict[str, Any] | None = None) -> None:
            listing.blockSignals(True)
            listing.clear()
            shown[:] = sorted(copies, key=lambda tag: str(tag.get("time")), reverse=True)
            viewing = self._viewing["tag"] if self._viewing else None
            for row, tag in enumerate(shown):
                text = self._version_label(tag)
                if row == 0:
                    text += "  (current)"
                if (row == 0 and viewing is None) or (viewing is not None and originals.get(id(tag)) is viewing):
                    text += "  (shown)"
                listing.addItem(text)
            listing.blockSignals(False)
            listing.setCurrentRow(shown.index(select) if select in shown else (0 if shown else -1))
            selected(listing.currentRow())

        def selected(row: int) -> None:
            tag = shown[row] if 0 <= row < len(shown) else None
            note_box.blockSignals(True)
            note_box.setPlainText(str(tag.get("note") or "") if tag else "")
            note_box.setEnabled(tag is not None and editable)
            note_box.blockSignals(False)
            rename_button.setEnabled(tag is not None and editable)
            delete_button.setEnabled(tag is not None and editable)

        def changed() -> None:
            save_button.setEnabled(True)

        def note_edited() -> None:
            row = listing.currentRow()
            if 0 <= row < len(shown) and shown[row] is not None:
                text = note_box.toPlainText().strip()
                if text:
                    shown[row]["note"] = text
                else:
                    shown[row].pop("note", None)
                listing.item(row).setText(self._version_label(shown[row]))
                changed()

        def add() -> None:
            if not self._save_first("A data version"):
                return
            name, accepted = QInputDialog.getText(self, "Add data version", "Name:")
            name = name.strip()
            if not accepted or not name:
                return
            note, accepted = QInputDialog.getMultiLineText(self, "Add data version", "Note (optional):")
            tag = {"name": name, "time": self._next_time(), "by": self.current_annotator}
            if accepted and note.strip():
                tag["note"] = note.strip()
            copies.append(tag)
            originals[id(tag)] = None
            changed()
            refresh(tag)

        def rename() -> None:
            row = listing.currentRow()
            if not 0 <= row < len(shown) or shown[row] is None:
                return
            tag = shown[row]
            name, accepted = QInputDialog.getText(self, "Rename data version", "Name:", text=str(tag.get("name") or ""))
            name = name.strip()
            if not accepted or not name or name == tag.get("name"):
                return
            tag["name"] = name
            listing.item(row).setText(self._version_label(tag))
            changed()

        def delete() -> None:
            row = listing.currentRow()
            if not 0 <= row < len(shown) or shown[row] is None:
                return
            tag = shown[row]
            if self._viewing and originals.get(id(tag)) is self._viewing["tag"]:
                QMessageBox.information(self, "Delete data version", "This version is shown. Show the current version "
                                        "first, then delete it.")
                return
            if not self._confirm("Delete data version", f"Delete the data version {tag.get('name')!r}? "
                                 "Only the name and note go; no box or image changes."):
                return
            copies.remove(tag)
            changed()
            refresh()

        def clicked(item: QListWidgetItem) -> None:
            tag = shown[listing.row(item)]
            if tag is not None and originals.get(id(tag)) is None:
                QMessageBox.information(self, "Data Versions", "Save the new data version first, then show it.")
                return
            if self._show_version(None if listing.row(item) == 0 else originals[id(tag)]):
                refresh(tag)
            restore_button.setEnabled(self._viewing is not None and editable)

        def restore() -> None:
            if save_button.isEnabled() and not self._confirm(
                    "Data Versions", "Restoring closes this list; changes to names and notes here are not saved. "
                    "Go on?"):
                return
            dialog.reject()
            self._restore_shown_version()

        def save() -> None:
            # The project's own records are updated in place, so the version shown stays the same record.
            kept = []
            for copy in copies:
                original = originals.get(id(copy))
                if original is None:
                    kept.append(copy)
                else:
                    original.clear()
                    original.update(copy)
                    kept.append(original)
            self.annotation_data["dataset_versions"] = kept
            self.dirty = True
            self.status_label.setText("Data versions changed — unsaved changes")
            dialog.accept()

        controls = QHBoxLayout()
        add_button = QPushButton("Add…")
        rename_button = QPushButton("Rename…")
        delete_button = QPushButton("Delete…")
        restore_button = QPushButton("Restore This Version…")
        restore_button.setToolTip("Put the project back as the version shown, starting a new data version")
        for button in (add_button, rename_button, delete_button):
            controls.addWidget(button)
        add_button.setEnabled(editable)
        controls.addStretch(1)
        controls.addWidget(restore_button)
        layout.addLayout(controls)
        explanation = QLabel("A data version holds the work from when it was added until the next one is added; "
                             "the newest is the current project. Click an older one to show the boxes as they were "
                             "at its end (read-only; the project is not edited), and the current one to go back. "
                             "File → Export → Training Dataset… can export a version as it was then. Changes to names, notes "
                             "and the list apply when you click Save.")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        save_button.setEnabled(False)
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        add_button.clicked.connect(add)
        rename_button.clicked.connect(rename)
        delete_button.clicked.connect(delete)
        restore_button.clicked.connect(restore)
        restore_button.setEnabled(self._viewing is not None and editable)
        listing.currentRowChanged.connect(selected)
        listing.itemClicked.connect(clicked)
        note_box.textChanged.connect(note_edited)
        refresh()
        dialog.resize(600, 460)
        dialog.exec()

    def _choose_as_of(self, title: str, *, allow_current: bool, with_scope: bool = False,
                      current_label: str = "Current project", preselect: dict[str, Any] | None = None):
        """Ask for a data version (or the current project, if allowed). Returns (label, the data version or
        None for the current project, scope), or None if cancelled or there is no data version to choose."""
        tags = sorted(self.annotation_data.get("dataset_versions") or [],
                      key=lambda tag: str(tag.get("time")), reverse=True)
        if not tags and not allow_current:
            QMessageBox.information(self, title, "There is no data version yet. Add one in Data → Data "
                                                 "Versions… before changing annotation strategy.")
            return None
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        form = QFormLayout(dialog)
        source = QComboBox()
        if allow_current:
            source.addItem(current_label, None)
        for tag in tags:
            source.addItem(self._version_label(tag), tag)
            if tag is preselect:
                source.setCurrentIndex(source.count() - 1)
        form.addRow("As of", source)
        scope = QComboBox()
        if with_scope:
            scope.addItem("All images", "all")
            scope.addItem("Images shown by the current filters", "shown")
            form.addRow("Images", scope)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return source.currentText(), source.currentData(), (scope.currentData() if with_scope else "all")

    def _as_of_input(self) -> dict[str, Any]:
        """The saved project as the session holds it, for working out how it was at an earlier time."""
        self._commit_current_scene()
        return {
            "images": self.images, "annotations": self._all_annotations(),
            "annotation_history": self.annotation_data.get("annotation_history") or [],
            "state_history": self.annotation_data.get("state_history") or [],
        }

    def _saved_project(self) -> dict[str, Any]:
        """The project as last saved (read from the copy kept in memory, once per save), or the open project
        if it was never saved."""
        if self._base_json is None:
            return self._as_of_input()
        if self._saved_project_source is not self._base_json:
            self._saved_project_cache = json.loads(zlib.decompress(self._base_json))
            self._saved_project_source = self._base_json
        return self._saved_project_cache

    def _restore_shown_version(self) -> None:
        """Put the project back as the shown data version was, as edits by the current user, starting a new
        data version (e.g. "Restored v1"). Later versions stay as they are; nothing is erased."""
        if self._viewing is None or self.current_annotator is None:
            return
        tag = self._viewing["tag"]
        if self.dirty and not self._save_first("Restoring a data version"):
            return
        name, accepted = QInputDialog.getText(
            self, "Restore This Version",
            f"Put every image's boxes and state back as they were at the end of {tag.get('name')!r}, as your edits, "
            "starting a new data version. Later versions stay as they are.\n\nNew data version:",
            text=f"Restored {tag.get('name')}")
        name = name.strip()
        if not accepted or not name:
            return
        boxes, states = self._viewing["boxes"], self._viewing["states"]
        self._viewing = None
        self._set_annotation_editing_enabled(self._editing_allowed())
        user_id = self._annotator_id(self.current_annotator)
        changed = 0
        for image in self.images:
            image_id = image["id"]
            before = self.annotations_by_image.get(image_id, [])
            current = {annotation.get("box_id"): annotation for annotation in before}
            target = {record.get("box_id"): record for record in boxes.get(image_id, [])}
            state = states.get(image_id, ("unverified", False))
            state_back = state != (self._frame_state(image_id), self.background_by_image.get(image_id, False))
            if all(box_id in target and same_content(annotation, target[box_id])
                   for box_id, annotation in current.items()) and target.keys() <= current.keys() and not state_back:
                continue
            after = []
            for box_id, record in target.items():
                category_id = (record.get("category_id") if record.get("category_id") in self.categories
                               else self._create_class(str((self._retired_category(record.get("category_id"))
                                                            or {"name": f"category {record.get('category_id')}"})["name"])))
                annotation = dict(current.get(box_id) or {"id": self.next_annotation_id, "image_id": image_id,
                                                          "iscrowd": 0, "box_id": box_id})
                if box_id not in current:
                    self.next_annotation_id += 1
                annotation.update(category_id=category_id, bbox=[float(value) for value in record["bbox"]],
                                  verified=is_verified(record.get("verified", True)))
                if self._model_score(record) is not None:
                    annotation["score"] = record["score"]
                else:
                    annotation.pop("score", None)
                annotation["area"] = annotation["bbox"][2] * annotation["bbox"][3]
                if box_id not in current or not same_content(current[box_id], annotation):
                    annotation["annotator_id"] = user_id
                after.append(annotation)
            self._note_deletions(image_id, before, after)
            if state_back:
                self.frame_verified_by_image[image_id] = state[0] == "verified"
                self.review_by_image[image_id] = state[0] == "review"
                self.background_by_image[image_id] = state[1]
                self._note_state_change(image_id)
            self.annotations_by_image[image_id] = after
            self._set_image_roi_counts(image_id, after)
            self._pair_ious_by_image.pop(image_id, None)
            self.touched_image_ids.add(image_id)
            self._update_frame_item_text(image_id)
            changed += 1
        # A version from before the Verified rule can hold a Verified image with an unverified box.
        self._settle_verified_states(list(self.image_by_id), credit_nobody=False)
        self._undo_history.clear()  # a project-wide change: undo steps from before it no longer apply
        self._redo_history.clear()
        self.annotation_data.setdefault("dataset_versions", []).append(
            {"name": name, "time": self._next_time(), "by": self.current_annotator,
             "note": f"Restored from {tag.get('name')}."})
        self.dirty = True
        if self.current_image_id is not None:
            self.scene.clearSelection()
            old_items, self.current_items = self.current_items, []
            for item in old_items:
                self.scene.removeItem(item)
            self._add_box_items(self.current_image_id)
            self._refresh_annotation_list()
        self._sync_frame_controls()
        self._apply_frame_filters(select_first=False)
        self._update_window_title()
        self.status_label.setText(f"Restored {tag.get('name')!r} on {changed:,} image(s) as data version {name!r} "
                                  "— unsaved changes")

    def _version_end(self, tag: dict[str, Any]) -> str | None:
        """When a data version ends: when the next one starts. None for the newest, which is the current
        project (every edit since it started is part of it)."""
        start = canonical_time(tag.get("time"))
        later = [canonical_time(other.get("time")) for other in self.annotation_data.get("dataset_versions") or []
                 if other is not tag and canonical_time(other.get("time")) > start]
        return min(later, default=None)

    def _revert_to(self) -> None:
        """Data → Show Data Version…: show the project as it was at the end of a data version; the newest
        version is the current project."""
        if self.root is None:
            return
        chosen = self._choose_as_of("Show a Data Version", allow_current=False,
                                    preselect=self._viewing["tag"] if self._viewing else None)
        if chosen is not None:
            tag = chosen[1]
            self._show_version(None if self._version_end(tag) is None else tag)

    def _show_version(self, tag: dict[str, Any] | None) -> bool:
        """Show every image's boxes as they were at the end of data version ``tag`` (when the next one started),
        as last saved; None shows the current project (the newest version) again. Only the display (and the
        default for Export Training Dataset) changes: the project's boxes, unsaved edits included, stay as
        they are, and editing is off until the current version is shown again (or the version is restored).
        Returns True when switched."""
        current = self._viewing["tag"] if self._viewing else None
        if tag is current:
            return False
        if tag is None:
            self._viewing = None
        else:
            self._commit_current_scene()  # unsaved edits stay in the project while a version is shown
            data = self._saved_project()  # worked out from the last save, so unsaved edits play no part
            boxes, states = self._run_in_background(
                "Data Versions", f"Working out {tag.get('name')!r}…", 0,
                lambda _report, _cancelled: as_of(data, self._version_end(tag), tag.get("exclude")))
            self._viewing = {"tag": tag, "boxes": boxes, "states": states}
        self._set_annotation_editing_enabled(self._editing_allowed())
        self.restore_version_action.setEnabled(self._viewing is not None)
        if self.current_image_id is not None:
            self.scene.clearSelection()
            old_items, self.current_items = self.current_items, []
            for item in old_items:
                self.scene.removeItem(item)
            self._add_box_items(self.current_image_id)
            self._refresh_annotation_list()
        self._update_window_title()
        self.status_label.setText(f"Showing data version {tag.get('name')!r} (read-only)" if tag
                                  else "Showing the current version")
        return True

    # ---------- changed in a version ----------

    def _toggle_changed_since(self, checked: bool) -> None:
        """Turn the "changed in version" filter on (asking which version, and by whom) or off."""
        if not checked:
            self._changed_since = None
            self.changed_since_action.setText("Changed in Version…")
            self._apply_frame_filters()
            return
        chosen = self._choose_as_of("Changed in Version", allow_current=False) if self._save_first(
            "This filter") else None
        if chosen is None:
            self.changed_since_action.setChecked(False)
            return
        label, tag, _scope = chosen
        names = sorted(self._user_id_by_name, key=str.casefold)
        user, accepted = QInputDialog.getItem(self, "Changed in Version", "Changed by:", ["Anyone", *names], 0, False)
        if not accepted:
            self.changed_since_action.setChecked(False)
            return
        user_id = None if user == "Anyone" else self._user_id_by_name.get(user)
        data = self._as_of_input()
        # Everything saved from the version's creation until the next one's; the current one has no end yet,
        # and its unsaved changes count too.
        end = self._version_end(tag)
        start = canonical_time(tag.get("time"))
        previous = max((other for other in self.annotation_data.get("dataset_versions") or []
                        if canonical_time(other.get("time")) < start),
                       key=lambda other: canonical_time(other.get("time")), default=None)
        ids = changed_images_in(data, tag.get("time"), end, user_id, tag.get("exclude"),
                                previous.get("exclude") if previous else None)
        name = f"{tag.get('name')}{'' if user_id is None else f' by {user}'}"
        self._set_changed_since(name, ids, unsaved=end is None, text=f"changed in {name}")

    def _set_changed_since(self, label: str, ids: set[Any], *, unsaved: bool, text: str) -> None:
        self._changed_since = {"label": label, "ids": ids, "unsaved": unsaved}
        self.changed_since_action.setChecked(True)
        self.changed_since_action.setText(f"Changed in: {label[:60]}")
        self._apply_frame_filters()
        self.status_label.setText(f"{len(ids):,} image(s) {text}")

    # ---------- compare two versions ----------

    def _compare_versions(self) -> None:
        """Report what changed between two data versions (or a version and the current project)."""
        tags = sorted(self.annotation_data.get("dataset_versions") or [], key=lambda tag: str(tag.get("time")))
        if not tags:
            QMessageBox.information(self, "Compare Data Versions", "There is no data version yet. Add one "
                                    "in Data → Data Versions….")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Compare Data Versions")
        form = QFormLayout(dialog)
        before, after = QComboBox(), QComboBox()
        for tag in tags:
            before.addItem(self._version_label(tag), tag)
            after.addItem(self._version_label(tag) + ("  (current)" if tag is tags[-1] else ""), tag)
        before.setCurrentIndex(max(0, len(tags) - 2))
        after.setCurrentIndex(after.count() - 1)
        form.addRow("From", before)
        form.addRow("To", after)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        start_tag, end_tag = before.currentData(), after.currentData()
        if (self._version_end(start_tag) is None or self._version_end(end_tag) is None) and not self._save_first(
                "Comparing with the current version"):
            return
        # Each version as it was at its end; the current one (no end yet) as saved now.
        start = self._version_end(start_tag) or "9999-12-31T23:59:59.999Z"
        end = self._version_end(end_tag) or "9999-12-31T23:59:59.999Z"
        start_name, end_name = start_tag.get("name"), end_tag.get("name")
        data = self._as_of_input()
        report = self._run_in_background(
            "Compare Data Versions", "Comparing…", 0,
            lambda _r, _c: compare_versions(data, start, end, start_tag.get("exclude"), end_tag.get("exclude")))
        totals = Counter()
        for counts in report["by_category"].values():
            totals.update(counts)
        lines = [
            f"{start_name} → {end_name}: {totals['added']:,} box(es) added, {totals['deleted']:,} deleted, "
            f"{totals['changed']:,} changed, on {len(report['images']):,} image(s); image states changed: "
            + (", ".join(f"{count:,} to {state}" for state, count in sorted(report["states"].items())) or "none")
            + ".",
            "", "By category (added / deleted / changed):",
            *(f"  {name}: {c['added']:,} / {c['deleted']:,} / {c['changed']:,}"
              for name, c in sorted(report["by_category"].items(), key=lambda item: item[0].casefold())),
            "", "By user (added / deleted / changed):",
            *(f"  {name}: {c['added']:,} / {c['deleted']:,} / {c['changed']:,}"
              for name, c in sorted(report["by_user"].items(), key=lambda item: item[0].casefold())),
        ]
        text = "\n".join(lines)
        result = QDialog(self)
        result.setWindowTitle(f"Changes {start_name} → {end_name}")
        layout = QVBoxLayout(result)
        browser = QPlainTextEdit(text)
        browser.setReadOnly(True)
        layout.addWidget(browser, 1)
        row = QHBoxLayout()
        show_button = QPushButton("List these images")
        show_button.setToolTip("Filter the image list to the images that changed")
        copy_button = QPushButton("Copy")
        note_button = QPushButton(f"Add to the note of {end_name}")
        note_button.setVisible(end_tag is not None)
        close_button = QPushButton("Close")
        for button in (show_button, copy_button, note_button):
            row.addWidget(button)
        row.addStretch(1)
        row.addWidget(close_button)
        layout.addLayout(row)

        def show_images() -> None:
            self._set_changed_since(f"{start_name} → {end_name}", set(report["images"]),
                                    unsaved=self._version_end(end_tag) is None,
                                    text=f"changed from {start_name} to {end_name}")
            result.accept()

        def add_to_note() -> None:
            end_tag["note"] = (str(end_tag.get("note") or "").rstrip() + "\n" + lines[0]).strip()
            self.dirty = True
            note_button.setEnabled(False)

        show_button.clicked.connect(show_images)
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(text))
        note_button.clicked.connect(add_to_note)
        close_button.clicked.connect(result.reject)
        result.resize(640, 480)
        result.exec()

    def _compact_history(self) -> None:
        """Shrink box history on the master project, once every copy has been merged into it."""
        if not self._editing_allowed():
            return
        if self.dirty:
            # Unsaved edits would put another version between saved ones, so only saved work is compacted.
            answer = QMessageBox.question(
                self, "Save before compacting",
                "Only saved history is compacted, and you have unsaved changes. Save them first?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer != QMessageBox.StandardButton.Save or not self.save_project():
                return
        self._commit_current_scene()
        history = self.annotation_data.get("annotation_history") or []
        annotations = self._all_annotations()
        kept, removed, boxes = self._run_in_background(
            "Compact History", "Looking for back-to-back versions…", 0,
            lambda _report, _cancelled: compact_history(history, annotations),
        )
        if not removed:
            QMessageBox.information(self, "Compact History", "There are no back-to-back versions to remove.")
            return
        images = {image_id for image_id, _box_id in boxes}
        answer = QMessageBox.warning(
            self, "Compact History",
            f"Remove {removed:,} earlier version(s) from {len(boxes):,} box(es) on {len(images):,} image(s)?\n\n"
            "Where a user saved several versions of a box in a row, only the newest is kept. Versions by "
            "other users, deletion records and the version before a deletion are kept.\n\n"
            "Do this only on the master project after every copy has been merged into it. A copy merged "
            "later may share only the removed versions, and its boxes would then come in as separate "
            "objects. This cannot be undone; the project file changes at the next save.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.annotation_data["annotation_history"] = kept
        self._history_changed()
        self._mark_images_unsaved(images)
        self._mark_dirty(touch_image=False)
        self._refresh_annotation_list(self._selected_item())
        self.status_label.setText(f"Removed {removed:,} earlier box version(s) — unsaved changes")
