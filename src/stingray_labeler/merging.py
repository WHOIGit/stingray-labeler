"""Merging two copies of a project: names, the merge dialog and taking the result.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

import difflib
import json
import re
import zlib
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .history import later_time, newest_time, utc_now
from .merge import (
    MergeResult, NameMap, build_project, legacy_state, merge, merged_away, name_counts, shared_name_pairs,
)


class MergingMixin:
    """Merge Project and Save → Merge: settling names, merging and adopting the merged project."""

    # ---------- merging two copies of a project ----------

    def _current_project_data(self) -> dict[str, Any]:
        """This session's project as a saved file would hold it (unverified images are ignored by the merge)."""
        self._commit_current_scene()
        return {
            "images": [
                dict(image, frame_state=self._frame_state(image["id"]),
                     background=self.background_by_image.get(image["id"], False))
                for image in self.images
            ],
            "annotations": [
                annotation for image in self.images for annotation in self.annotations_by_image[image["id"]]
            ],
            "categories": self.annotation_data.get("categories", []),
            "annotators": self.annotation_data.get("annotators", []),
        }

    def _reconcile_names(self, mine: dict[str, Any], theirs: dict[str, Any],
                         theirs_label: str) -> tuple[NameMap, NameMap, dict] | None:
        """Before boxes are combined, settle categories and users that only one copy has.

        Names in both copies are kept. A name only one copy has is new there and is added, unless the
        other copy shows it was renamed or merged since the copies parted: versions both copies hold name
        it differently, or (a category) the other copy merged it into another. Only then the dialog asks,
        with both copies' lists and box counts side by side: merge it as the other copy did, keep it, merge
        it into another name or (from the other copy) remove the category's boxes or leave the user's boxes
        unassigned. New names similar to one in the other copy are pointed out there. Decisions from
        earlier merges are filled in. Returns (this copy's map, the other copy's map, decisions to
        remember), or None if cancelled.
        """
        remembered = mine.get("name_aliases") or {}
        mine_counts, theirs_counts, pairs, mine_away, theirs_away = self._run_in_background(
            "Merge project", "Reading categories and users…", 0,
            lambda _report, _cancelled: (name_counts(mine), name_counts(theirs), shared_name_pairs(mine, theirs),
                                         merged_away(mine), merged_away(theirs)),
        )
        kinds = (("categories", "Categories", 0), ("users", "Users", 1))
        # Per kind: (key, side, [(suggested name, why)], similar names in the other copy)
        rows: dict[str, list[tuple[str, str, list[tuple[str, str]], list[str]]]] = {}
        pending = False
        for kind, _title, index in kinds:
            mine_names, theirs_names = mine_counts[index], theirs_counts[index]
            rows[kind] = []
            for key in sorted(mine_names.keys() | theirs_names.keys()):
                side = "both" if key in mine_names and key in theirs_names else (
                    "mine" if key in mine_names else "theirs")
                evidence: list[tuple[str, str]] = []
                similar: list[str] = []
                if side != "both":
                    others = theirs_names if side == "mine" else mine_names
                    away = (theirs_away if side == "mine" else mine_away) if kind == "categories" else {}
                    if key in away:
                        evidence.append((away[key], f"merged into {away[key]!r} "
                                         + (f"in {theirs_label}" if side == "mine" else "in your copy")))
                    for (here, there), count in pairs[index].items():
                        own, other = (here, there) if side == "mine" else (there, here)
                        if own.casefold() == key and other.casefold() in others:
                            evidence.append((others[other.casefold()][0], f"{count:,} shared version(s) are "
                                             f"{other!r} " + (f"in {theirs_label}" if side == "mine" else "in your copy")))
                    if not evidence:
                        similar = [others[other][0] for other in others if self._similar_names(key, other)]
                    if evidence and not (side == "theirs" and key in remembered.get(kind, {})):
                        pending = True
                rows[kind].append((key, side, evidence, similar))
        if not pending:  # every difference is a new name (kept), or earlier decisions cover it
            theirs_map = NameMap(*(dict(remembered.get(kind, {})) for kind in ("categories", "users")))
            return NameMap(), theirs_map, {}

        dialog = QDialog(self)
        dialog.setWindowTitle("Merge: categories and users")
        layout = QVBoxLayout(dialog)
        note = QLabel(
            "Some names were renamed or merged in one copy since the copies parted. Decide for each name marked "
            "\"Choose…\": merge it as the other copy did, keep it, or (from the other copy) remove the category's "
            "boxes or leave the user's boxes unassigned. Other names only one copy has are new and are kept; "
            "change them here if needed. Boxes are combined only after this."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        choices: dict[str, list[tuple[str, str, QComboBox]]] = {}
        all_combos: list[QComboBox] = []
        for kind, title, index in kinds:
            mine_names, theirs_names = mine_counts[index], theirs_counts[index]
            table = QTableWidget(len(rows[kind]), 6)
            table.setHorizontalHeaderLabels(["Your copy", "Boxes", theirs_label, "Boxes", "Note", "Decision"])
            table.verticalHeader().setVisible(False)
            choices[kind] = []
            for row, (key, side, evidence, similar) in enumerate(rows[kind]):
                if key in mine_names:
                    table.setItem(row, 0, QTableWidgetItem(mine_names[key][0]))
                    table.setItem(row, 1, QTableWidgetItem(f"{mine_names[key][1]:,}"))
                if key in theirs_names:
                    table.setItem(row, 2, QTableWidgetItem(theirs_names[key][0]))
                    table.setItem(row, 3, QTableWidgetItem(f"{theirs_names[key][1]:,}"))
                if side == "both":
                    table.setItem(row, 4, QTableWidgetItem("In both"))
                    table.setItem(row, 5, QTableWidgetItem("Same"))
                    continue
                table.setItem(row, 4, QTableWidgetItem(
                    "; ".join(why for _name, why in evidence) if evidence
                    else "New" + (f"; similar to {', '.join(repr(name) for name in similar)}" if similar else "")))
                combo = QComboBox()
                others = theirs_names if side == "mine" else mine_names
                if evidence:
                    combo.addItem("Choose…", "unset")
                suggested = []
                for name, _why in evidence:
                    if name.casefold() not in {other.casefold() for other in suggested}:
                        suggested.append(name)
                        combo.addItem(f"Merge into {name}", name)
                combo.addItem("Keep", "keep")
                for other_key in sorted(others, key=str.casefold):
                    if others[other_key][0] not in suggested:
                        combo.addItem(f"Merge into {others[other_key][0]}", others[other_key][0])
                if side == "theirs":
                    if kind == "categories":
                        combo.addItem("Remove (its boxes are not taken)", None)
                    else:
                        combo.addItem("Leave boxes unassigned", None)
                    earlier = remembered.get(kind, {})
                    if key in earlier:
                        found = combo.findData(earlier[key]) if earlier[key] is not None else combo.count() - 1
                        if earlier[key] is not None and earlier[key].casefold() == key:
                            found = combo.findData("keep")
                        if found >= 0:
                            combo.setCurrentIndex(found)
                table.setCellWidget(row, 5, combo)
                choices[kind].append((key, side, combo))
                all_combos.append(combo)
            table.resizeColumnsToContents()
            tabs.addTab(table, title)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        continue_button = buttons.addButton("Continue", QDialogButtonBox.ButtonRole.AcceptRole)

        def decided() -> None:
            continue_button.setEnabled(all(combo.currentData() != "unset" for combo in all_combos))

        for combo in all_combos:
            combo.currentIndexChanged.connect(lambda _index: decided())
        decided()
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(900, 520)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        mine_map, theirs_map = NameMap(), NameMap()
        for kind, _title, index in kinds:
            names = theirs_counts[index]
            decided_names = {key: combo.currentData() for key, side, combo in choices[kind] if side == "theirs"}
            for key, target in decided_names.items():
                getattr(theirs_map, kind)[key] = names[key][0] if target == "keep" else target
            for key, side, combo in choices[kind]:
                if side != "mine" or combo.currentData() == "keep":
                    continue
                target = combo.currentData()
                # Merged into a name only the other copy has, which may itself be merged or removed.
                final = getattr(theirs_map, kind).get(target.casefold(), target)
                if final is not None and final.casefold() != key:
                    getattr(mine_map, kind)[key] = final
        remember = {kind: dict(getattr(theirs_map, kind)) for kind in ("categories", "users")}
        return mine_map, theirs_map, remember

    @staticmethod
    def _similar_names(first: str, second: str) -> bool:
        """True for two names (ignoring case) that look like spellings of one name, e.g. salpa and salp."""
        a, b = (re.sub(r"[^0-9a-z]", "", name.casefold()) for name in (first, second))
        if not a or not b:
            return False
        if min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):
            return True
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8

    def _merge_dialog(self, mine: dict[str, Any], theirs: dict[str, Any], theirs_label: str,
                      accept_text: str) -> tuple[MergeResult, NameMap, dict, dict[str, Any]] | None:
        """Settle names, merge, show what happened, and ask for the data version the merge starts; return
        (result, this copy's name map, decisions, the new data version) if the user goes ahead. The project
        open is the main one: it keeps its own data versions, and the merged result is a new version."""
        names = self._reconcile_names(mine, theirs, theirs_label)
        if names is None:
            return None
        mine_map, theirs_map, remember = names
        outcome = self._run_in_background(
            "Merge project", "Comparing the two copies…", 0,
            lambda _report, _cancelled: merge(mine, theirs, mine_map, theirs_map),
        )
        dialog = QDialog(self)
        dialog.setWindowTitle("Merge project")
        layout = QVBoxLayout(dialog)
        summary_label = QLabel(
            f"Combining {theirs_label} with your saved project: {len(outcome.changed):,} image(s) change and "
            f"{outcome.versions_added:,} box version(s) are added. Nothing is overwritten: every version from "
            "both copies is kept in box history, ordered by time. An image with a box or image state changed in "
            "both copies is set to Review: check its boxes' history to settle it."
        )
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)
        details = QListWidget()
        layout.addWidget(details, 1)
        for kind, heading in (
            ("review", "Changed in both copies: set to Review"),
            ("unverified_here", "Unverified here, with boxes from " + theirs_label + ": combined"),
            ("added", "Images added from " + theirs_label),
            ("from_theirs", "Images with box versions from " + theirs_label),
        ):
            names = outcome.summary.get(kind, [])
            header = QListWidgetItem(f"{heading}: {len(names):,}")
            font = header.font()
            font.setBold(True)
            header.setFont(font)
            details.addItem(header)
            for name in names[:500]:
                details.addItem(f"    {name}")
            if len(names) > 500:
                details.addItem(f"    … and {len(names) - 500:,} more")
        version_form = QFormLayout()
        version_name = QLineEdit(f"Merged {Path(theirs_label).stem}")
        version_note = QLineEdit()
        version_form.addRow("New data version", version_name)
        version_form.addRow("Note (optional)", version_note)
        layout.addLayout(version_form)
        version_hint = QLabel("The merged project starts this data version; the versions of this project are kept, "
                              f"and those made only in {theirs_label} are not taken.")
        version_hint.setWordWrap(True)
        layout.addWidget(version_hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        accept_button = buttons.addButton(accept_text, QDialogButtonBox.ButtonRole.AcceptRole)
        version_name.textChanged.connect(lambda text: accept_button.setEnabled(bool(text.strip())))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(640, 560)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        # After every time either copy holds, so times keep moving forward in the merged project.
        newest = max(newest_time(mine), newest_time(theirs) if isinstance(theirs, dict) else "")
        new_version = {"name": version_name.text().strip(), "time": later_time(newest, utc_now()),
                       "by": self.current_annotator}
        if version_note.text().strip():
            new_version["note"] = version_note.text().strip()
        return outcome, mine_map, remember, new_version

    def _merge_input(self) -> dict[str, Any]:
        """This session's project with its box history, as a separate copy for merging."""
        data = self._current_project_data()
        # The merge copies every record it changes, so plain list copies are enough here.
        data["categories"] = [dict(category) for category in data["categories"]]
        data["annotators"] = [dict(user) for user in data["annotators"]]
        data["annotation_history"] = list(self.annotation_data.get("annotation_history") or [])
        for name in ("retired_categories", "name_aliases", "info", "licenses", "dataset_versions"):
            if name in self.annotation_data:
                data[name] = json.loads(json.dumps(self.annotation_data[name]))
        data["state_history"] = list(self.annotation_data.get("state_history") or [])
        return data

    def _adopt_project(self, project: dict[str, Any], changed_ids: set[Any], added_ids: set[Any]) -> None:
        """Take a merged project (from build_project) into this session; unchanged images are left alone."""
        self._commit_current_scene()
        self._newest_time = None  # the other copy's times may be newer: found again at the next save
        kept_categories = {category["id"] for category in project.get("categories", [])}
        for category_id in [category_id for category_id in self.categories if category_id not in kept_categories]:
            self._remove_category_entry(category_id)  # merged into another in the reconcile step
        for category in project.get("categories", []):
            if category["id"] not in self.categories:
                self._add_category_entry(category["id"], str(category["name"]))
        for name in ("retired_categories", "name_aliases", "dataset_versions"):
            self.annotation_data.pop(name, None)  # a list the merge no longer has is gone
            if name in project:
                self.annotation_data[name] = json.loads(json.dumps(project[name]))
        self.annotation_data["state_history"] = list(project.get("state_history") or [])
        self.annotation_data["annotators"] = [dict(user) for user in project.get("annotators", [])]
        self._rebuild_user_cache()
        self._refresh_annotator_filter()
        records = {image["id"]: image for image in project.get("images", [])}
        for image_id in added_ids:
            image = dict(records[image_id])
            self.images.append(image)
            self.image_by_id[image_id] = image
            if self.root is not None:
                self.source_paths_by_image[image_id] = self.root / image["file_name"]
            self.annotations_by_image[image_id] = []
            self.roi_counts_by_image[image_id] = {}
            self.persisted_image_ids.add(image_id)
            if isinstance(image_id, int):
                self._next_image_id = max(self._next_image_id, image_id + 1)
        boxes_by_image: dict[Any, list[dict[str, Any]]] = {}
        for annotation in project.get("annotations", []):
            boxes_by_image.setdefault(annotation["image_id"], []).append(annotation)
        for image_id in changed_ids:
            state = legacy_state(records[image_id])
            self.frame_verified_by_image[image_id] = state == "verified"
            self.review_by_image[image_id] = state == "review"
            self.background_by_image[image_id] = bool(records[image_id].get("background"))
            self.touched_image_ids.add(image_id)
            self._update_frame_item_text(image_id)
            annotations = boxes_by_image.get(image_id, [])
            if image_id == self.current_image_id:
                self._restore_image_annotations(image_id, annotations)  # the image on screen is redrawn
            else:  # others: data only; the list and counts are refreshed once below
                self.annotations_by_image[image_id] = annotations
                self._set_image_roi_counts(image_id, annotations)
                self._pair_ious_by_image.pop(image_id, None)
        # A copy saved under the old rule can hold a Verified image with an unverified box.
        self._settle_verified_states(changed_ids | added_ids, credit_nobody=True)
        history = list(project.get("annotation_history") or [])
        if any(image_id not in records for image_id in self.image_by_id):
            history += [record for record in self.annotation_data.get("annotation_history") or []
                        if record.get("image_id") not in records]
        self.annotation_data["annotation_history"] = history
        self._history_changed()
        # Box ids and versions changed under the undo history, so it no longer applies.
        self._undo_history.clear()
        self._redo_history.clear()
        self.next_annotation_id = max(
            (int(annotation["id"]) for annotation in self._all_annotations()
             if isinstance(annotation.get("id"), int)), default=0,
        ) + 1
        self.dirty = True
        self._highest_ids = {}
        if self._iou_filter_on():
            for image_id in changed_ids:
                self._refresh_pair_ious(image_id)
        if added_ids:
            self._rebuild_frame_list(show_progress=True)
        else:
            self._apply_frame_filters(select_first=False)
        if self.current_image_id is not None:
            self._refresh_annotation_list(self._selected_item())  # grey rows come from the merged history
        self._sync_frame_controls()

    def merge_project(self) -> None:
        """Combine another saved copy of this project with the saved state of this one."""
        if not self._editing_allowed():
            return
        if self.dirty:
            answer = QMessageBox.question(
                self, "Save before merging",
                "Only saved work is merged, and you have unsaved changes. Save them first?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer != QMessageBox.StandardButton.Save or not self.save_project():
                return
        if self._base_json is None:
            QMessageBox.information(self, "Save before merging", "Save the project first; only saved work is merged.")
            return
        start = str((self.annotations_path or self.root).parent) if (self.annotations_path or self.root) else ""
        other, _ = QFileDialog.getOpenFileName(
            self, "Choose the other copy of the project", start, "JSON files (*.json)"
        )
        if not other:
            return
        try:
            theirs = json.loads(Path(other).read_bytes())
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Cannot merge", f"Could not read {Path(other).name}:\n\n{error}")
            return
        if isinstance(theirs, dict):
            self._normalize_user_ids(theirs)
        mine = self._merge_input()
        merged = self._merge_dialog(mine, theirs, Path(other).name, "Merge")
        if merged is None:
            return
        outcome, mine_map, remember, new_version = merged
        reserved = set(self.image_by_id)
        def combine(_report, _cancelled):
            built = build_project(mine, outcome, reserved, mine_map, remember, new_version)
            # The merged project becomes the baseline; serialized and indexed here, off the window's thread.
            return built, zlib.compress(json.dumps(built[0]).encode("utf-8"), 1), self._saved_index(built[0])

        (project, changed_ids, added_ids), base_json, prepared = self._run_in_background(
            "Merge project", "Combining boxes…", 0, combine,
        )
        self._adopt_project(project, changed_ids, added_ids)
        # A merge starts a new session: the merged project is the baseline (nothing in it is an unsaved
        # edit of this session), and it is saved at once.
        self._base_json = base_json
        self._set_saved_cache(None, base_json, prepared)
        if self._save_project(self.annotations_path):
            self.status_label.setText(f"Merged {Path(other).name} and saved.")
        else:
            self.status_label.setText(
                f"Merged {Path(other).name} — not saved yet. Images from the other copy open from the project "
                "folder; add their folder if they are elsewhere, then save."
            )
