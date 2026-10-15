"""Categories: choosing, adding, renaming, merging and coloring them.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from .graphics import BoxItem


class CategoriesMixin:
    """The category picker, the Categories dialog, Import Categories and retired categories."""

    def _retired_category(self, category_id: Any) -> dict[str, Any] | None:
        return next((category for category in self.annotation_data.get("retired_categories") or []
                     if category.get("id") == category_id), None)

    def _merged_into(self, category_id: Any) -> Any:
        """The category a retired one went to now, following later merges; None if there is none."""
        seen = set()
        while category_id not in self.categories and category_id not in seen:
            seen.add(category_id)
            retired = self._retired_category(category_id)
            if retired is None or retired.get("merged_into") is None:
                return None
            category_id = retired["merged_into"]
        return category_id if category_id in self.categories else None

    def _ask_category(self, title: str, current_id: Any = None) -> Any:
        """The type-to-search category picker; a new name becomes a category. None if cancelled."""
        names = sorted(self.categories.values(), key=str.casefold)
        current_name = self.categories.get(current_id, names[0] if names else "")
        name, accepted = QInputDialog.getItem(
            self, title, "Select a category, or type a new name to add it:",
            names, names.index(current_name) if current_name in names else 0, True,
        )
        name = name.strip()
        if not accepted or not name:
            return None
        category_id = next(
            (key for key, existing in self.categories.items() if existing.casefold() == name.casefold()),
            None,
        )
        if category_id is None:
            category_id = self._create_class(name)  # a new name becomes a category, like Edit → Categories… → Add
        return category_id

    def _change_category(self) -> None:
        """Change the selected box's category (right-click → Change category…, or its shortcut key)."""
        item = self._selected_item()
        if item is None or not self._editing_allowed():
            return
        category_id = self._ask_category("Change category", item.annotation.get("category_id"))
        if category_id is None or item.annotation.get("category_id") == category_id:
            return
        self._push_undo()
        item.annotation["category_id"] = category_id
        item.category_name = self.categories[category_id]
        item.update()
        self._refresh_annotation_list(item)
        self._annotation_changed(item.annotation, counts_changed=True)

    def _edit_labels(self) -> None:
        """Edit → Categories…: add, rename, merge (rename to an existing name, or Delete on a category boxes
        use), delete and color categories. Changes are listed in the dialog and applied together with Save;
        Cancel leaves the project as it was."""
        if not self._editing_allowed():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Categories")
        layout = QVBoxLayout(dialog)
        label_list = QListWidget(dialog)
        layout.addWidget(label_list)
        # Working list: key (a category id, or ("new", n) for one added here) -> name; and the changes in order.
        names: dict[Any, str] = dict(self.categories)
        changes: list[tuple] = []
        counter = iter(range(1, 1_000_000))

        def refresh(selected_key: Any = None) -> None:
            label_list.clear()
            for key, name in sorted(names.items(), key=lambda pair: pair[1].casefold()):
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, key)
                label_list.addItem(item)
                if key == selected_key:
                    label_list.setCurrentItem(item)
            if label_list.count() and label_list.currentRow() < 0:
                label_list.setCurrentRow(0)
            save_button.setEnabled(bool(changes))

        def selected_key() -> Any:
            item = label_list.currentItem()
            return item.data(Qt.ItemDataRole.UserRole) if item else None

        def usage(key: Any) -> tuple[int, int, int]:
            if isinstance(key, tuple):
                return 0, 0, 0
            return self._usage(lambda annotation: annotation.get("category_id") == key)

        def merge_into(key: Any, target: Any) -> None:
            del names[key]
            changes.append(("merge", key, target))
            refresh(target)

        def add_label() -> None:
            name, accepted = QInputDialog.getText(self, "Add category", "New category name:")
            name = name.strip()
            if not accepted or not name:
                return
            if any(existing.casefold() == name.casefold() for existing in names.values()):
                QMessageBox.information(self, "Category already exists", f"The category {name!r} is already available.")
                return
            key = ("new", next(counter))
            names[key] = name
            changes.append(("add", key, name))
            refresh(key)

        def rename_label() -> None:
            key = selected_key()
            if key is None:
                return
            old_name = names[key]
            name, accepted = QInputDialog.getText(self, "Rename category", "Category name:", text=old_name)
            name = name.strip()
            if not accepted or not name or name == old_name:
                return
            target = next((other for other, existing in names.items()
                           if other != key and existing.casefold() == name.casefold()), None)
            boxes, images, earlier = usage(key)
            if target is not None:
                if self._confirm("Merge categories", f"The category {names[target]!r} already exists. Merge "
                                 f"{old_name!r} into {names[target]!r}?\n\n{self._usage_text(boxes, images)} move to "
                                 f"{names[target]!r} as your edit, and {old_name!r} is removed. Applied when you "
                                 "click Save."):
                    merge_into(key, target)
                return
            if (boxes or earlier) and not self._confirm(
                    "Rename category", f"Rename {old_name!r} to {name!r}?\n\n{self._usage_text(boxes, images, earlier)} "
                    "will show the new name. Applied when you click Save."):
                return
            names[key] = name
            changes.append(("rename", key, name))
            refresh(key)

        def delete_label() -> None:
            key = selected_key()
            if key is None:
                return
            name = names[key]
            boxes, images, _earlier = usage(key)
            if boxes:
                others = sorted((other for other_key, other in names.items() if other_key != key), key=str.casefold)
                if not others:
                    QMessageBox.information(self, "Delete category", f"{name!r} is used by "
                                            f"{self._usage_text(boxes, images)}, and there is no other category to "
                                            "move them to.")
                    return
                target_name, accepted = QInputDialog.getItem(
                    self, "Delete category",
                    f"{name!r} is used by {self._usage_text(boxes, images)}. A category in use cannot be deleted; "
                    "its boxes can be moved to another category (as your edit), and it is then removed. Applied "
                    "when you click Save.\n\nMove to:", others, 0, False)
                if accepted and target_name:
                    merge_into(key, next(other_key for other_key, other in names.items() if other == target_name))
                return
            if not isinstance(key, tuple):
                reason = self._category_in_use(key)
                if reason:
                    QMessageBox.information(self, "Delete category", f"{name!r} cannot be deleted: {reason} Use "
                                            "Edit to merge it into another category instead.")
                    return
            if not self._confirm("Delete category", f"Delete category {name!r}? Nothing uses it. Applied when you "
                                 "click Save."):
                return
            del names[key]
            changes.append(("delete", key))
            refresh()

        def color_label() -> None:
            key = selected_key()
            if key is None:
                return
            current = next((change[2] for change in reversed(changes) if change[0] == "color" and change[1] == key),
                           None) or self._class_color(key)
            color = QColorDialog.getColor(current, self, f"Color for {names[key]}")
            if color.isValid():
                changes.append(("color", key, color))
                refresh(key)

        def save() -> None:
            ids: dict[Any, Any] = {}

            def resolve(key: Any) -> Any:
                return ids.get(key, key)

            skipped = []
            for change in changes:
                kind, key = change[0], change[1]
                if kind == "add":
                    ids[key] = self._create_class(change[2])
                elif kind == "rename":
                    self._apply_rename_class(resolve(key), change[2])
                elif kind == "merge":
                    self._merge_classes(resolve(key), resolve(change[2]))
                elif kind == "delete":
                    if self._category_in_use(resolve(key)) or any(
                            annotation.get("category_id") == resolve(key) for annotation in self._all_annotations()):
                        skipped.append(self.categories.get(resolve(key), str(key)))
                    else:
                        self._remove_category_entry(resolve(key))
                elif kind == "color":
                    self._apply_class_color(resolve(key), change[2])
            self._mark_dirty(counts_changed=True, touch_image=False)
            self._apply_frame_filters(select_first=False)
            self.status_label.setText("Categories changed — unsaved changes")
            if skipped:
                QMessageBox.information(self, "Categories", "Not deleted, because other changes use them now: "
                                        + ", ".join(skipped))
            dialog.accept()

        controls = QHBoxLayout()
        add_button = QPushButton("Add…", dialog)
        rename_button = QPushButton("Edit…", dialog)
        rename_button.setToolTip("Rename; a name that already exists offers to merge into it")
        delete_button = QPushButton("Delete…", dialog)
        color_button = QPushButton("Color…", dialog)
        for button in (add_button, rename_button, delete_button, color_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
                                   parent=dialog)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        add_button.clicked.connect(add_label)
        rename_button.clicked.connect(rename_label)
        delete_button.clicked.connect(delete_label)
        color_button.clicked.connect(color_label)
        label_list.itemDoubleClicked.connect(lambda _item: rename_label())
        refresh()
        dialog.setMinimumSize(480, 420)
        dialog.exec()

    def _import_classes(self) -> None:
        if not self._editing_allowed():
            return
        start_path = self.annotations_path or self.root
        json_path, _ = QFileDialog.getOpenFileName(
            self, "Import categories from annotation JSON", str(start_path), "JSON files (*.json)"
        )
        if not json_path:
            return
        try:
            annotation_data = json.loads(Path(json_path).read_text(encoding="utf-8"))
            if not isinstance(annotation_data, dict) or not isinstance(annotation_data.get("categories"), list):
                raise ValueError("The selected JSON must contain a categories array.")
            names = []
            for category in annotation_data["categories"]:
                if not isinstance(category, dict) or not isinstance(category.get("name"), str):
                    raise ValueError("Every category must have a name.")
                name = category["name"].strip()
                if not name:
                    raise ValueError("Category names cannot be empty.")
                names.append(name)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            QMessageBox.critical(self, "Cannot import categories", str(error))
            return

        existing = {name.casefold() for name in self.categories.values()}
        added = []
        for name in names:
            key = name.casefold()
            if key in existing:
                continue
            self._create_class(name)
            existing.add(key)
            added.append(name)
        if added:
            self._mark_dirty(touch_image=False)
            QMessageBox.information(
                self, "Categories imported", f"Imported {len(added):,} categories:\n" + ", ".join(added)
            )
        else:
            QMessageBox.information(self, "Categories imported", "All categories already exist in this project.")

    def _create_class(self, name: str) -> Any:
        numeric_ids = []
        for category_id in self.categories:
            try:
                numeric_ids.append(int(category_id))
            except (TypeError, ValueError):
                continue
        retired = self.annotation_data.get("retired_categories") or []
        revived = next((category for category in retired if str(category["name"]).casefold() == name.casefold()), None)
        if revived is not None:  # the same name again is the same category: it comes back under its id
            retired.remove(revived)
            self._add_category_entry(revived["id"], name)
            return revived["id"]
        numeric_ids.append(self._highest_id("category_id"))
        numeric_ids += [category["id"] for category in retired if isinstance(category.get("id"), int)]
        category_id = max(numeric_ids, default=0) + 1  # never reuses an id a box, saved version or retired one has
        self._highest_ids["category_id"] = category_id
        self._add_category_entry(category_id, name)
        return category_id

    def _add_category_entry(self, category_id: Any, name: str) -> None:
        self.categories[category_id] = name
        self.annotation_data.setdefault("categories", []).append({"id": category_id, "name": name})
        self._sync_frame_controls()
        self.draw_button.setToolTip("")
        filter_item = QListWidgetItem(name)
        filter_item.setData(Qt.ItemDataRole.UserRole, category_id)
        filter_item.setFlags(filter_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        filter_item.setCheckState(Qt.CheckState.Checked)
        self.class_filter.addItem(filter_item)
        self._show_matching_classes()

    def _apply_rename_class(self, category_id: Any, name: str) -> None:
        """Rename a category: same id, so every version, saved ones too, shows the new name."""
        if category_id not in self.categories:
            return
        self.categories[category_id] = name
        for category in self.annotation_data.get("categories", []):
            if category.get("id") == category_id:
                category["name"] = name
                break
        for box in self.current_items:
            if box.annotation.get("category_id") == category_id:
                box.category_name = name
                box.update()
        for row in range(self.class_filter.count()):
            filter_item = self.class_filter.item(row)
            if filter_item.data(Qt.ItemDataRole.UserRole) == category_id:
                filter_item.setText(name)
                break
        self._show_matching_classes()
        self._refresh_annotation_list(self._selected_item())

    def _merge_classes(self, source_id: Any, target_id: Any) -> None:
        self._commit_current_scene()
        source_name = self.categories[source_id]
        target_name = self.categories[target_id]
        changed_images = set()
        for image_id, annotations in self.annotations_by_image.items():
            for annotation in annotations:
                if annotation.get("category_id") == source_id:
                    annotation["category_id"] = target_id
                    changed_images.add(image_id)
                    if self.current_annotator:
                        self._set_annotation_annotator(annotation)
        self._mark_images_unsaved(changed_images)
        # The moved boxes become new versions at save; saved versions keep the category they had, which
        # stays listed as retired (with where it went). Undo must not restore boxes that point at it.
        for annotation in self._history_annotations():
            if annotation.get("category_id") == source_id:
                annotation["category_id"] = target_id
        self._remove_category_entry(source_id, merged_into=target_id)

        for item in self.current_items:
            if item.annotation.get("category_id") == target_id:
                item.category_name = target_name
                item.update()
        for image_id in changed_images:  # only these images' boxes moved
            self._set_image_roi_counts(image_id, self.annotations_by_image[image_id])
        self._selection_changed()
        self._mark_dirty(counts_changed=True, touch_image=False)
        self._apply_frame_filters(select_first=False)
        self.status_label.setText(
            f"Merged category {source_name!r} into {target_name!r} — unsaved changes"
        )

    def _category_name(self, category_id: Any) -> str:
        """A category's name, also for a retired one that saved versions still name."""
        if category_id in self.categories:
            return self.categories[category_id]
        retired = next((category for category in self.annotation_data.get("retired_categories") or []
                        if category.get("id") == category_id), None)
        return f"{retired['name']} (merged)" if retired else f"category {category_id}"

    def _remove_category_entry(self, category_id: Any, merged_into: Any = None) -> None:
        """Drop a category from the project and the category widgets; its boxes are handled by the caller.
        A category merged into another stays listed as retired, with the category it went to, so saved
        versions keep its name; one nothing uses is simply removed."""
        if merged_into is not None:
            retired = self.annotation_data.setdefault("retired_categories", [])
            retired[:] = [category for category in retired if category.get("id") != category_id]
            retired.append({"id": category_id, "name": self.categories[category_id], "merged_into": merged_into})
        self.annotation_data["categories"] = [
            category for category in self.annotation_data.get("categories", [])
            if category.get("id") != category_id
        ]
        del self.categories[category_id]
        BoxItem.class_colors.pop(category_id, None)
        if self._last_category_id == category_id:
            self._last_category_id = None
        for row in range(self.class_filter.count()):
            if self.class_filter.item(row).data(Qt.ItemDataRole.UserRole) == category_id:
                self.class_filter.takeItem(row)
                break

    def _category_in_use(self, category_id: Any) -> str:
        """Why a category without current boxes cannot be deleted ("" when nothing uses it): saved versions
        or undo steps still name it."""
        self._commit_current_scene()
        _boxes, _images, earlier = self._usage(lambda annotation: annotation.get("category_id") == category_id)
        self._ensure_saved_cache()
        if (earlier or any(box.get("category_id") == category_id for box in self._saved_cache[0].values())
                or any(annotation.get("category_id") == category_id for annotation in self._history_annotations())):
            return "saved versions or undo steps still use it."
        return ""

    def _class_color(self, category_id: Any) -> QColor:
        if category_id in BoxItem.class_colors:
            return BoxItem.class_colors[category_id]
        return BoxItem.default_color(category_id)

    def _apply_class_color(self, category_id: Any, color: QColor) -> None:
        if category_id not in self.categories:
            return
        BoxItem.class_colors[category_id] = color
        for item in self.scene.items():
            if isinstance(item, BoxItem):
                item.update()
        self._refresh_annotation_list(self._selected_item())
