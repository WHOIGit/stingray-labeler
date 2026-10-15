"""Project files: new, open, add images, autosave and save.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import socket
import threading
import time
import zlib
from base64 import b64decode
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEventLoop, QFile, QStandardPaths, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox, QProgressDialog
from PIL import Image

from .avi import AviInfo, VideoFrameRef, read_avi_info
from .dataset import FileChangedDuringSave, OperationCancelled, is_verified, dataset_image_path, path_key
from .history import is_box_id, new_box_id, record_states, record_versions, saved_boxes
from .merge import FRAME_STATES, build_project, image_key, legacy_state


class ProjectFilesMixin:
    """Opening, reading, autosaving and saving a project, and adding image sources."""

    # ---------- autosave / crash recovery ----------

    @staticmethod
    def _autosave_file(key_path: Path | None) -> Path | None:
        """Backup location on this PC (never next to a shared project), keyed by project file or image folder."""
        if key_path is None:
            return None
        base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation))
        digest = hashlib.sha1(path_key(key_path).encode("utf-8")).hexdigest()[:16]
        return base / "StingrayLabeler" / "autosave" / f"{digest}.json"

    def _current_autosave_file(self) -> Path | None:
        return self._autosave_file(self.annotations_path or self.root)

    def _discard_autosave(self, key_path: Path | None = None) -> None:
        if self._autosave_thread is not None and self._autosave_thread.is_alive():
            self._autosave_thread.join(5)  # a backup still being written would come back after the delete
        path = self._autosave_file(key_path) if key_path is not None else self._current_autosave_file()
        if path is not None:
            for leftover in (path, self._autosave_base_file(path)):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            self._autosave_base_source = None

    @staticmethod
    def _autosave_base_file(path: Path) -> Path:
        """The saved project a backup builds on, kept beside it and rewritten only when it changes."""
        return path.with_name(path.stem + ".base.zlib")

    def _autosave(self) -> None:
        """Every few minutes, back up unsaved work so a crash or power loss does not lose it.

        Only what changed since the save is copied: the boxes of images with unsaved changes and,
        if it changed, the box history. The saved project itself is kept once beside the backup.
        Writing happens in small pieces on another thread, so the window does not stall.
        """
        if not self.dirty or self.root is None or self._edit_count == self._autosaved_count:
            return  # nothing unsaved, or nothing changed since the last backup
        if self._autosave_thread is not None and self._autosave_thread.is_alive():
            return
        path = self._current_autosave_file()
        if path is None:
            return
        self._commit_current_scene()
        has_base = self._base_json is not None
        changed = set(self.touched_image_ids) if has_base else set(self.annotations_by_image)
        boxes = {str(image_id): self._copy_annotations(self.annotations_by_image.get(image_id, []))
                 for image_id in changed if image_id in self.image_by_id}
        history = (list(self.annotation_data.get("annotation_history") or [])
                   if self._history_unsaved or not has_base else None)
        meta = json.loads(json.dumps({name: value for name, value in self.annotation_data.items()
                                      if name not in ("images", "annotations", "annotation_history")}))
        images = [dict(image) for image in self.images]
        state = [
            {
                "id": image["id"],
                "frame_verified": self.frame_verified_by_image.get(image["id"], False),
                "review": self.review_by_image.get(image["id"], False),
                "background": self.background_by_image.get(image["id"], False),
                # Written only for images opening from somewhere other than the project folder.
                "source": str(self.source_paths_by_image[image["id"]])
                if image["id"] in self.source_paths_by_image
                and self.source_paths_by_image[image["id"]].parent != self.root else None,
                "video": [str(self.video_frame_by_image[image["id"]][0].path),
                          self.video_frame_by_image[image["id"]][1]]
                if image["id"] in self.video_frame_by_image else None,
                "touched": image["id"] in self.touched_image_ids,
                "persisted": image["id"] in self.persisted_image_ids,
            }
            for image in self.images
        ]
        head = {
            "autosave_version": 2,
            "saved_at": time.time(),
            "root": str(self.root),
            "annotations_path": str(self.annotations_path) if self.annotations_path else None,
            "file_signature": list(self._file_signature) if self._file_signature else None,
            "annotator": self.current_annotator,
            "videos": [str(info.path) for info in self.videos.values()],
            "has_base": has_base,
            "purged": [list(entry) for entry in self._purged_boxes],
            "trash_on_save": [str(path) for path in self._trash_on_save],
            "user_remap": [[old, new] for old, new in self._user_remap.items()],
            "deleted_by": [[key, box_id, user] for (key, box_id), user in self._deleted_by.items()],
            "state_by": [[key, user] for key, user in self._state_by.items()],
            "meta": meta,
        }
        base_bytes = self._base_json if has_base and self._autosave_base_source is not self._base_json else None
        base_source = self._base_json
        edit_count = self._edit_count

        def write_list(stream, name: str, items: list) -> None:
            stream.write(f', "{name}": [')
            for index, item in enumerate(items):
                stream.write((", " if index else "") + json.dumps(item))  # small pieces: the window keeps going
            stream.write("]")

        def write() -> None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if base_bytes is not None:
                    base_path = self._autosave_base_file(path)
                    base_temporary = base_path.with_name(base_path.name + ".tmp")
                    base_temporary.write_bytes(base_bytes)
                    base_temporary.replace(base_path)
                temporary_path = path.with_name(path.name + ".tmp")
                with temporary_path.open("w", encoding="utf-8") as stream:
                    stream.write(json.dumps(head)[:-1])
                    write_list(stream, "images", images)
                    write_list(stream, "state", state)
                    stream.write(', "boxes": {')
                    for index, (image_id, annotations) in enumerate(boxes.items()):
                        stream.write((", " if index else "") + json.dumps(image_id) + ": " + json.dumps(annotations))
                    stream.write("}")
                    if history is None:
                        stream.write(', "history": null')
                    else:
                        write_list(stream, "history", history)
                    stream.write("}")
                temporary_path.replace(path)
                self._autosave_base_source = base_source
                self._autosaved_count = edit_count
            except OSError:
                pass  # a failed backup must never interrupt annotation work

        self._autosave_thread = threading.Thread(target=write, daemon=True)
        self._autosave_thread.start()
        self.statusBar().showMessage(f"Autosaved unsaved work at {time.strftime('%H:%M')}", 8000)

    def _autosave_project(self, snapshot: dict[str, Any]) -> tuple[dict[str, Any], bytes | None, tuple | None]:
        """Rebuild the backed-up project: the saved project kept beside the backup, with the backup's
        images, changed boxes and (if changed) history laid over it. Returns it and the saved project."""
        if "project" in snapshot:  # a backup holding the whole project
            base_json = b64decode(snapshot["merge_base"]) if snapshot.get("merge_base") else None
            return snapshot["project"], base_json, None
        base_json, base = None, {}
        if snapshot.get("has_base"):
            key_path = Path(snapshot["annotations_path"]) if snapshot.get("annotations_path") else Path(snapshot["root"])
            backup = self._autosave_file(key_path)
            try:
                base_json = self._autosave_base_file(backup).read_bytes()
                base = json.loads(zlib.decompress(base_json))
            except (OSError, ValueError, zlib.error):
                try:  # the saved project itself, if the copy beside the backup is gone
                    base_json = zlib.compress(Path(snapshot["annotations_path"]).read_bytes(), 1)
                    base = json.loads(zlib.decompress(base_json))
                except (OSError, TypeError, ValueError, zlib.error):
                    base_json, base = None, {}
        project = dict(snapshot.get("meta") or {})
        project["images"] = snapshot.get("images", [])
        changed = snapshot.get("boxes") or {}
        saved_by_image: dict[Any, list[dict[str, Any]]] = {}
        for annotation in base.get("annotations", []):
            saved_by_image.setdefault(annotation.get("image_id"), []).append(annotation)
        project["annotations"] = [
            annotation for image in project["images"]
            for annotation in (changed[str(image["id"])] if str(image["id"]) in changed
                               else saved_by_image.get(image["id"], []))
        ]
        history = snapshot.get("history")
        project["annotation_history"] = history if history is not None else base.get("annotation_history", [])
        # Indexed now, before opening the project changes the records it shares with the saved one.
        return project, base_json, (self._saved_index(base) if base_json is not None else None)

    def _offer_autosave(self, key_path: Path, newer_than: float = 0.0) -> dict[str, Any] | None:
        """Return a backup the user chose to restore, if one newer than the project file exists."""
        path = self._autosave_file(key_path)
        if path is None or not path.exists():
            return None
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        saved_at = float(snapshot.get("saved_at", 0))
        # Always offered: the project file may be newer because someone else saved it, not because
        # this work was saved. A restored session keeps the file's old signature, so Save offers Merge.
        newer_file = saved_at <= newer_than
        answer = QMessageBox.question(
            self, "Recover unsaved work?",
            f"Unsaved work from {time.strftime('%Y-%m-%d %H:%M', time.localtime(saved_at))} was found "
            f"for this project (it was not saved before the app closed)."
            + ("\n\nThe project file was saved after this backup, by you or someone else. If you restore "
               "it, Save will offer to merge it with the newer file." if newer_file else "")
            + "\n\nRestore it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._discard_autosave(key_path)
            return None
        return snapshot

    def _restore_autosave(self, root: Path, snapshot: dict[str, Any]) -> None:
        annotations_path = snapshot.get("annotations_path")
        self.current_annotator = None
        project, base_json, prepared = self._run_in_background(
            "Recover unsaved work", "Reading the backup…", 0,
            lambda _report, _cancelled: self._autosave_project(snapshot),
        )
        self._set_dataset(root, project, Path(annotations_path) if annotations_path else None)
        signature = snapshot.get("file_signature")
        self._file_signature = (int(signature[0]), int(signature[1])) if signature else None

        def video_info(text: str) -> AviInfo | None:
            key = path_key(Path(text))
            if key not in self.videos:
                try:
                    self.videos[key] = read_avi_info(Path(text))
                except (OSError, ValueError):
                    return None
            return self.videos[key]

        if base_json is not None:
            self._base_json = base_json
            if prepared is not None:
                self._set_saved_cache(None, base_json, prepared)
        self._history_unsaved = snapshot.get("history") is not None or "project" in snapshot
        self._purged_boxes = {tuple(entry) for entry in snapshot.get("purged", [])}
        self._trash_on_save = {Path(path) for path in snapshot.get("trash_on_save", [])}
        self._user_remap = {old: new for old, new in snapshot.get("user_remap", [])}
        self._deleted_by = {(key, box_id): user for key, box_id, user in snapshot.get("deleted_by", [])}
        self._state_by = {key: user for key, user in snapshot.get("state_by", [])}
        for text in snapshot.get("videos", []):
            video_info(text)
        persisted = set()
        for entry in snapshot.get("state", []):
            image_id = entry.get("id")
            if image_id not in self.image_by_id:
                continue
            self.frame_verified_by_image[image_id] = bool(entry.get("frame_verified"))
            # Backups from before image states existed saved every touched or saved image; keep them as review.
            review = entry["review"] if "review" in entry else entry.get("touched") or entry.get("persisted")
            self.review_by_image[image_id] = bool(review) and not entry.get("frame_verified")
            self.background_by_image[image_id] = bool(entry.get("background"))
            if entry.get("source"):
                self.source_paths_by_image[image_id] = Path(entry["source"])
            if entry.get("video"):
                info = video_info(entry["video"][0])
                if info is not None and 0 <= int(entry["video"][1]) < info.frame_count:
                    self.video_frame_by_image[image_id] = (info, int(entry["video"][1]))
            if entry.get("touched"):
                self.touched_image_ids.add(image_id)
            if entry.get("persisted"):
                persisted.add(image_id)
        self.persisted_image_ids = persisted
        settled = self._settle_verified_states(list(self.image_by_id), credit_nobody=True)  # the Verified rule
        # What is unsaved is worked out from the data itself, not only the backup's marks: every image that
        # differs from the last save (boxes, state, Background), and every image new since then with work.
        self._ensure_saved_cache()
        for image_id in self.image_by_id:
            key = image_key(self.image_by_id[image_id]["file_name"])
            if key in self._saved_states:
                if not self._matches_saved(image_id):
                    self.touched_image_ids.add(image_id)
            elif self._has_work_or_verified(image_id):
                self.touched_image_ids.add(image_id)
        if self.video_frame_by_image:
            # The image shown while loading may be a frame whose video was only known now; reload it.
            self._clear_scene()
            self._dataset_generation += 1
            self._load_workers.clear()
        self._refresh_video_list()
        self._rebuild_frame_list(show_progress=True)
        self.dirty = True
        annotator = self._choose_annotator(self.annotation_data)
        if annotator is not None:
            self._activate_annotator(annotator)
        self.status_label.setText(
            "Restored unsaved work from autosave — save the project to keep it."
            + (f"\nOn load: set {settled:,} Verified image(s) with an unverified box to Review." if settled else ""))

    def _confirm_dataset_change(self) -> bool:
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save annotation changes before opening another dataset?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            self.save_project()
            return not self.dirty
        self._discard_autosave()
        return True

    def _empty_annotation_data(self) -> dict[str, Any]:
        return {
            "info": {"description": "Created with Stingray Labeler"},
            "licenses": [], "images": [], "annotations": [], "categories": [],
            "annotators": [],
        }

    def new_project(self) -> None:
        if not self._confirm_dataset_change():
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose image folder for the new project", str(self.root or Path.home())
        )
        if not folder:
            return
        root = Path(folder).resolve()
        snapshot = self._offer_autosave(root)
        if snapshot is not None:
            self._restore_autosave(root, snapshot)
            return
        paths = self._scan_image_folder(root)
        if paths is None:
            return
        annotation_data = self._empty_annotation_data()
        self.current_annotator = None
        self._set_dataset(root, annotation_data)
        added, duplicates = self._add_source_images(paths, refresh=False)
        self._rebuild_frame_list(show_progress=True)
        annotator = self._choose_annotator(self.annotation_data)
        if annotator is None:
            self.status_label.setText(
                "Project loaded. Choose a user to enable annotation edits."
            )
            return
        self._activate_annotator(annotator)
        if duplicates:
            self._show_duplicate_summary(duplicates, added)
        self.status_label.setText(
            f"New project: {added:,} candidate image(s) scanned. Save Project packages reviewed work only."
        )

    def open_project(self) -> None:
        if not self._confirm_dataset_change():
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Choose image folder", str(self.root or Path.home())
        )
        if not folder:
            return
        root = Path(folder).resolve()
        annotation_folder = root.parent
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose annotations JSON (Cancel to start a new dataset)",
            str(annotation_folder),
            "JSON files (*.json)",
        )
        annotations_file = Path(selected).resolve() if selected else None
        if annotations_file is not None:
            try:
                snapshot = self._offer_autosave(annotations_file, annotations_file.stat().st_mtime)
                if snapshot is not None:
                    self._restore_autosave(root, snapshot)
                    return
                signature = self._file_signature_of(annotations_file)  # taken before reading
                annotation_data = self._read_annotation_data(annotations_file)
                self._fill_missing_frame_state(annotation_data)
                self.current_annotator = None
                self._set_dataset(root, annotation_data, annotations_file)
                self._file_signature = signature
                self._base_json = self._last_read_base
                if self._last_read_index is not None:
                    self._set_saved_cache(None, self._base_json, self._last_read_index)
                annotator = self._choose_annotator(self.annotation_data)
                loaded_notes = "; ".join(self._load_notes)
                version_note = self._add_import_version(annotations_file, annotator)
                if annotator is None:
                    self.status_label.setText(
                        "Project loaded. Choose a user to enable annotation edits."
                        + (f"\nOn load: {loaded_notes}." if loaded_notes else "")
                        + (f"\n{version_note}" if version_note else "")
                    )
                    return
                self._activate_annotator(annotator)
                if loaded_notes or version_note:
                    self.status_label.setText("\n".join(
                        line for line in (f"On load: {loaded_notes}." if loaded_notes else "", version_note) if line))
            except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as error:
                QMessageBox.critical(self, "Cannot open project", str(error))
            return
        answer = QMessageBox.question(
            self,
            "Start a new dataset?",
            "No annotations JSON was selected. Scan this folder to start a new dataset?\n\n"
            "This lists image filenames only; no image pixels or annotations will be loaded.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        snapshot = self._offer_autosave(root)
        if snapshot is not None:
            self._restore_autosave(root, snapshot)
            return
        paths = self._scan_image_folder(root)
        if paths is None:
            return
        annotation_data = self._empty_annotation_data()
        self.current_annotator = None
        self._set_dataset(root, annotation_data)
        added, duplicates = self._add_source_images(paths, refresh=False)
        self._rebuild_frame_list(show_progress=True)
        annotator = self._choose_annotator(self.annotation_data)
        if annotator is None:
            self.status_label.setText(
                "Project loaded. Choose a user to enable annotation edits."
            )
            return
        self._activate_annotator(annotator)
        if duplicates:
            self._show_duplicate_summary(duplicates, added)
        self.status_label.setText(
            f"Scanned project: {added:,} candidate image(s). Save Project packages reviewed work only."
        )

    def add_images(self) -> None:
        if self.root is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add images to project", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp)",
        )
        if not paths:
            return
        added, duplicates = self._add_source_images(
            [Path(path).resolve() for path in paths]
        )
        if duplicates:
            self._show_duplicate_summary(duplicates, added)

    def add_folder(self) -> None:
        if self.root is None:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Scan image folder", str(Path.home())
        )
        if not folder:
            return
        paths = self._scan_image_folder(Path(folder).resolve())
        if paths is None:
            return
        added, duplicates = self._add_source_images(paths)
        if duplicates:
            self._show_duplicate_summary(duplicates, added)

    def _add_source_images(
        self, paths: list[Path], *, refresh: bool = True,
    ) -> tuple[int, list[tuple[Path, Path]]]:
        existing_by_name = {}
        for image in self.images:
            existing_by_name.setdefault(Path(image["file_name"]).name.casefold(), image)
        duplicates = []
        added = switched = 0
        batch_sources: dict[str, Path] = {}  # names taken from this batch, to catch the same name twice in it
        used_ids = {image["id"] for image in self.images}
        numeric_ids = [int(value) for value in used_ids if str(value).isdigit()]
        next_id = max(max(numeric_ids, default=0) + 1, self._next_image_id)
        for source in (Path(path) for path in paths):
            if source.suffix.lower() not in {
                ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
            }:
                continue
            key = source.name.casefold()
            if key in batch_sources:
                if path_key(batch_sources[key]) != path_key(source):
                    duplicates.append((source, batch_sources[key]))
                continue
            batch_sources[key] = source
            existing_image = existing_by_name.get(key)
            if existing_image is not None:
                # Already in the project: the boxes stay, and the image now opens from the source just added.
                image_id = existing_image["id"]
                current_source = self.source_paths_by_image.get(image_id)
                if (current_source is None or path_key(current_source) != path_key(source)
                        or image_id in self.video_frame_by_image):
                    self.source_paths_by_image[image_id] = source
                    self.video_frame_by_image.pop(image_id, None)
                    self._folder_settled.discard(image_id)
                    switched += 1
                continue
            while next_id in used_ids:
                next_id += 1
            image = {
                "id": next_id, "file_name": source.name, "width": 0, "height": 0,
                "frame_state": "unverified", "background": False,
            }
            self.images.append(image)
            self.image_by_id[next_id] = image
            self.source_paths_by_image[next_id] = source
            self.annotations_by_image[next_id] = []
            self.frame_verified_by_image[next_id] = False
            self.review_by_image[next_id] = False
            self.background_by_image[next_id] = False
            self.roi_counts_by_image[next_id] = {}
            used_ids.add(next_id)
            existing_by_name[source.name.casefold()] = image
            next_id += 1
            added += 1
        self._next_image_id = max(self._next_image_id, next_id)
        if added:
            if refresh:
                self._rebuild_frame_list(show_progress=True)
        if added or switched:
            self.status_label.setText(
                f"Added {added:,} image(s)"
                + (f"; {switched:,} already in the project now open from the added source" if switched else "")
                + ". Source files remain in place until Save Project."
            )
        return added, duplicates

    def _show_duplicate_summary(self, duplicates: list[tuple[Path, Path]], added: int) -> None:
        details = "\n\n".join(
            f"Filename: {source.name}\nNew source: {source}\nAlready in project: {existing}"
            for source, existing in duplicates
        )
        message = QMessageBox(self)
        message.setWindowTitle("Duplicate image names")
        message.setIcon(QMessageBox.Icon.Warning)
        message.setText(f"Added {added:,} image(s); found {len(duplicates):,} duplicate filename(s).")
        message.setInformativeText(
            "The same name came from two different paths; only the first was used. Review the two paths; "
            "image names are expected to be unique in a dataset."
        )
        message.setDetailedText(details)
        copy_button = message.addButton("Copy paths", QMessageBox.ButtonRole.ActionRole)
        save_button = message.addButton("Save list…", QMessageBox.ButtonRole.ActionRole)
        message.exec()
        if message.clickedButton() == copy_button:
            QApplication.clipboard().setText(details)
        elif message.clickedButton() == save_button:
            report_path, _ = QFileDialog.getSaveFileName(
                self, "Save duplicate image list", "duplicate_images.txt", "Text files (*.txt)"
            )
            if report_path:
                try:
                    Path(report_path).write_text(details + "\n", encoding="utf-8")
                except OSError as error:
                    QMessageBox.critical(self, "Could not save duplicate list", str(error))

    def _fill_missing_frame_state(self, annotation_data: dict[str, Any]) -> None:
        """Give every image a frame_state: one without (a plain COCO file has none) is Unverified, as a merge
        reads it too. Set many images at once with Data → Set State of Listed Images."""
        images = annotation_data["images"]
        unknown = [image for image in images
                   if "frame_state" in image and image["frame_state"] not in FRAME_STATES]
        if unknown:
            raise ValueError(
                f"{len(unknown):,} image(s) have an unknown frame_state "
                f"(expected {', '.join(FRAME_STATES)}), e.g. {unknown[0]['file_name']!r}"
            )
        missing = [image for image in images if "frame_state" not in image]
        for image in missing:
            image["frame_state"] = "unverified"
        if missing:
            self._load_notes.append(f"{len(missing):,} image(s) had no image state and are Unverified")

    def _read_annotation_data(self, path: Path) -> dict[str, Any]:
        size = path.stat().st_size

        def read(report, cancelled: threading.Event) -> Any:
            contents = bytearray()
            with path.open("rb") as stream:
                while chunk := stream.read(4 * 1024 * 1024):
                    if cancelled.is_set():
                        raise OperationCancelled()
                    contents.extend(chunk)
                    report(len(contents))
            report(size, f"Parsing {path.name}…")
            # The file as opened, compressed, is the common starting point for a later merge; it is
            # indexed here too (before anything changes the data), so it is never parsed again.
            data = json.loads(contents)
            prepared = self._saved_index(data) if isinstance(data, dict) else None
            return data, zlib.compress(bytes(contents), 1), prepared

        try:
            annotation_data, self._last_read_base, self._last_read_index = self._run_in_background(
                "Loading annotations", f"Reading {path.name}…", max(1, size), read,
                abandon_on_cancel=True,
            )
        except OperationCancelled:
            raise ValueError("Loading annotations was cancelled") from None
        if not isinstance(annotation_data, dict) or not isinstance(annotation_data.get("images"), list):
            raise ValueError("Annotation JSON must contain an images array")
        for field in ("annotations", "categories"):
            if field not in annotation_data:
                annotation_data[field] = []
            elif not isinstance(annotation_data[field], list):
                raise ValueError(f"The {field} field must be a JSON array")
        if annotation_data.get("annotators") is None:
            annotation_data["annotators"] = []
        elif not isinstance(annotation_data.get("annotators", []), list):
            raise ValueError("User metadata must be a JSON array")
        annotator_ids = set()
        for annotator in annotation_data["annotators"]:
            if (
                not isinstance(annotator, dict)
                or "id" not in annotator
                or not isinstance(annotator.get("name"), str)
            ):
                raise ValueError("Each user must have an id and a non-empty name")
            annotator["name"] = annotator["name"].strip()
            if not annotator["name"]:
                raise ValueError("Each user must have an id and a non-empty name")
            annotator["id"] = self._user_key(annotator["id"])
            if not isinstance(annotator["id"], (int, str)) or isinstance(annotator["id"], bool) or (
                    isinstance(annotator["id"], str) and not annotator["id"].strip()):
                raise ValueError("Each user id must be a number or a non-empty text")
            if annotator["id"] in annotator_ids:
                raise ValueError("User IDs must be unique")
            annotator_ids.add(annotator["id"])
        for image in annotation_data["images"]:
            if not isinstance(image, dict) or not isinstance(image.get("file_name"), str):
                raise ValueError("Each image entry must have a file_name")
            image["file_name"] = image["file_name"].replace("\\", "/")
        self._load_notes = self._repair_annotation_data(annotation_data)
        return annotation_data

    @staticmethod
    def _repair_annotation_data(annotation_data: dict[str, Any]) -> list[str]:
        """Bring a standard COCO file up to this app's conventions; return what changed."""
        notes = []
        annotations = [a for a in annotation_data["annotations"] if isinstance(a, dict)]

        # Boxes may reference categories the file does not list; add them so nothing is lost.
        categories = annotation_data["categories"]
        category_ids = {category.get("id") for category in categories if isinstance(category, dict)}
        names = {str(category.get("name", "")).casefold() for category in categories if isinstance(category, dict)}
        added = []
        for annotation in annotations:
            category_id = annotation.get("category_id")
            if category_id is None or category_id in category_ids:
                continue
            name = f"category {category_id}"
            while name.casefold() in names:
                name += "_"
            categories.append({"id": category_id, "name": name})
            category_ids.add(category_id)
            names.add(name.casefold())
            added.append(name)
        if added:
            notes.append(f"added {len(added):,} missing categor{'y' if len(added) == 1 else 'ies'}: "
                         + ", ".join(added[:5]) + ("…" if len(added) > 5 else ""))

        # A box with a model score and no verification flag is model output nobody has reviewed yet;
        # without a score, a missing flag still means verified (manual COCO).
        unreviewed = 0
        for annotation in annotations:
            if "score" in annotation and "verified" not in annotation:
                annotation["verified"] = False
                unreviewed += 1
        if unreviewed:
            notes.append(f"loaded {unreviewed:,} box{'' if unreviewed == 1 else 'es'} with a model score as unverified")
        return notes

    def _scan_image_folder(self, root: Path) -> list[Path] | None:
        extensions = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

        def scan(report, cancelled: threading.Event) -> list[Path]:
            paths = []
            for directory, _subdirectories, filenames in os.walk(root):
                if cancelled.is_set():
                    raise OperationCancelled()
                for filename in filenames:
                    if os.path.splitext(filename)[1].lower() in extensions:
                        paths.append(Path(directory) / filename)
                report(0, f"Scanning image folder…  {len(paths):,} images found")
            return sorted(paths, key=lambda path: path.relative_to(root).as_posix().casefold())

        try:
            return self._run_in_background(
                "Loading images", "Scanning image folder…", 0, scan, abandon_on_cancel=True
            )
        except OperationCancelled:
            return None

    def _source_image_path(self, image: dict[str, Any]) -> Path | None:
        image_id = image["id"]
        path = self.source_paths_by_image.get(image_id)
        if path is not None:
            return path
        if self.root is None:
            return None
        try:
            path = dataset_image_path(self.root, image["file_name"])
        except ValueError:
            return None
        self.source_paths_by_image[image_id] = path
        return path

    def _image_source(self, image: dict[str, Any]) -> Path | VideoFrameRef | None:
        """Where an image's pixels come from: its file, or a frame of a video not yet saved."""
        video_frame = self._video_source(image["id"])
        if video_frame is not None:
            return VideoFrameRef(*video_frame)
        return self._source_image_path(image)

    def _fill_image_size(self, image: dict[str, Any]) -> None:
        """Width and height of an image never shown, read from its file header (no pixels), upright as shown."""
        if image.get("width") and image.get("height"):
            return
        source = self._image_source(image)
        if isinstance(source, VideoFrameRef):
            width, height = source.video.width, source.video.height
        elif source is None:
            return
        else:
            try:
                with Image.open(source) as opened:
                    width, height = opened.size
                    if opened.getexif().get(0x0112) in (5, 6, 7, 8):  # EXIF orientation: a quarter turn
                        width, height = height, width
            except OSError:
                return
        image["width"], image["height"] = width, height

    def _choose_json_save_path(self, title: str, suggested: Path) -> Path | None:
        """Ask for a JSON file location; the dialog confirms before replacing a file."""
        dialog = QFileDialog(self, title, str(suggested))
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilter("JSON files (*.json)")
        dialog.setDefaultSuffix("json")
        dialog.selectFile(suggested.name)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selectedFiles():
            return None
        return Path(dialog.selectedFiles()[0]).resolve()

    def _run_in_background(self, title: str, label: str, total: int, work,
                           *, abandon_on_cancel: bool = False):
        """Run ``work(report, cancelled)`` on a thread behind a modal progress dialog.

        The window keeps repainting while the task runs. ``report(done, text)``
        updates the dialog; Cancel sets ``cancelled`` and ``work`` decides how to
        stop. With ``abandon_on_cancel`` the call returns at once on Cancel and
        the thread's result is discarded (for read-only work such as parsing).
        """
        state = {"done": 0, "label": label, "finished": False, "result": None, "error": None}
        cancelled = threading.Event()

        def report(done: int, text: str | None = None) -> None:
            state["done"] = done
            if text:
                state["label"] = text

        def target() -> None:
            try:
                state["result"] = work(report, cancelled)
            except BaseException as error:  # noqa: BLE001 - re-raised on the UI thread
                state["error"] = error
            finally:
                state["finished"] = True

        progress = QProgressDialog(label, "Cancel", 0, max(0, total), self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        loop = QEventLoop(self)

        def poll() -> None:
            if total:
                progress.setValue(min(state["done"], total))
            progress.setLabelText(state["label"])
            if progress.wasCanceled() and not cancelled.is_set():
                cancelled.set()
                progress.setLabelText("Cancelling…")
            if state["finished"] or (abandon_on_cancel and cancelled.is_set()):
                loop.quit()

        timer = QTimer(self)
        timer.setInterval(50)
        timer.timeout.connect(poll)
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        timer.start()
        if not state["finished"]:
            loop.exec()
        timer.stop()
        progress.close()
        if abandon_on_cancel and cancelled.is_set() and not state["finished"]:
            raise OperationCancelled()
        thread.join()
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    @staticmethod
    def _copy_then_write_json(copies: list[tuple[Path | VideoFrameRef, Path, bool]], data: dict[str, Any],
                              save_path: Path, report, cancelled: threading.Event,
                              unchanged=None, indented: bool = False) -> tuple[bytes, tuple[int, int] | None]:
        """Copy images (video frames are written as PNG), then write the JSON; return it compressed.

        The JSON is compact (one line, written fast) unless ``indented``: the project file is the app's
        own; the training export, read by other tools and people, is indented.

        Each copy is ``(source, target, overwrite)``. A file being overwritten is first written beside
        it and only swapped in once every copy and the JSON are ready, so a failure or Cancel before
        then removes only files written here and never touches what was already in the folder.
        ``unchanged`` (optional) is checked just before swapping in: if it says the project file was
        changed meanwhile (another PC saved), nothing is swapped and FileChangedDuringSave is raised.
        Temporary files carry a name of their own, so two PCs saving at once never share one.
        Returns the JSON written (compressed) and the file's (time, size) right after writing.
        """
        token = os.urandom(4).hex()
        created: list[Path] = []
        staged: list[tuple[Path, Path]] = []
        try:
            for index, (source, target, overwrite) in enumerate(copies, start=1):
                if cancelled.is_set():
                    raise OperationCancelled()
                if target.exists() and not overwrite:
                    raise OSError(f"{target} already exists and was not overwritten.")
                report(index - 1, f"Copying {source.name} ({index:,} of {len(copies):,})…")
                destination = target.with_name(f"{target.name}.{token}.stingray-new") if overwrite else target
                created.append(destination)
                if overwrite:
                    staged.append((destination, target))
                if isinstance(source, VideoFrameRef):
                    try:
                        source.save_png(destination)
                    except (ValueError, IndexError) as error:
                        raise OSError(f"Could not read {source}: {error}") from error
                else:
                    shutil.copy2(source, destination)
            if cancelled.is_set():
                raise OperationCancelled()
            report(len(copies), f"Writing {save_path.name}…")
            text = (json.dumps(data, indent=2) if indented else json.dumps(data, separators=(",", ":"))) + "\n"
            temporary_path = save_path.with_name(f"{save_path.name}.{token}.tmp")
            written = text.encode("utf-8")
            temporary_path.write_bytes(written)  # exactly these bytes (no line-ending conversion), as kept in memory
        except BaseException:
            for path in created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        # Swap in: each file being overwritten is moved aside first, so a failure part way (a file locked
        # by a viewer or a sync client) puts every original back and removes what this save wrote.
        backups: list[tuple[Path, Path]] = []
        try:
            if unchanged is not None and not unchanged():
                raise FileChangedDuringSave(
                    f"{save_path.name} was saved by someone else while this save was running.")
            for staged_path, target in staged:
                backup = target.with_name(f"{target.name}.{token}.stingray-old")
                target.replace(backup)
                backups.append((backup, target))
                staged_path.replace(target)
            temporary_path.replace(save_path)
        except OSError as error:
            not_restored = []
            for backup, target in reversed(backups):
                try:
                    backup.replace(target)
                except OSError:
                    not_restored.append(f"{target.name} (kept as {backup.name})")
            for path in (*created, temporary_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            if not_restored:
                raise OSError(f"{error}\n\nThese originals could not be put back: " + ", ".join(not_restored)) from error
            raise
        for backup, _target in backups:
            try:
                backup.unlink()
            except OSError:
                pass  # the save is complete; a leftover backup copy is harmless
        try:
            stat = save_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        if signature is not None and signature[1] != len(written):
            signature = None  # another write landed right after ours: the next save will offer Merge
        return zlib.compress(written, 1), signature  # what was written, kept as the next merge base

    @staticmethod
    def _file_signature_of(path: Path | None) -> tuple[int, int] | None:
        try:
            stat = path.stat() if path is not None else None
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size) if stat else None

    SAVE_LOCK_SECONDS = 600

    def _take_save_lock(self, save_path: Path):
        """Mark that this PC is saving the project file. Returns the lock path, None if no lock could be
        written (saving goes on), or False if another PC is saving it right now (the user was told)."""
        lock = save_path.with_name(save_path.name + ".lock")
        # HOSTNAME is usually not set for a desktop app on Linux; the socket name always is.
        me = os.environ.get("COMPUTERNAME") or socket.gethostname() or "this PC"
        try:
            other = json.loads(lock.read_text(encoding="utf-8"))
            if time.time() - float(other.get("time", 0)) < self.SAVE_LOCK_SECONDS and other.get("pc") != me:
                QMessageBox.information(
                    self, "Project is being saved",
                    f"{other.get('user') or 'Someone'} on {other.get('pc') or 'another PC'} is saving "
                    f"{save_path.name} right now. Try again in a moment.",
                )
                return False
        except (OSError, ValueError, TypeError):
            pass  # no lock, or an unreadable one
        try:
            lock.write_text(json.dumps({"pc": me, "user": self.current_annotator, "time": time.time()}),
                            encoding="utf-8")
            self._held_lock = lock
            return lock
        except OSError:
            return None

    def _release_save_lock(self) -> None:
        lock, self._held_lock = getattr(self, "_held_lock", None), None
        if lock is not None:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass

    def _changed_by_someone_else(self, path: Path) -> bool:
        """True when the project file on disk is no longer the version this session opened or saved."""
        if self._file_signature is None or not path.exists():
            return False
        return self._file_signature_of(path) != self._file_signature

    def save_project(self) -> bool:
        return self._save_with_status(self.annotations_path)

    def save_project_as(self) -> bool:
        return self._save_with_status(None)

    def _save_with_status(self, save_path: Path | None) -> bool:
        """Save, saying so in the status line at once (painted now, before the save's work starts)."""
        if self.root is None or (save_path is not None and not self.dirty):
            return self._save_project(save_path)
        previous = self.status_label.text()
        saving = f"Saving {save_path.name if save_path else 'project'}…"
        self.status_label.setText(saving)
        self.status_label.repaint()
        saved = self._save_project(save_path)
        if not saved and self.status_label.text() == saving:
            self.status_label.setText(previous)  # cancelled before anything was written
        return saved

    def _save_first(self, what: str) -> bool:
        """``what`` works on saved history, so unsaved changes are saved first (the user is asked)."""
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Save first", f"{what} works on saved work, and you have unsaved changes. Save them first?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save,
        )
        return answer == QMessageBox.StandardButton.Save and self.save_project()

    def _save_project(self, save_path: Path | None) -> bool:
        if self.root is None:
            QMessageBox.information(self, "No project", "Create or open a project folder first.")
            return False
        if save_path is not None and not self.dirty:
            return True
        self._commit_current_scene()
        # Verified and Review images are saved; an Unverified one too when it holds anything: boxes (e.g. from an
        # opened file), a favorite mark, box history or a saved state. Only an Unverified image with none of these
        # (one only scanned from a folder) stays out: it can always be added again.
        self._ensure_saved_cache()
        history_images = self._history_index()[0]
        export_images = [
            image for image in self.images
            if self._frame_state(image["id"]) != "unverified" or image["id"] in history_images
            or image_key(image["file_name"]) in self._saved_state_keys
            or image.get("favorite")  # a favorite mark is work too, whatever the image's state
            or self.annotations_by_image.get(image["id"])  # boxes, e.g. from an opened file: kept as Unverified
        ]
        if not export_images and not self.dirty:
            QMessageBox.information(
                self, "Nothing to save yet",
                "No images are marked Verified or Review. Unverified images stay out of the project "
                "JSON; editing an image marks it Review.",
            )
            return False
        export_ids = {image["id"] for image in export_images}
        export_annotations = [
            annotation for image_id in export_ids
            for annotation in self.annotations_by_image[image_id]
        ]
        if any(  # only boxes changed since the save can have become invalid
            not all(math.isfinite(float(value)) for value in annotation["bbox"])
            for image_id in export_ids & self.touched_image_ids for annotation in self.annotations_by_image[image_id]
        ):
            QMessageBox.critical(self, "Invalid box", "A box contains non-finite coordinates; nothing was saved.")
            return False
        merge_with_disk = False
        if (save_path is not None and self.annotations_path is not None
                and path_key(save_path) == path_key(self.annotations_path)
                and self._changed_by_someone_else(save_path)):
            message = QMessageBox(self)
            message.setIcon(QMessageBox.Icon.Warning)
            message.setWindowTitle("Project file changed on disk")
            message.setText(f"{save_path.name} was saved by someone else after you opened it.")
            message.setInformativeText(
                "Merge combines their changes with yours, then saves. Save As keeps your work in a "
                "separate file instead. Saving over the file without merging would overwrite their work."
            )
            merge_button = message.addButton("Merge…", QMessageBox.ButtonRole.AcceptRole)
            save_as_button = message.addButton("Save As…", QMessageBox.ButtonRole.AcceptRole)
            message.addButton(QMessageBox.StandardButton.Cancel)
            message.setDefaultButton(merge_button)
            message.exec()
            if message.clickedButton() is merge_button:
                merge_with_disk = True
            elif message.clickedButton() is save_as_button:
                save_path = None
            else:
                return False
        if save_path is None:
            suggested = self.annotations_path or self.root.parent / f"{self.root.name}.json"
            if self.annotations_path is not None and self._changed_by_someone_else(self.annotations_path):
                suggested = suggested.with_name(f"{suggested.stem} ({self.current_annotator or 'copy'}).json")
            save_path = self._choose_json_save_path("Save project as", suggested)
            if save_path is None:
                return False
            if (not merge_with_disk and self.annotations_path is not None
                    and path_key(save_path) == path_key(self.annotations_path)
                    and self._changed_by_someone_else(save_path)):
                QMessageBox.warning(
                    self, "Project file changed on disk",
                    f"{save_path.name} was saved by someone else after you opened it, so it was not "
                    "overwritten. Choose another name, or use Save and choose Merge… to combine your work "
                    "with theirs.",
                )
                return False

        # Plan every image first; nothing on disk or in memory changes until all checks pass.
        # Each plan entry is (image, target, action): None (already the folder file), "copy" or "existing".
        plan: list[tuple[dict[str, Any], Path, str | None]] = []
        planned_names: set[str] = set()
        conflicts: list[tuple[Path, Path]] = []
        missing: list[Path] = []
        root_key = path_key(self.root)
        folder_keys: dict[str, str] = {}  # each folder resolved once, not each image
        for image in export_images:
            source = self._image_source(image)
            if source is None:
                missing.append(Path(image["file_name"]))
                continue
            self._fill_image_size(image)
            target_name = image["file_name"].replace("\\", "/").rsplit("/", 1)[-1]
            target = self.root / target_name
            key = target_name.casefold()
            same_path = False
            if isinstance(source, Path) and os.path.normcase(source.name) == os.path.normcase(target_name):
                parent = str(source.parent)
                if parent not in folder_keys:
                    folder_keys[parent] = path_key(source.parent)
                same_path = folder_keys[parent] == root_key
            if key in planned_names:
                conflicts.append((source, target))  # two project images with one name
                continue
            planned_names.add(key)
            # Only files that may be missing are looked for, one by one (no listing of the whole folder):
            # an image the project already holds opens from its folder file.
            if same_path:
                if image["id"] not in self.persisted_image_ids and not target.exists():
                    missing.append(source)
                else:
                    plan.append((image, target, None))
                continue
            if not target.exists():
                plan.append((image, target, "copy"))
            elif image["id"] in self._folder_settled:
                plan.append((image, target, None))  # already copied, skipped or overwritten this session
            else:
                plan.append((image, target, "existing"))

        if conflicts:
            self._show_duplicate_summary(conflicts, 0)
            return False
        if missing:
            QMessageBox.warning(
                self, "Images not found in project folder",
                "These image paths were not present in the project image folder and could not be packaged:\n\n"
                + "\n".join(str(path) for path in missing[:20])
                + "\n\nAdd the folder or video they come from (File → Add Image Folder… / Add Video Folder…) "
                "and save again.",
            )
            return False
        # A file already in the folder is skipped or overwritten as the user chooses; never deleted.
        overwrite_ids: set[Any] = set()
        existing = [(image, target) for image, target, action in plan if action == "existing"]
        choice_for_rest: str | None = None
        for position, (image, target) in enumerate(existing, start=1):
            choice = choice_for_rest
            if choice is None:
                message = QMessageBox(self)
                message.setWindowTitle("Image already in the project folder")
                message.setIcon(QMessageBox.Icon.Question)
                message.setText(f"{target.name} already exists in the project folder "
                                f"({position:,} of {len(existing):,}).")
                message.setInformativeText(
                    f"Skip keeps the file in the folder. Overwrite replaces it with the image from "
                    f"{self._image_source(image)}. No file is deleted either way."
                )
                buttons = {
                    message.addButton("Skip", QMessageBox.ButtonRole.AcceptRole): ("skip", None),
                    message.addButton("Overwrite", QMessageBox.ButtonRole.AcceptRole): ("overwrite", None),
                    message.addButton("Skip all", QMessageBox.ButtonRole.AcceptRole): ("skip", "skip"),
                    message.addButton("Overwrite all", QMessageBox.ButtonRole.AcceptRole):
                        ("overwrite", "overwrite"),
                }
                message.addButton(QMessageBox.StandardButton.Cancel)
                message.exec()
                picked = buttons.get(message.clickedButton())
                if picked is None:
                    self.status_label.setText("Save cancelled; no project files were changed.")
                    return False
                choice, choice_for_rest = picked
            if choice == "overwrite":
                overwrite_ids.add(image["id"])

        records = []
        exported_ids = set()
        for image, target, _action in plan:
            record = dict(image)
            record["file_name"] = target.name
            record["frame_state"] = self._frame_state(image["id"])
            record["background"] = self.background_by_image.get(image["id"], False)
            records.append(record)
            exported_ids.add(image["id"])
        packaged_annotations = [
            annotation for annotation in export_annotations
            if annotation["image_id"] in exported_ids
        ]
        # Versions are formed against the file as last saved, on copies, so a failed save changes nothing.
        for annotation in packaged_annotations:
            if not is_box_id(annotation.get("box_id")):
                annotation["box_id"] = new_box_id()
        self._ensure_saved_cache()  # the last saved file, indexed once (not parsed again here)
        saved_annotations = [dict(annotation) for annotation in packaged_annotations]
        current_history = self.annotation_data.get("annotation_history") or []
        saver_id = self._annotator_id(self.current_annotator) if self.current_annotator else None
        current_states = self.annotation_data.get("state_history") or []
        now = self._next_time()
        # Only edited images, and images new to the file, can hold boxes that differ from their saved version.
        changed_ids = set(self.touched_image_ids) | {
            image_id for image_id in exported_ids if image_id not in self.persisted_image_ids}

        def versions(_report, _cancelled):
            box_history = record_versions(
                None, records, saved_annotations, current_history, saver_id, now,
                purged=self._purged_boxes, user_remap=self._user_remap, deleted_by=self._deleted_by,
                saved_index=self._saved_cache, changed=changed_ids,
            )
            state_history = record_states(self._saved_states, records, current_states, saver_id, now,
                                          self._state_by, self._user_remap)
            return box_history, state_history

        # Versions are formed on another thread; all inputs are copies.
        history, state_history = self._run_in_background("Saving project", "Recording versions…", 0, versions)
        state_fields = {record["id"]: (record.get("state_time"), record.get("state_by")) for record in records}
        project_data = dict(self.annotation_data)
        project_data["state_history"] = state_history
        project_data["images"] = records
        project_data["annotations"] = saved_annotations
        project_data["annotation_history"] = history
        merged_project = None
        disk_signature = None
        if merge_with_disk:
            # Only saved work is merged: this save's versions of our work, and the file on disk.
            disk_signature = self._file_signature_of(save_path)  # taken before reading
            try:
                theirs = json.loads(save_path.read_bytes())
            except (OSError, ValueError) as error:
                QMessageBox.critical(self, "Cannot merge", f"Could not read {save_path.name}:\n\n{error}")
                return False
            if isinstance(theirs, dict):
                self._normalize_user_ids(theirs)
            # Unverified images are not written, but their boxes are ours too and must not be overwritten.
            saved_ids = {record["id"] for record in records}
            unverified_ids = {
                image["id"] for image in self.images
                if image["id"] not in saved_ids and self.annotations_by_image.get(image["id"])
            }
            # The merge copies every record it changes, so plain lists are enough here (no deep copy).
            history_by_image = self._history_index()[0]
            mine = dict(
                project_data,
                images=records + [dict(self.image_by_id[image_id], frame_state="unverified")
                                  for image_id in unverified_ids],
                annotations=saved_annotations + [annotation for image_id in unverified_ids
                                                 for annotation in self.annotations_by_image[image_id]],
                annotation_history=history + [record for image_id in unverified_ids
                                              for record in history_by_image.get(image_id, [])],
            )
            merged = self._merge_dialog(mine, theirs, "the version on disk", "Merge and save")
            if merged is None:
                return False
            outcome, mine_map, remember, new_version = merged
            reserved = set(self.image_by_id)
            merged_project, merged_changed, merged_added = self._run_in_background(
                "Merge project", "Combining boxes…", 0,
                lambda _report, _cancelled: build_project(mine, outcome, reserved, mine_map, remember, new_version),
            )
            # Written: what this save writes anyway, images the merge made Review or Verified, and any image
            # with box history (saved work stays saved even when Unverified).
            # Everything either saved copy holds is saved work and is written, whatever its state.
            history_images = {record.get("image_id") for record in merged_project["annotation_history"]}
            theirs_keys = {image_key(image["file_name"]) for image in theirs.get("images", [])
                           if isinstance(image, dict) and isinstance(image.get("file_name"), str)}
            written = {image["id"] for image in merged_project["images"]
                       if image["id"] in saved_ids or legacy_state(image) != "unverified"
                       or image["id"] in history_images or image_key(image["file_name"]) in theirs_keys}
            project_data = dict(
                merged_project,
                images=[image for image in merged_project["images"] if image["id"] in written],
                annotations=[annotation for annotation in merged_project["annotations"]
                             if annotation["image_id"] in written],
                annotation_history=[record for record in merged_project["annotation_history"]
                                    if record.get("image_id") in written],
            )
        copies = [
            (self._image_source(image), target, action == "existing")
            for image, target, action in plan
            if action == "copy" or image["id"] in overwrite_ids
        ]
        # What the project file must still be when this save swaps in its result.
        if merge_with_disk:
            expected = disk_signature
        elif self.annotations_path is not None and path_key(save_path) == path_key(self.annotations_path):
            expected = self._file_signature
        else:
            expected = "absent" if not save_path.exists() else None  # an existing file chosen in Save As

        def unchanged() -> bool:
            if expected is None:
                return True
            if expected == "absent":
                return not save_path.exists()
            return self._file_signature_of(save_path) == expected

        lock = self._take_save_lock(save_path)
        if lock is False:
            return False
        written_data = project_data

        def write(report, cancelled):
            written = self._copy_then_write_json(copies, written_data, save_path, report, cancelled, unchanged)
            # The next "last saved" index, built here too. A plain save wrote copies nothing changes later, so
            # the index shares them; a merged project becomes the session's own data, so it is copied.
            return written, self._saved_index(written_data, copy=merged_project is not None)

        try:
            (saved_base, written_signature), prepared = self._run_in_background(
                "Saving project", f"Saving {save_path.name}…", len(copies) + 1, write,
            )
        except OperationCancelled:
            self.status_label.setText("Save cancelled; no project files were changed.")
            return False
        except FileChangedDuringSave as error:
            QMessageBox.warning(
                self, "Project file changed during save",
                f"{error}\n\nNothing was saved. Save again to merge their work with yours.",
            )
            return False
        except OSError as error:
            QMessageBox.critical(
                self, "Save project failed",
                f"{error}\n\nNothing was saved: files this save wrote were removed, and every file it was "
                "replacing was put back (unless listed above).",
            )
            return False
        finally:
            self._release_save_lock()

        for image, target, action in plan:
            image["file_name"] = target.name
            image["frame_state"] = self._frame_state(image["id"])
            image["background"] = self.background_by_image.get(image["id"], False)
            for name, value in zip(("state_time", "state_by"), state_fields.get(image["id"], (None, None))):
                if value is None:
                    image.pop(name, None)
                else:
                    image[name] = value
            # The source stays as it is: an image keeps opening from the folder or video added this session.
            if action is not None:
                self._folder_settled.add(image["id"])  # don't ask about this file again until its source changes
            if action == "existing" and image["id"] not in overwrite_ids:
                # Skipped: the project names the folder file, so the image opens from it from now on.
                self.source_paths_by_image[image["id"]] = target
                self.video_frame_by_image.pop(image["id"], None)
        given_ids: dict[Any, Any] = {}  # boxes drawn since the last save: temporary id -> id by the rule
        for annotation, saved in zip(packaged_annotations, saved_annotations):
            if saved.get("box_id") != annotation.get("box_id"):
                given_ids[annotation.get("box_id")] = saved.get("box_id")
            for name in ("box_id", "rev", "time", "annotator_id"):
                if name in saved:
                    annotation[name] = saved[name]
                else:
                    annotation.pop(name, None)
        if given_ids:
            for _image_id, state in (*self._undo_history, *self._redo_history):
                for annotation in state:
                    if annotation.get("box_id") in given_ids:
                        annotation["box_id"] = given_ids[annotation["box_id"]]
        self.annotation_data["annotation_history"] = history
        self.annotation_data["state_history"] = state_history
        if merged_project is not None:
            # Written: now the session takes the merged project (changed images, history, users, categories).
            self._adopt_project(merged_project, merged_changed, merged_added)
            records = project_data["images"]
            exported_ids = {image["id"] for image in records}
            packaged_annotations = project_data["annotations"]
        self._purged_boxes.clear()  # gone from the saved file now
        # Files of images deleted permanently go to the Recycle Bin now that the saved file no longer lists them;
        # a name the project lists again (added back, or brought back by a merge) is left alone.
        listed_names = {image["file_name"].replace("\\", "/").rsplit("/", 1)[-1].casefold() for image in self.images}
        not_trashed = []
        for path in sorted(self._trash_on_save):
            if path.name.casefold() in listed_names or not path.exists():
                continue
            moved = QFile.moveToTrash(str(path))
            if not (moved[0] if isinstance(moved, tuple) else moved):
                not_trashed.append(path.name)
        self._trash_on_save.clear()
        self._user_remap.clear()
        self._deleted_by.clear()
        self._state_by.clear()
        self._base_json = saved_base  # the saved file is the new common base
        self._set_saved_cache(None, saved_base, prepared)  # indexed from what was written, on the save's thread
        self._history_unsaved = False
        self._discard_autosave(self.annotations_path or self.root)
        self._discard_autosave(save_path)
        self.output_path = save_path
        self.annotations_path = save_path
        # The file as this save wrote it; unknown if another write landed right after (then Save offers Merge).
        self._file_signature = written_signature if written_signature is not None else (0, -1)
        self.persisted_image_ids = exported_ids
        saved_ids, self.touched_image_ids = self.touched_image_ids, set()
        for image_id in saved_ids:
            self._update_frame_item_text(image_id)
        if self.unsaved_filter_check.isChecked():
            self._apply_frame_filters(select_first=False)
        self.dirty = False
        self.status_label.setText(
            f"Saved {len(records):,} image(s) and {len(packaged_annotations):,} annotation boxes "
            f"to {save_path}"
            + (f"\nCould not move to the Recycle Bin (delete by hand): {', '.join(not_trashed)}" if not_trashed else "")
        )
        self._update_image_info()  # saving changes no counts
        return True

    def _image_path(self, image_id: Any) -> Path | None:
        video_frame = self._video_source(image_id)
        if video_frame is not None:
            return video_frame[0].path
        path = self.source_paths_by_image.get(image_id)
        if path is not None or self.root is None:
            return path
        try:
            return dataset_image_path(self.root, self.image_by_id[image_id]["file_name"])
        except ValueError:
            return None

    def _ensure_saved_cache(self) -> None:
        """Index the boxes of the last saved file (the merge base); redone only when that file changes."""
        if self._saved_cache_source is self._base_json:
            return
        try:
            base = json.loads(zlib.decompress(self._base_json)) if self._base_json else None
        except (ValueError, zlib.error):
            base = None
        self._set_saved_cache(base, self._base_json)

    @classmethod
    def _normalize_user_ids(cls, data: dict[str, Any]) -> None:
        """User ids as one kind of key throughout a file, in place: a number written as text ("3") becomes 3 on
        users, boxes, versions and image states alike, so every record still matches its user."""
        key = cls._user_key
        for user in data.get("annotators") or []:
            if isinstance(user, dict) and "id" in user:
                user["id"] = key(user["id"])
        for name in ("annotations", "annotation_history", "state_history"):
            for record in data.get(name) or []:
                if isinstance(record, dict) and record.get("annotator_id") is not None:
                    record["annotator_id"] = key(record["annotator_id"])
        for image in data.get("images") or []:
            if isinstance(image, dict) and image.get("state_by") is not None:
                image["state_by"] = key(image["state_by"])

    @classmethod
    def _saved_index(cls, base: dict[str, Any] | None, copy: bool = True) -> tuple:
        """The index of a saved project the "*" markers, grey rows and the next save use; it touches nothing
        else, so it can be built on another thread. User ids in ``base`` are normalized first, in place (see
        _normalize_user_ids). With ``copy`` False the index shares ``base``'s boxes, for data nothing changes
        afterwards (what a save just wrote)."""
        if isinstance(base, dict):
            cls._normalize_user_ids(base)
        keys = {
            image_key(image["file_name"]) for image in (base or {}).get("images", [])
            if isinstance(image, dict) and "frame_state" in image and isinstance(image.get("file_name"), str)
        }
        states = {
            image_key(image["file_name"]): {
                "frame_state": legacy_state(image), "background": is_verified(image.get("background", False)),
                "state_time": image.get("state_time"), "state_by": image.get("state_by"),
                "favorite": bool(image.get("favorite")),
            }
            for image in (base or {}).get("images", [])
            if isinstance(image, dict) and isinstance(image.get("file_name"), str)
        }
        return saved_boxes(base, copy), keys, states

    def _set_saved_cache(self, base: dict[str, Any] | None, source: bytes | None, prepared: tuple | None = None) -> None:
        """Index the last saved project (or take an index already built, ``prepared``)."""
        index, keys, states = prepared if prepared is not None else self._saved_index(base)
        self._saved_cache, self._saved_cache_source = index, source
        self._saved_state_keys = keys
        self._saved_states = states
        self._saved_by_image = {}
        for (key, _box_id), box in self._saved_cache[0].items():
            self._saved_by_image.setdefault(key, []).append(box)

    def _saved_version(self, image_id: Any, annotation: dict[str, Any]) -> dict[str, Any] | None:
        """The box as it was last saved, or None if it has not been saved yet."""
        self._ensure_saved_cache()
        key = image_key(self.image_by_id[image_id]["file_name"])
        by_box_id, by_number = self._saved_cache
        return by_box_id.get((key, annotation.get("box_id"))) or by_number.get((key, annotation.get("id")))
