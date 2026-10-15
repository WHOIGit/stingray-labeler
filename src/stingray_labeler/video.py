"""Videos: listing them and stepping through their frames.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QListWidgetItem, QMessageBox

from .avi import AviInfo, AviReader, read_avi_info
from .dataset import OperationCancelled, path_key


class VideoMixin:
    """Adding videos, the video list and frame stepping; frames become images once they have work."""

    def add_videos(self) -> None:
        if self.root is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add videos", str(Path.home()), "AVI video (*.avi)"
        )
        if paths:
            self._add_video_paths([Path(path).resolve() for path in paths])

    def add_video_folder(self) -> None:
        if self.root is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "Scan video folder", str(Path.home()))
        if not folder:
            return
        root = Path(folder).resolve()

        def scan(report, cancelled: threading.Event) -> list[Path]:
            paths = []
            for directory, _subdirectories, filenames in os.walk(root):
                if cancelled.is_set():
                    raise OperationCancelled()
                paths.extend(Path(directory) / name for name in filenames if name.lower().endswith(".avi"))
                report(0, f"Scanning video folder…  {len(paths):,} videos found")
            return paths

        try:
            paths = self._run_in_background(
                "Loading videos", "Scanning video folder…", 0, scan, abandon_on_cancel=True
            )
        except OperationCancelled:
            return
        if not paths:
            QMessageBox.information(self, "No videos found", f"No .avi files were found in {root}.")
            return
        self._add_video_paths(paths)

    def _add_video_paths(self, paths: list[Path]) -> None:
        """List videos by reading only their headers; no frames are read until one is shown."""
        new_paths = [path for path in paths if path_key(path) not in self.videos]

        def read_headers(report, cancelled: threading.Event) -> list[tuple[Path, AviInfo | None, str]]:
            results = []
            for index, path in enumerate(new_paths, start=1):
                if cancelled.is_set():
                    raise OperationCancelled()
                report(index - 1, f"Reading video headers…  {index:,} of {len(new_paths):,}")
                try:
                    results.append((path, read_avi_info(path), ""))
                except (OSError, ValueError) as error:
                    results.append((path, None, str(error)))
            return results

        try:
            results = self._run_in_background(
                "Loading videos", "Reading video headers…", len(new_paths), read_headers,
                abandon_on_cancel=True,
            )
        except OperationCancelled:
            return
        by_stem = {info.path.stem.casefold(): info for info in self.videos.values()}
        added = []
        duplicates: list[tuple[Path, Path]] = []
        failures = []
        for path, info, error in results:
            if info is None:
                failures.append(f"{path}\n    {error}")
            elif info.path.stem.casefold() in by_stem:
                # Frames are named after the video, so two videos with one name would share frame names.
                duplicates.append((path, by_stem[info.path.stem.casefold()].path))
            else:
                self.videos[path_key(info.path)] = info
                by_stem[info.path.stem.casefold()] = info
                added.append(info)
        if added:
            linked = self._link_images_to_videos(added)
            self._refresh_video_list()
            self.status_label.setText(
                f"Added {len(added):,} video(s)"
                + (f"; {linked:,} project image(s) now open from their video" if linked else "")
                + ". Select a video to show its frames; a frame joins the image list once it has work."
            )
        if failures:
            message = QMessageBox(self)
            message.setWindowTitle("Videos not added")
            message.setIcon(QMessageBox.Icon.Warning)
            message.setText(f"{len(failures):,} video(s) could not be read and were not added.")
            message.setInformativeText("Only uncompressed AVI video can be read frame by frame.")
            message.setDetailedText("\n\n".join(failures))
            message.exec()
        if duplicates:
            self._show_duplicate_summary(duplicates, len(added))
        if not added and not failures and not duplicates:
            self.status_label.setText("Those videos are already listed.")

    def _refresh_video_list(self) -> None:
        current = self._active_video
        self.video_list.blockSignals(True)
        self.video_list.clear()
        for key, info in sorted(self.videos.items(), key=lambda pair: self._natural_key(pair[1].path.name)):
            item = QListWidgetItem(info.path.name)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setToolTip(
                f"{info.path}\n{info.frame_count:,} frames (0–{info.frame_count - 1:,}), "
                f"{info.width:,} × {info.height:,} px, {info.fps:.2f} fps"
            )
            self.video_list.addItem(item)
            if current is not None and key == path_key(current.path):
                self.video_list.setCurrentItem(item)
        self.video_list.blockSignals(False)
        self.video_section.setVisible(bool(self.videos))
        self._filter_video_list()
        self._update_navigation_buttons()

    def _filter_video_list(self) -> None:
        """The video search box filters videos by name."""
        text = self.video_search.text().strip().casefold()
        for row in range(self.video_list.count()):
            item = self.video_list.item(row)
            item.setHidden(bool(text) and text not in item.text().casefold())

    # ---------- video frames ----------

    def _video_selection_changed(self, current, _previous) -> None:
        if current is None:
            return
        info = self.videos.get(current.data(Qt.ItemDataRole.UserRole))
        if info is None or info == self._active_video:
            return
        if self._video_reader is not None:
            self._video_reader.close()
        reader = AviReader(info)
        try:
            reader.open()
        except OSError as error:
            self._video_reader = None
            QMessageBox.critical(self, "Cannot open video", f"{info.path}\n\n{error}")
            return
        self._video_reader = reader
        self._active_video = info
        # Arrow keys now step this video's frames, so nothing stays selected in the image list.
        self.frame_list.blockSignals(True)
        self.frame_list.setCurrentRow(-1)
        self.frame_list.clearSelection()
        self.frame_list.blockSignals(False)
        last = info.frame_count - 1
        for control in (self.frame_box, self.frame_slider):
            control.blockSignals(True)
            control.setRange(0, last)
        self.frame_box.setSuffix(f" / {last}")
        self._set_video_controls_shown(True)
        self._show_video_frame(self._video_positions.get(path_key(info.path), 0))
        for control in (self.frame_box, self.frame_slider):
            control.blockSignals(False)

    def _set_video_controls_shown(self, shown: bool) -> None:
        """Frame controls under the image, and Previous/Next moving between videos instead of images."""
        self.video_controls.setVisible(shown)
        self.previous_button.setText("Previous video" if shown else "Previous image")
        self.next_button.setText("Next video" if shown else "Next image")
        self._update_navigation_buttons()

    def _step_video(self, step: int) -> None:
        row = self.video_list.currentRow() + step
        if 0 <= row < self.video_list.count():
            self.video_list.setCurrentRow(row)  # selecting it shows that video's frame

    def _leave_video_mode(self) -> None:
        if self._video_reader is not None:
            self._video_reader.close()
            self._video_reader = None
        if self._active_video is None:
            return
        self._active_video = None
        self.video_list.blockSignals(True)
        self.video_list.setCurrentRow(-1)
        self.video_list.clearSelection()
        self.video_list.blockSignals(False)
        self._set_video_controls_shown(False)

    def _show_video_frame(self, index: int) -> None:
        video = self._active_video
        if video is None:
            return
        index = max(0, min(video.frame_count - 1, index))
        self._video_positions[path_key(video.path)] = index
        for control in (self.frame_box, self.frame_slider):
            blocked = control.blockSignals(True)
            control.setValue(index)
            control.blockSignals(blocked)
        image_id = self._video_frame_image_id(video, index)
        self._load_frame(image_id)
        if image_id == self.current_image_id:  # already on screen, e.g. reselecting this video
            self._update_image_info()

    def _step_video_frame(self, step: int) -> None:
        if self._active_video is not None:
            self._show_video_frame(self.frame_box.value() + step)

    def _video_frame_image_id(self, video: AviInfo, index: int) -> Any:
        """The image entry for a frame: an existing one with the frame's name, or a new one."""
        name = video.frame_name(index)
        key = name.casefold()
        frame = (path_key(video.path), index)
        lookup_key = (id(self.video_frame_by_image), len(self.video_frame_by_image))
        if self._frame_lookup_key != lookup_key or frame in self._frame_lookup and (
                self.video_frame_by_image.get(self._frame_lookup[frame], (None, -1))[1] != index):
            self._frame_lookup = {(path_key(linked.path), linked_index): image_id
                                  for image_id, (linked, linked_index) in self.video_frame_by_image.items()}
            self._frame_lookup_key = lookup_key
        if frame in self._frame_lookup:
            return self._frame_lookup[frame]  # already linked to this frame, whatever its name
        if self._name_lookup_key != len(self.images):
            self._name_lookup = {image["file_name"].rsplit("/", 1)[-1].casefold(): image["id"] for image in self.images}
            self._name_lookup_key = len(self.images)
        found = self._name_lookup.get(key)
        if found in self.image_by_id and self.image_by_id[found]["file_name"].rsplit("/", 1)[-1].casefold() == key:
            return found
        while self._next_image_id in self.image_by_id:
            self._next_image_id += 1
        image_id = self._next_image_id
        self._next_image_id += 1
        image = {
            "id": image_id, "file_name": name, "width": video.width, "height": video.height,
            "frame_state": "unverified", "background": False,
        }
        self.images.append(image)
        self.image_by_id[image_id] = image
        self.annotations_by_image[image_id] = []
        self.frame_verified_by_image[image_id] = False
        self.review_by_image[image_id] = False
        self.background_by_image[image_id] = False
        self.roi_counts_by_image[image_id] = {}
        self.video_frame_by_image[image_id] = (video, index)
        return image_id

    def _link_images_to_videos(self, videos: list[AviInfo]) -> int:
        """Point images named ``<video stem>_<N>.<ext>`` at the frames of newly added videos; nothing is read.

        The most recently added source wins, so these frames now open from the video, not the project folder.
        """
        by_stem = {info.path.stem.casefold(): info for info in videos}
        linked = 0
        for image in self.images:
            name = image["file_name"].rsplit("/", 1)[-1]
            match = re.fullmatch(r"(.+)_(\d+)\.png", name, re.IGNORECASE)
            if match is None:
                continue
            video = by_stem.get(match.group(1).casefold())
            index = int(match.group(2))
            # Only the exact frame name (no padding, .png), so one frame never becomes two images.
            if video is None or index >= video.frame_count or name.casefold() != video.frame_name(index).casefold():
                continue
            self.video_frame_by_image[image["id"]] = (video, index)
            self._folder_settled.discard(image["id"])
            if image["id"] in self._frame_items:
                self._listed_video_frames.add(image["id"])  # it already has a row; keep it listed
            linked += 1
        return linked

    def _video_source(self, image_id: Any) -> tuple[AviInfo, int] | None:
        """The video frame to read for an image, when its latest source is a video."""
        return self.video_frame_by_image.get(image_id)

    def _drop_unused_video_frame(self, image_id: Any) -> None:
        """Forget a frame that was only looked at, so stepping through a video adds nothing."""
        if (image_id not in self.video_frame_by_image or image_id in self._listed_video_frames
                or image_id in self.touched_image_ids or image_id in self.persisted_image_ids
                or image_id in (self.current_image_id, self._pending_image_id)
                or self._has_work_or_verified(image_id)):
            return
        image = self.image_by_id.pop(image_id)
        for index in range(len(self.images) - 1, -1, -1):
            if self.images[index] is image:
                del self.images[index]
                break
        for table in (self.video_frame_by_image, self.annotations_by_image, self.frame_verified_by_image,
                      self.review_by_image,
                      self.background_by_image, self.roi_counts_by_image, self.source_paths_by_image):
            table.pop(image_id, None)

    def _jump_to_video_work(self, step: int) -> None:
        """Move to the next (or previous) frame of the selected video that has work, wrapping."""
        video = self._active_video
        if video is None:
            return
        self._commit_current_scene()
        current = self.frame_box.value()
        pattern = re.compile(re.escape(video.path.stem) + r"_(\d+)\.png", re.IGNORECASE)
        indices = set()
        for image in self.images:
            image_id = image["id"]
            if not self._has_work_or_verified(image_id):
                continue
            video_frame = self.video_frame_by_image.get(image_id)
            if video_frame is not None:
                if video_frame[0] == video:
                    indices.add(video_frame[1])
                continue
            name = image["file_name"].rsplit("/", 1)[-1]
            match = pattern.fullmatch(name)
            if match and name.casefold() == video.frame_name(int(match.group(1))).casefold():
                indices.add(int(match.group(1)))
        others = sorted(indices - {current})
        if not others:
            self.status_label.setText("No other frames with work in this video.")
            return
        if step > 0:
            target = next((index for index in others if index > current), others[0])
        else:
            target = next((index for index in reversed(others) if index < current), others[-1])
        self._show_video_frame(target)

    def _video_key(self, event) -> bool:
        """Fixed frame keys while a video is selected (arrows, Home / End); True when the key was used."""
        key, modifiers = event.key(), event.modifiers()
        if modifiers == Qt.KeyboardModifier.NoModifier:
            steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}
            if key in steps:
                self._step_video_frame(steps[key])
            elif key == Qt.Key.Key_Home:
                self._show_video_frame(0)
            elif key == Qt.Key.Key_End:
                self._show_video_frame(self._active_video.frame_count - 1)
            else:
                return False
            return True
        return False
