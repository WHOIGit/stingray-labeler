"""Users: the active user, user ids and the Users dialog.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)


class UsersMixin:
    """The active user, user ids and names, and the Users dialog."""

    def _rebuild_user_cache(self) -> None:
        """Refresh the id <-> name lookups; only needed when users are loaded, added, renamed or merged."""
        self._user_name_by_id = {
            self._user_key(user["id"]): str(user.get("name", ""))
            for user in self.annotation_data.get("annotators", [])
            if isinstance(user, dict) and "id" in user
        }
        self._user_id_by_name = {name: user_id for user_id, name in self._user_name_by_id.items()}

    @staticmethod
    def _user_key(value: Any) -> Any:
        """A user id as one key: a number when it reads as one (3, "3", 3.0), otherwise the text itself."""
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return value.strip()
        return value

    def _remap_user(self, old_id: Any, new_id: Any) -> None:
        """Remember that boxes saved under ``old_id`` are now credited to ``new_id`` (None: unassigned)."""
        for earlier, credited in list(self._user_remap.items()):
            if credited == old_id:
                self._user_remap[earlier] = new_id
        self._user_remap[old_id] = new_id

    def _annotator_id(self, name: str) -> int:
        user_id = self._user_id_by_name.get(name)
        if user_id is None:  # names are matched ignoring case, like categories
            user_id = next((uid for other, uid in self._user_id_by_name.items()
                            if other.casefold() == name.casefold()), None)
        if user_id is not None:
            return user_id
        taken = [value for value in (*self._user_name_by_id, *self._user_remap) if isinstance(value, int)]
        user_id = max(max(taken, default=0), self._highest_id("annotator_id")) + 1  # never reused
        self._highest_ids["annotator_id"] = user_id
        self.annotation_data.setdefault("annotators", []).append({"id": user_id, "name": name})
        self._user_name_by_id[user_id] = name
        self._user_id_by_name[name] = user_id
        self._refresh_annotator_filter()  # a new user is the only reason to touch the dropdown
        return user_id

    def _set_annotation_annotator(self, annotation: dict[str, Any]) -> None:
        if self.current_annotator:
            annotation["annotator_id"] = self._annotator_id(self.current_annotator)

    def _annotation_user_name(self, annotation: dict[str, Any]) -> str:
        """Who a box is credited to: its user; "model" for a box with a model score and no user; "–" otherwise."""
        user_id = self._user_key(annotation.get("annotator_id"))
        if isinstance(user_id, (int, str)) and user_id in self._user_name_by_id:
            return self._user_name_by_id[user_id]
        return "model" if self._model_score(annotation) is not None else "–"

    def _show_user_name(self) -> None:
        """Show the active user's name in the menu bar corner, sized to fit."""
        self.user_label.setText(self.current_annotator or "No user")
        self.user_label.setToolTip(
            f"Active user: {self.current_annotator}. Change it from Edit → Change User…"
            if self.current_annotator else "No active user. Choose one from Edit → Choose User…"
        )
        self.user_label.adjustSize()
        # The menu bar sizes its corner widget only on layout; setting it again re-lays it out at the new width.
        self.menuBar().setCornerWidget(self.user_label, Qt.Corner.TopRightCorner)

    def _activate_annotator(self, name: str) -> None:
        is_new = name not in self._user_id_by_name
        self.current_annotator = name
        self._annotator_id(name)
        self._show_user_name()
        self.change_annotator_action.setText(f"Change User… ({name})")
        self._set_annotation_editing_enabled(True)
        if is_new:
            self.dirty = True
            self.status_label.setText(f"Unsaved user record: {name}")
        else:
            self.status_label.setText(f"Active user: {name}")

    def _choose_annotator(
        self, annotation_data: dict[str, Any], *, include_current: bool = False,
    ) -> str | None:
        names = {
            value.strip()
            for annotator in annotation_data.get("annotators", [])
            if isinstance(annotator, dict)
            and isinstance((value := annotator.get("name")), str)
            and value.strip()
        }
        if include_current and self.current_annotator:
            names.add(self.current_annotator)
        choices = sorted(names, key=str.casefold)
        dialog = QDialog(self)
        dialog.setWindowTitle("Select user")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Choose an existing user or enter a new name:"))
        name_input = QComboBox(dialog)
        name_input.setEditable(True)
        name_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)  # Enter never adds a name to the list
        name_input.addItems(choices)
        if self.current_annotator:
            name_input.setCurrentText(self.current_annotator)
        elif choices:
            name_input.setCurrentIndex(0)
        layout.addWidget(name_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)

        def name_typed(text: str) -> None:
            ok_button.setEnabled(bool(text.strip()))  # OK and Enter do nothing until a name is there

        def accept() -> None:
            if name_input.currentText().strip():
                dialog.accept()

        name_input.currentTextChanged.connect(name_typed)
        name_typed(name_input.currentText())
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.setMinimumWidth(360)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        value = name_input.currentText().strip()
        value = next((name for name in choices if name.casefold() == value.casefold()), value)
        return value

    def _change_annotator(self) -> None:
        if self.root is None:
            return
        value = self._choose_annotator(self.annotation_data, include_current=True)
        if value is None:
            return
        self._activate_annotator(value)

    def _users_changed(self, select_filter: str | None = None) -> None:
        self._mark_dirty(touch_image=False)
        self._show_user_name()
        self.change_annotator_action.setText(
            f"Change User… ({self.current_annotator})"
            if self.current_annotator else "Choose User…"
        )
        self._rebuild_user_cache()
        self._refresh_annotation_list(self._selected_item())
        self._refresh_annotator_filter(select=select_filter, refresh_frames=False)
        # Boxes changed owner, so re-check rows against the active user filter.
        self._apply_frame_filters(select_first=False)

    def _edit_annotators(self) -> None:
        """Edit → Users…: add, rename, merge (rename to an existing name) and delete users. Changes are
        listed in the dialog and applied together with Save; Cancel leaves the project as it was."""
        if not self._editing_allowed():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Users")
        layout = QVBoxLayout(dialog)
        annotator_list = QListWidget(dialog)
        layout.addWidget(annotator_list)
        # Working list: key (a user id, or ("new", n) for one added here) -> name; and the changes in order.
        names: dict[Any, str] = {user["id"]: user["name"] for user in self.annotation_data.get("annotators", [])}
        changes: list[tuple] = []
        counter = iter(range(1, 1_000_000))

        def refresh(selected_key: Any = None) -> None:
            annotator_list.clear()
            for key, name in sorted(names.items(), key=lambda pair: pair[1].casefold()):
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, key)
                annotator_list.addItem(item)
                if key == selected_key:
                    annotator_list.setCurrentItem(item)
            if annotator_list.count() and annotator_list.currentRow() < 0:
                annotator_list.setCurrentRow(0)
            save_button.setEnabled(bool(changes))

        def selected_key() -> Any:
            item = annotator_list.currentItem()
            return item.data(Qt.ItemDataRole.UserRole) if item else None

        def usage(key: Any) -> tuple[int, int, int]:
            if isinstance(key, tuple):
                return 0, 0, 0
            return self._usage(lambda annotation: annotation.get("annotator_id") == key)

        def rename_annotator() -> None:
            key = selected_key()
            if key is None:
                return
            old_name = names[key]
            name, accepted = QInputDialog.getText(self, "Rename user", "User name:", text=old_name)
            name = name.strip()
            if not accepted or not name or name == old_name:
                return
            target = next((other for other, existing in names.items()
                           if other != key and existing.casefold() == name.casefold()), None)
            boxes, images, earlier = usage(key)
            if target is not None:
                if not self._confirm("Merge users", f"Merge {old_name!r} into {names[target]!r}?\n\n"
                                     f"{self._usage_text(boxes, images, earlier)} will be credited to "
                                     f"{names[target]!r}, saved versions too. Applied when you click Save."):
                    return
                del names[key]
                changes.append(("merge", key, target))
                refresh(target)
                return
            if (boxes or earlier) and not self._confirm(
                    "Rename user", f"Rename {old_name!r} to {name!r}?\n\n{self._usage_text(boxes, images, earlier)} "
                    "will show the new name. Applied when you click Save."):
                return
            names[key] = name
            changes.append(("rename", key, name))
            refresh(key)

        def add_annotator() -> None:
            name, accepted = QInputDialog.getText(self, "Add user", "User name:")
            name = name.strip()
            if not accepted or not name:
                return
            if any(existing.casefold() == name.casefold() for existing in names.values()):
                QMessageBox.information(self, "User already exists", f"The user {name!r} already exists.")
                return
            key = ("new", next(counter))
            names[key] = name
            changes.append(("add", key, name))
            refresh(key)

        def delete_annotator() -> None:
            key = selected_key()
            if key is None:
                return
            name = names[key]
            if not isinstance(key, tuple):
                reason = self._user_in_use(key)
                if reason:
                    QMessageBox.information(self, "Delete user", f"{name!r} cannot be deleted: {reason} Use Edit "
                                            "to merge them into another user instead.")
                    return
            if not self._confirm("Delete user", f"Delete user {name!r}? Nothing is credited to them. Applied when "
                                 "you click Save."):
                return
            del names[key]
            changes.append(("delete", key))
            refresh()

        def save() -> None:
            ids: dict[Any, Any] = {}

            def resolve(key: Any) -> Any:
                return ids.get(key, key)

            skipped = []
            for change in changes:
                kind, key = change[0], change[1]
                if kind == "add":
                    ids[key] = self._annotator_id(change[2])
                elif kind == "rename":
                    self._apply_rename_user(resolve(key), change[2])
                elif kind == "merge":
                    self._apply_merge_users(resolve(key), resolve(change[2]))
                elif kind == "delete":
                    reason = self._user_in_use(resolve(key))
                    if reason:
                        skipped.append(self._user_name_by_id.get(resolve(key), str(key)))
                    else:
                        self._apply_delete_user(resolve(key))
            self._users_changed()
            self.status_label.setText("Users changed — unsaved changes")
            if skipped:
                QMessageBox.information(self, "Users", "Not deleted, because other changes credit them now: "
                                        + ", ".join(skipped))
            dialog.accept()

        controls = QHBoxLayout()
        add_button = QPushButton("Add…", dialog)
        rename_button = QPushButton("Edit…", dialog)
        rename_button.setToolTip("Rename; a name that already exists offers to merge into it")
        delete_button = QPushButton("Delete…", dialog)
        controls.addWidget(add_button)
        controls.addWidget(rename_button)
        controls.addWidget(delete_button)
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
                                   parent=dialog)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        add_button.clicked.connect(add_annotator)
        rename_button.clicked.connect(rename_annotator)
        delete_button.clicked.connect(delete_annotator)
        annotator_list.itemDoubleClicked.connect(lambda _item: rename_annotator())
        refresh()
        dialog.setMinimumSize(420, 360)
        dialog.exec()

    def _user_in_use(self, user_id: Any) -> str:
        """Why a user cannot be deleted ("" when nothing credits them): the active user, or credited on a box,
        saved version, image state or undo step."""
        name = self._user_name_by_id.get(user_id)
        if name is not None and name == self.current_annotator:
            return "they are the active user."
        self._commit_current_scene()

        def credited(annotation: dict[str, Any]) -> bool:
            return annotation.get("annotator_id") == user_id

        count, images, earlier = self._usage(credited)
        if count or earlier:
            return f"they are credited on {self._usage_text(count, images, earlier)}."
        self._ensure_saved_cache()
        if (any(credited(box) for box in self._saved_cache[0].values())
                or any(entry.get("annotator_id") == user_id for entry in self.annotation_data.get("state_history") or [])
                or any(image.get("state_by") == user_id for image in self.images)
                or any(credited(annotation) for annotation in self._history_annotations())):
            return "saved work or undo steps still credit them."
        return ""

    def _apply_rename_user(self, user_id: Any, name: str) -> None:
        record = next((user for user in self.annotation_data.get("annotators", []) if user["id"] == user_id), None)
        if record is None:
            return
        if self.current_annotator == record["name"]:
            self.current_annotator = name
        record["name"] = name

    def _apply_merge_users(self, source_id: Any, target_id: Any) -> None:
        """The same person under two names: every version of the source user, saved ones and the image states
        they set too, is credited to the target, and the source user is removed."""
        users = self.annotation_data.get("annotators", [])
        source = next((user for user in users if user["id"] == source_id), None)
        target = next((user for user in users if user["id"] == target_id), None)
        if source is None or target is None:
            return
        self._commit_current_scene()
        changed_images = set()
        for annotation in self._all_annotations():
            if annotation.get("annotator_id") == source_id:
                changed_images.add(annotation["image_id"])
        for annotation in (*self._all_annotations(), *self._history_annotations(),
                           *(self.annotation_data.get("annotation_history") or [])):
            if annotation.get("annotator_id") == source_id:
                annotation["annotator_id"] = target_id
        for entry in self.annotation_data.get("state_history") or []:
            if entry.get("annotator_id") == source_id:
                entry["annotator_id"] = target_id
        for image in self.images:
            if image.get("state_by") == source_id:
                image["state_by"] = target_id
        self._history_changed()
        self._remap_user(source_id, target_id)
        self._mark_images_unsaved(changed_images)
        users.remove(source)
        if self.current_annotator == source["name"]:
            self.current_annotator = target["name"]

    def _apply_delete_user(self, user_id: Any) -> None:
        users = self.annotation_data.get("annotators", [])
        users[:] = [user for user in users if user["id"] != user_id]

    def _refresh_annotator_filter(
        self, *, reset: bool = False, select: str | None = None,
        refresh_frames: bool = True,
    ) -> None:
        if not hasattr(self, "annotator_filter"):
            return
        before = self.annotator_filter.currentData()
        previous = select or (self.ALL_ANNOTATORS_FILTER_ID if reset else before)
        names = {name.strip() for name in self._user_id_by_name if name.strip()}
        if self.current_annotator:
            names.add(self.current_annotator)
        self.annotator_filter.blockSignals(True)
        self.annotator_filter.clear()
        self.annotator_filter.addItem("All users", self.ALL_ANNOTATORS_FILTER_ID)
        for name in sorted(names, key=str.casefold):
            self.annotator_filter.addItem(name, name)
        self.annotator_filter.addItem(
            "Background / no annotations", self.BACKGROUND_ANNOTATOR_FILTER_ID
        )
        index = self.annotator_filter.findData(previous)
        self.annotator_filter.setCurrentIndex(index if index >= 0 else 0)
        self.annotator_filter.blockSignals(False)
        # Signals were blocked above, so re-check rows when the active filter moved.
        if refresh_frames and self.annotator_filter.currentData() != before:
            self._apply_frame_filters(select_first=False)
