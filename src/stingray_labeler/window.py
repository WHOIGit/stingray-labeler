"""Main image annotation window and user workflows."""

from __future__ import annotations

import bisect
import difflib
import csv
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sys
import threading
import time
import zlib
from base64 import b64decode, b64encode
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

try:
    from PySide6.QtCore import QDate, QEvent, QEventLoop, QFile, QKeyCombination, QObject, QPoint, QSettings, QStandardPaths, QPointF, QRectF, QSize, Qt, QRunnable, QThreadPool, QTimer, Signal
    from PySide6.QtGui import QAction, QColor, QBrush, QFont, QIcon, QImage, QKeySequence, QPainter, QPalette, QPen, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QCheckBox,
        QComboBox,
        QColorDialog,
        QDoubleSpinBox,
        QDialog,
        QDateEdit,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QGraphicsItem,
        QGraphicsLineItem,
        QGraphicsPixmapItem,
        QGraphicsRectItem,
        QGraphicsScene,
        QGraphicsView,
        QGridLayout,
        QHBoxLayout,
        QInputDialog,
        QKeySequenceEdit,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMenu,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QProgressDialog,
        QRadioButton,
        QSlider,
        QSpinBox,
        QSplitter,
        QTabWidget,
        QTableWidget,
        QTableWidgetItem,
        QTextBrowser,
        QToolButton,
        QVBoxLayout,
        QWidget,
        QWidgetAction,
    )
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
except ImportError as error:
    raise SystemExit("Install the GUI dependencies with: python -m pip install PySide6 Pillow") from error

from .avi import AviInfo, AviReader, VideoFrameRef, read_avi_info
from .dataset import is_verified, dataset_image_path, directory_names, model_score, path_key
from .history import (
    activity, as_of, canonical_time, changed_images_in, compact_history, compare_versions, is_box_id, new_box_id, normalize_box_ids, record_states,
    box_note, later_time, newest_time, record_versions, same_content, saved_boxes, utc_now,
)
from .merge import (
    MergeResult, NameMap, build_project, image_key, json_copy, legacy_state, merge, merged_away, name_counts,
    shared_name_pairs,
)
from .graphics import (
    AnnotationView, BoxItem, ImageRuler, ScaleBarItem, format_length, scale_bar_lengths_mm,
)
from .image_processing import (
    Adjustments, AutoLevelsWorker, DetailWorker, ImageLoadWorker, LoadedImage, PipelineSignals,
    PreviewAdjustmentWorker, apply_image_adjustments, auto_levels, to_rgb,
)

AUTOSAVE_INTERVAL_MS = 5 * 60 * 1000
FRAME_STATES = ("verified", "review", "unverified")
# Single-key shortcuts, changed in Edit → Keyboard Shortcuts… and kept on this PC: (name, what it does, default).
# Arrows, Home / End, Delete, Esc, Enter and Space stay as they are.
SHORTCUTS = (
    ("previous", "Previous image, or frame when a video is selected", "A"),
    ("next", "Next image, or frame when a video is selected", "D"),
    ("back_10", "Back 10 frames (video)", "Shift+A"),
    ("forward_10", "Forward 10 frames (video)", "Shift+D"),
    ("previous_unverified", "Previous image not Verified, or frame with work (video)", "Q"),
    ("next_unverified", "Next image not Verified, or frame with work (video)", "E"),
    ("previous_box", "Previous box in the image", "W"),
    ("next_box", "Next box in the image", "S"),
    ("draw", "Draw a box", "B"),
    ("verify_box", "Verify / unverify the selected box", "V"),
    ("change_category", "Change the selected box's category", "R"),
    ("delete_box", "Delete the selected box", "X"),
    ("image_verified", "Image: Verified ↔ Review", "Shift+V"),
    ("image_review", "Image: Review ↔ Unverified", "C"),
    ("favorite", "Mark / unmark the image as a favorite", "T"),
    ("auto_levels", "Automatic black/white levels; again for the defaults", "F"),
    ("reset_image", "Reset image settings", "Shift+F"),
    ("go_to_frame", "Type a frame number (video)", "G"),
)
FIXED_KEYS = ("Left", "Right", "Up", "Down", "Home", "End", "Del", "Esc", "Return", "Enter", "Space", "Tab")


class FrameListWidget(QListWidget):
    """Image list: a mouse click never scrolls it; keyboard and Previous/Next selection still follow."""

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # Auto-scroll covers both scroll-to-selection and the drag-near-edge scroll timer.
        self.setAutoScroll(False)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        self.setAutoScroll(True)


class FrameNumberBox(QSpinBox):
    """Frame number entry for typing only; Up/Down and the mouse wheel do not step it."""

    def stepBy(self, _steps: int) -> None:  # noqa: N802
        pass

    def stepEnabled(self):  # noqa: N802
        return QSpinBox.StepEnabledFlag.StepNone


class OperationCancelled(Exception):
    """Raised inside a background task when the user presses Cancel."""


class FileChangedDuringSave(OSError):
    """Raised before anything is swapped in when the project file changed while this save was running."""


class ImageAnnotator(QMainWindow):
    """Browse, filter, edit, and save image annotations."""

    BACKGROUND_FILTER_ID = "__background_filter__"
    ALL_ANNOTATORS_FILTER_ID = "__all_annotators__"
    BACKGROUND_ANNOTATOR_FILTER_ID = "__background_no_annotations__"

    def __init__(self):
        super().__init__()
        self.root: Path | None = None
        self.annotations_path: Path | None = None
        self.output_path: Path | None = None
        self.annotation_data: dict[str, Any] = {"images": [], "annotations": [], "categories": []}
        self.current_annotator: str | None = None
        self.images: list[dict[str, Any]] = []
        self.categories: dict[Any, str] = {}
        self.image_by_id: dict[Any, dict[str, Any]] = {}
        self.source_paths_by_image: dict[Any, Path] = {}
        self.annotations_by_image: dict[Any, list[dict[str, Any]]] = {}
        # Image state, saved as frame_state: verified, review, or unverified (neither set; never saved).
        self.frame_verified_by_image: dict[Any, bool] = {}
        self.review_by_image: dict[Any, bool] = {}
        self.background_by_image: dict[Any, bool] = {}
        self._crop_export_settings: dict[str, Any] = {}  # last Export Crops choices, kept for the session
        self.persisted_image_ids: set[Any] = set()
        self.touched_image_ids: set[Any] = set()
        # Images whose project-folder file was copied, skipped or overwritten this session from their current source.
        self._folder_settled: set[Any] = set()
        self.roi_counts_by_image: dict[Any, dict[Any, list[int]]] = {}
        self.roi_totals = [0, 0]
        self.image_info = None
        self.next_annotation_id = 1
        # Highest box number per (image id, creator); box numbers are never reused, even after deletion.
        self._history_dialog: QDialog | None = None
        self._history_preview: QGraphicsRectItem | None = None
        # Boxes of the last saved file (see _saved_version), rebuilt whenever _base_json changes.
        self._saved_cache: tuple[dict, dict] = ({}, {})
        self._saved_cache_source: bytes | None = None
        self._saved_by_image: dict[str, list[dict[str, Any]]] = {}
        # Deleted boxes listed (greyed) after the current boxes in the box list, and the outline
        # drawn while one of them is selected.
        self._deleted_list_rows: list[dict[str, Any]] = []
        self._deleted_preview: QGraphicsRectItem | None = None
        self._selected_deleted: tuple[Any, Any] | None = None  # (image id, box id) of the selected grey row
        # Boxes removed permanently since the last save, as (image key, box id); the save drops their history.
        self._purged_boxes: set[tuple[str, Any]] = set()
        # Project-folder files of images deleted permanently since the last save; the save moves them to the trash.
        self._trash_on_save: set[Path] = set()
        # Users merged or deleted since the last save: old user id -> user now credited (None: unassigned).
        self._user_remap: dict[Any, Any] = {}
        # Who deleted each box since the last save, as (image key, box id) -> user id; the save credits them.
        self._deleted_by: dict[tuple[str, Any], Any] = {}
        # Who changed each image's state since the last save, by image key; the save credits them.
        self._state_by: dict[str, Any] = {}
        self._saved_states: dict[str, dict[str, Any]] = {}  # the last saved file's image states, by image key
        # Box history indexed by image and by user, rebuilt only when the history list changes.
        self._history_index_key: tuple | None = None
        self._history_by_image: dict[Any, list[dict[str, Any]]] = {}
        self._history_images_by_user: dict[Any, set[Any]] = {}
        self._history_generation = 0
        self._newest_time: str | None = None  # the newest time the project holds; found once, then kept
        self._history_unsaved = False  # history changed since the save (rewritten, purged, merged)
        # The image counts line in parts per image, so one edited image updates it without a recount.
        self._stats_key: tuple | None = None
        self._stats_parts: dict[Any, tuple] = {}
        self._stats_totals: dict[str, Any] = {}
        self._autosave_base_source: bytes | None = None  # the saved project last written beside a backup
        self._frame_lookup_key: tuple | None = None
        self._frame_lookup: dict[tuple, Any] = {}
        self._name_lookup_key: int | None = None
        self._name_lookup: dict[str, Any] = {}
        # Keys of images the last saved file holds with a state: saved work, kept even if set to Unverified.
        self._saved_state_keys: set[str] = set()
        # The box list as last built (what it showed), so a selection change only moves the highlight.
        self._box_list_last: tuple | None = None
        self._swatches: dict[int, QIcon] = {}
        # Image list rows in order, and the shown ones, so moving through a filtered list skips hidden rows fast.
        self._row_ids: list[Any] = []
        self._row_of: dict[Any, int] = {}
        self._shown_rows: list[int] = []
        # Highest category and user ids ever used, kept as ids are given out (found once, not per new id).
        self._highest_ids: dict[str, int] = {}
        self._last_read_index: tuple | None = None
        # "Changed since version" filter: {"label", "ids"} of the images it lists, or None when off.
        self._changed_since: dict[str, Any] | None = None
        self._viewing: dict[str, Any] | None = None  # a data version shown read-only: {"tag", "boxes", "states"}
        self._saved_project_cache: dict[str, Any] | None = None  # the last save, parsed (for showing versions)
        self._saved_project_source: bytes | None = None
        self.visible_image_ids: set[Any] | None = None
        self.current_image_id: Any = None
        self.current_items: list[BoxItem] = []
        self.base_image: Image.Image | None = None
        self.background_item: QGraphicsPixmapItem | None = None
        self.scale_bar_item: ScaleBarItem | None = None
        self.detail_item: QGraphicsPixmapItem | None = None
        self._loaded: LoadedImage | None = None
        self._pending_image_id: Any = None
        self._dataset_generation = 0
        self._load_workers: dict[Any, ImageLoadWorker] = {}
        self._adjust_generation = 0
        self._adjust_cancel: threading.Event | None = None
        self._adjust_workers: dict[int, PreviewAdjustmentWorker] = {}
        self._auto_level_workers: dict[int, AutoLevelsWorker] = {}
        self._auto_level_generation = 0
        self._detail_generation = 0
        self._detail_workers: dict[int, DetailWorker] = {}
        self.adjustment_pool = QThreadPool(self)
        self.adjustment_pool.setMaxThreadCount(2)
        self.image_pool = QThreadPool(self)
        self.image_pool.setMaxThreadCount(2)
        self.pipeline_signals = PipelineSignals(self)
        self._frame_items: dict[Any, QListWidgetItem] = {}
        self._frame_names: dict[Any, str] = {}
        self._pair_ious_by_image: dict[Any, list[float]] = {}
        self._shown_ids: set[Any] = set()
        self._user_name_by_id: dict[Any, str] = {}  # ids are numbers, or text in a file from another tool
        self._user_id_by_name: dict[str, Any] = {}
        self._load_notes: list[str] = []
        # (modified time, size) of the project file when it was opened or last saved by this session.
        self._file_signature: tuple[int, int] | None = None
        self._autosave_thread: threading.Thread | None = None
        self._edit_count = 0  # bumped by every change; the backup is skipped when it has not moved
        self._autosaved_count = -1
        self.dirty = False
        self._refreshing = False
        self._undo_history: list[tuple[Any, list[dict[str, Any]]]] = []
        self._redo_history: list[tuple[Any, list[dict[str, Any]]]] = []
        self._last_category_id: Any = None  # category chosen for the last drawn box
        self._file_sizes: dict[str, int | None] = {}  # source path -> bytes, for the image info line
        # The project file as opened or last saved, zlib-compressed: what Merge Project merges, and the baseline for "*".
        self._base_json: bytes | None = None
        # After a merge, the merged result: the next save forms versions against it instead of _base_json.
        self._last_read_base: bytes | None = None
        self._next_image_id = 1  # never reused, so a late result for a dropped frame cannot land on a new one
        self._frame_sort_keys: dict[Any, list] = {}
        # Videos listed this session. A frame opened from a video becomes an image entry; it joins the
        # image list once it has work and is written out as a PNG at Save.
        self.videos: dict[str, AviInfo] = {}
        self.video_frame_by_image: dict[Any, tuple[AviInfo, int]] = {}
        self._listed_video_frames: set[Any] = set()
        self._active_video: AviInfo | None = None
        self._video_reader: AviReader | None = None
        self._video_positions: dict[str, int] = {}

        self._update_window_title()
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "stingray_label_icon.png")))
        self.resize(1400, 900)
        self._load_shortcuts()
        self._build_ui()
        signals = self.pipeline_signals
        signals.image_loaded.connect(self._image_loaded)
        signals.image_failed.connect(self._image_failed)
        signals.image_skipped.connect(self._image_skipped)
        signals.preview_adjusted.connect(self._adjustment_finished)
        signals.detail_ready.connect(self._detail_finished)
        signals.levels_ready.connect(self._auto_levels_finished)
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setInterval(AUTOSAVE_INTERVAL_MS)
        self.autosave_timer.timeout.connect(self._autosave)
        self.autosave_timer.start()
        self._set_dataset(None, {"images": [], "annotations": [], "categories": []})

    def _build_ui(self) -> None:
        # The active user, shown only; it is changed from Edit → Change User….
        self.user_label = QLabel()
        self.user_label.setContentsMargins(8, 0, 12, 0)
        self.menuBar().setCornerWidget(self.user_label, Qt.Corner.TopRightCorner)
        self._show_user_name()
        file_menu = self.menuBar().addMenu("&File")
        self.new_project_action = QAction("New Project…", self)
        self.open_project_action = QAction("Open Project…", self)
        self.add_images_action = QAction("Add Images…", self)
        self.add_folder_action = QAction("Add Image Folder…", self)
        self.add_video_action = QAction("Add Video…", self)
        self.add_video_folder_action = QAction("Add Video Folder…", self)
        self.save_project_action = QAction("Save", self)
        self.save_project_action.setShortcut(QKeySequence("Ctrl+S"))
        self.save_project_action.setToolTip(
            "Save the project JSON to its current file, or choose a file if it has not been saved yet"
        )
        self.save_project_as_action = QAction("Save As…", self)
        self.save_project_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.save_project_as_action.setToolTip("Choose a folder and file name for the project JSON")
        self.merge_project_action = QAction("Merge Project…", self)
        self.merge_project_action.setToolTip(
            "Merge another saved copy of this project into your saved project; every box version is kept"
        )
        self.merge_project_action.setEnabled(False)
        # Every export in one submenu, named without the word "Export".
        self.training_export_action = QAction("Training Dataset…", self)
        self.training_export_action.setToolTip(
            "Export verified images, verified boxes, and explicitly marked background images"
        )
        favorites_export_action = QAction("Favorites…", self)
        favorites_export_action.setToolTip("Copy the favorite images, with a COCO JSON of their boxes, to a folder")
        favorites_export_action.triggered.connect(self._export_favorites)
        self.save_image_action = QAction("Image…", self)
        self.save_image_action.setToolTip("Save the displayed image with its boxes drawn on it")
        self.save_rois_action = QAction("Crops…", self)
        self.save_rois_action.setToolTip("Save boxes as separate images, filtered by image, status and class")
        activity_action = QAction("Activity (CSV)…", self)
        activity_action.setToolTip("Per day and user: boxes drawn, edited, verified, deleted and images set, as CSV")
        activity_action.triggered.connect(self._export_activity)
        export_menu = QMenu("Export", self)
        export_menu.setToolTipsVisible(True)
        export_menu.addAction(self.training_export_action)
        export_menu.addAction(favorites_export_action)
        export_menu.addSeparator()
        export_menu.addAction(self.save_image_action)
        export_menu.addAction(self.save_rois_action)
        export_menu.addSeparator()
        export_menu.addAction(activity_action)
        self.exit_action = QAction("Exit", self)
        self.add_images_action.setEnabled(False)
        self.add_folder_action.setEnabled(False)
        self.add_video_action.setEnabled(False)
        self.add_video_folder_action.setEnabled(False)
        self.save_project_action.setEnabled(False)
        self.save_project_as_action.setEnabled(False)
        self.training_export_action.setEnabled(False)
        # Grouped: open, save, add sources, merge, export, then Exit.
        for group in (
            (self.new_project_action, self.open_project_action),
            (self.save_project_action, self.save_project_as_action),
            (self.add_images_action, self.add_folder_action, self.add_video_action,
             self.add_video_folder_action),
            (self.merge_project_action,),
            (export_menu,),
            (self.exit_action,),
        ):
            if file_menu.actions():
                file_menu.addSeparator()
            for entry in group:
                file_menu.addMenu(entry) if isinstance(entry, QMenu) else file_menu.addAction(entry)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        self.redo_action = QAction("Redo", self)
        self.redo_action.setShortcut(QKeySequence("Ctrl+Y"))
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        # Its key is handled with the other single-key shortcuts (eventFilter), so typing in a search box is not
        # taken; the menus and tips that name keys are filled in by _show_shortcut_keys.
        self.change_category_action = QAction("Change Category…", self)
        self.change_category_action.setEnabled(False)
        self.change_category_action.triggered.connect(self._change_category)
        edit_menu.addAction(self.change_category_action)
        edit_menu.addSeparator()
        self.edit_labels_action = QAction("Categories…", self)
        self.import_classes_action = QAction("Import Categories…", self)
        self.import_classes_action.setToolTip("Copy category names from another project or COCO JSON")
        self.edit_labels_action.setEnabled(False)
        self.import_classes_action.setEnabled(False)
        self.change_annotator_action = QAction("Change User…", self)
        self.change_annotator_action.setEnabled(False)
        self.edit_annotators_action = QAction("Users…", self)
        self.edit_annotators_action.setEnabled(False)
        edit_menu.addAction(self.edit_labels_action)
        edit_menu.addAction(self.import_classes_action)
        edit_menu.addAction(self.edit_annotators_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.change_annotator_action)
        self._edit_menu_tail = edit_menu.addSeparator()  # Find… goes in before it
        edit_menu.addAction("Keyboard Shortcuts…").triggered.connect(self._edit_shortcuts)

        container = QWidget()
        layout = QHBoxLayout(container)
        splitter = QSplitter()

        sidebar = QWidget()
        side_layout = QVBoxLayout(sidebar)
        self.video_section = QWidget()
        video_layout = QVBoxLayout(self.video_section)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.addWidget(QLabel("Videos"))
        self.video_search = QLineEdit()
        self.video_search.setPlaceholderText("Search videos…")
        self.video_search.setClearButtonEnabled(True)
        video_layout.addWidget(self.video_search)
        self.video_list = FrameListWidget()
        self.video_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.video_list.setMaximumHeight(140)
        video_layout.addWidget(self.video_list)
        self.video_section.setVisible(False)
        # Frame controls sit under the image, beside the image stats, while a video is selected.
        self.video_controls = QWidget()
        frame_row = QHBoxLayout(self.video_controls)
        frame_row.setContentsMargins(0, 0, 0, 0)
        frame_row.addWidget(QLabel("Frame"))
        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setMinimumWidth(320)
        self.frame_slider.setPageStep(10)
        # Never takes keyboard focus, so the frame keys keep working after dragging it.
        self.frame_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.frame_slider.setToolTip("Drag to scrub through the video")
        frame_row.addWidget(self.frame_slider, 1)
        self.frame_back_button = QToolButton()
        self.frame_back_button.setArrowType(Qt.ArrowType.LeftArrow)
        self.frame_box = FrameNumberBox()
        self.frame_box.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.frame_box.setKeyboardTracking(False)  # typing jumps only on Enter
        self.frame_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame_forward_button = QToolButton()
        self.frame_forward_button.setArrowType(Qt.ArrowType.RightArrow)
        for button in (self.frame_back_button, self.frame_forward_button):
            button.setAutoRepeat(True)  # hold to keep stepping
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        frame_row.addWidget(self.frame_back_button)
        frame_row.addWidget(self.frame_box)
        frame_row.addWidget(self.frame_forward_button)
        self.video_controls.setVisible(False)
        # Videos (shown once videos are added) above Images; each list has its own title and search.
        side_layout.addWidget(self.video_section)
        images_row = QHBoxLayout()
        images_row.addWidget(QLabel("Images"))
        images_row.addStretch(1)
        self.unsaved_filter_check = QCheckBox("Unsaved only")
        self.unsaved_filter_check.setToolTip("Show only images with changes not saved yet (marked *)")
        images_row.addWidget(self.unsaved_filter_check)  # on the Images title line
        side_layout.addLayout(images_row)
        self.frame_search = QLineEdit()
        self.frame_search.setPlaceholderText("Search images…")
        self.frame_search.setClearButtonEnabled(True)
        side_layout.addWidget(self.frame_search)
        self.frame_list = FrameListWidget()
        # Qt's default per-row scrolling miscalculates positions when filters hide rows and a
        # horizontal scrollbar is shown, making the list jump; per-pixel scrolling does not.
        self.frame_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        side_layout.addWidget(self.frame_list, 1)
        filters_heading = QLabel("Filters")
        heading_font = filters_heading.font()
        heading_font.setBold(True)
        filters_heading.setFont(heading_font)
        filters_row = QHBoxLayout()
        filters_row.addWidget(filters_heading)
        filters_row.addStretch(1)
        self.clear_filters_button = QPushButton("Clear")
        self.clear_filters_button.setToolTip(
            "Reset every filter, including Unsaved only and those in Data → Filter, so all images are listed"
        )
        self.clear_filters_button.setEnabled(False)
        filters_row.addWidget(self.clear_filters_button)
        side_layout.addLayout(filters_row)
        side_layout.addWidget(QLabel("User"))
        self.annotator_filter = QComboBox()
        self.annotator_filter.addItem("All users", self.ALL_ANNOTATORS_FILTER_ID)
        self.annotator_filter.currentIndexChanged.connect(self._apply_frame_filters)
        side_layout.addWidget(self.annotator_filter)
        self.annotator_history_action = QAction("User: Include Earlier Versions", self)
        self.annotator_history_action.setCheckable(True)
        self.annotator_history_action.setToolTip(
            "Off: images where the user made the current version of a box.\n"
            "On: also images where the user made an earlier, saved version of a box."
        )
        self.annotator_history_action.toggled.connect(self._apply_frame_filters)
        side_layout.addWidget(QLabel("Categories"))
        class_buttons = QHBoxLayout()
        select_all = QPushButton("All")
        select_none = QPushButton("None")
        class_buttons.addWidget(select_all)
        class_buttons.addWidget(select_none)
        side_layout.addLayout(class_buttons)
        self.class_search = QLineEdit()
        self.class_search.setPlaceholderText("Search categories…")
        self.class_search.setClearButtonEnabled(True)
        self.class_search.setToolTip("Show only matching categories; All and None apply to the ones shown.")
        side_layout.addWidget(self.class_search)
        self.class_filter = QListWidget()
        self.class_filter.setMaximumHeight(150)
        side_layout.addWidget(self.class_filter)
        side_layout.addWidget(QLabel("Image state"))
        self.frame_verification_filter = QComboBox()
        self.frame_verification_filter.addItem("All", "all")
        self.frame_verification_filter.addItem("Verified", "verified")
        self.frame_verification_filter.addItem("Unverified", "unverified")
        self.frame_verification_filter.addItem("Review", "review")
        side_layout.addWidget(self.frame_verification_filter)
        side_layout.addWidget(QLabel("Box verification"))
        self.verification_filter = QComboBox()
        self.verification_filter.addItem("All", "all")
        self.verification_filter.addItem("Verified", "verified")
        self.verification_filter.addItem("Unverified", "unverified")
        side_layout.addWidget(self.verification_filter)
        # Overlap filter on one line (in Data → Filter); "Off" stands in for a separate on/off tick.
        iou_widget = QWidget()
        iou_row = QHBoxLayout(iou_widget)
        iou_row.addWidget(QLabel("Overlap IoU"))
        self.iou_filter_operator = QComboBox()
        self.iou_filter_operator.setToolTip(
            "Show images where at least one pair of overlapping boxes has an IoU that meets the comparison"
        )
        self.iou_filter_operator.addItem("Off", "off")
        for operator in (">=", ">", "<", "<=", "=="):
            self.iou_filter_operator.addItem(operator, operator)
        self.iou_filter_threshold = QDoubleSpinBox()
        self.iou_filter_threshold.setRange(0.0, 1.0)
        self.iou_filter_threshold.setDecimals(2)
        self.iou_filter_threshold.setSingleStep(0.05)
        self.iou_filter_threshold.setValue(0.5)
        self.iou_filter_threshold.setEnabled(False)
        iou_row.addWidget(self.iou_filter_operator)
        iou_row.addWidget(self.iou_filter_threshold)
        # Model score filter, laid out like the overlap filter; boxes without a score never match.
        score_widget = QWidget()
        score_row = QHBoxLayout(score_widget)
        score_row.addWidget(QLabel("Score"))
        self.score_filter_operator = QComboBox()
        self.score_filter_operator.setToolTip(
            "Show images where at least one box with a model score meets the comparison"
        )
        self.score_filter_operator.addItem("Off", "off")
        for operator in (">=", ">", "<", "<="):
            self.score_filter_operator.addItem(operator, operator)
        self.score_filter_threshold = QDoubleSpinBox()
        self.score_filter_threshold.setRange(0.0, 1.0)
        self.score_filter_threshold.setDecimals(2)
        self.score_filter_threshold.setSingleStep(0.05)
        self.score_filter_threshold.setValue(0.5)
        self.score_filter_threshold.setEnabled(False)
        score_row.addWidget(self.score_filter_operator)
        score_row.addWidget(self.score_filter_threshold)
        # One line of counts at the bottom of the panel; the full breakdown is in its tooltip.
        self.frame_stats = QLabel("No image selected")
        # Wraps to the panel's width, so it never sets the panel's minimum width.
        self.frame_stats.setWordWrap(True)
        side_layout.addWidget(self.frame_stats)
        self.previous_button = QPushButton("Previous image")
        self.next_button = QPushButton("Next image")
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)

        scale_widget = QWidget()
        scale_layout = QVBoxLayout(scale_widget)
        self.show_ruler = QCheckBox("Show ruler")
        self.show_ruler.setChecked(False)
        scale_layout.addWidget(self.show_ruler)
        self.show_scale_bar = QCheckBox("Show scale bar")
        self.show_scale_bar.setChecked(False)
        self.show_scale_bar.setEnabled(False)  # until the scale is calibrated
        scale_layout.addWidget(self.show_scale_bar)
        scale_layout.addWidget(QLabel("Pixel resolution"))
        self.pixel_resolution = QDoubleSpinBox()
        self.pixel_resolution.setRange(0.0, 100000)
        self.pixel_resolution.setDecimals(3)
        self.pixel_resolution.setSpecialValueText("Not calibrated")  # shown at 0
        self.pixel_resolution.setValue(0.0)
        self.pixel_resolution.setSuffix(" µm/px")
        scale_layout.addWidget(self.pixel_resolution)
        self.pixel_resolution.setToolTip(
            "Physical distance represented by each source-image pixel. Not calibrated (0): the ruler counts "
            "pixels and no scale bar is drawn. Type a value or use Calibrate from line."
        )
        self.calibrate_scale_button = QPushButton("Calibrate from line…")
        self.calibrate_scale_button.setCheckable(True)
        self.calibrate_scale_button.setToolTip(
            "Draw across a known dimension to calculate the image pixel resolution"
        )
        scale_layout.addWidget(self.calibrate_scale_button)
        scale_layout.addWidget(QLabel("Scale bar length (auto-fits when needed)"))
        self.scale_bar_length = QDoubleSpinBox()
        self.scale_bar_length.setRange(0.001, 100000)
        self.scale_bar_length.setDecimals(3)
        self.scale_bar_length.setValue(10.0)
        self.scale_bar_length.setSuffix(" mm")
        scale_layout.addWidget(self.scale_bar_length)
        self.scale_menu = QMenu("Scale", self)
        scale_action = QWidgetAction(self.scale_menu)
        scale_action.setDefaultWidget(scale_widget)
        self.scale_menu.addAction(scale_action)

        self.scene = QGraphicsScene(self)
        self.view = AnnotationView(self.scene)

        self.levels_menu = QMenu("Image", self)
        levels_widget = QWidget()
        levels_layout = QVBoxLayout(levels_widget)
        # One slider per row, in a grid so labels, sliders and values line up; the value shows beside it.
        slider_grid = QGridLayout()
        slider_grid.setColumnStretch(1, 1)
        for row, (name, label, low, high, value, text) in enumerate((
            ("brightness_slider", "Brightness", 0, 300, 100, "100%"),
            ("contrast_slider", "Contrast", 0, 300, 100, "100%"),
            ("gamma_slider", "Gamma", 25, 400, 100, "1.00"),
            ("black_point", "Black", 0, 254, 0, "0"),
            ("white_point", "White", 1, 255, 255, "255"),
        )):
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(low, high)
            slider.setValue(value)
            shown = QLabel(text)
            shown.setMinimumWidth(QLabel("300%").sizeHint().width())
            shown.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            slider_grid.addWidget(QLabel(label), row, 0)
            slider_grid.addWidget(slider, row, 1)
            slider_grid.addWidget(shown, row, 2)
            setattr(self, name, slider)
            if name in ("black_point", "white_point"):
                slider.valueChanged.connect(lambda number, shown=shown: shown.setText(str(number)))
            else:
                setattr(self, name.replace("_slider", "_value"), shown)
        levels_layout.addLayout(slider_grid)
        self.auto_levels_button = QPushButton("Auto levels for this image")
        levels_layout.addWidget(self.auto_levels_button)
        self.invert_check = QCheckBox("Invert")
        levels_layout.addWidget(self.invert_check)
        levels_widget.setMinimumWidth(340)
        levels_action = QWidgetAction(self.levels_menu)
        levels_action.setDefaultWidget(levels_widget)
        self.levels_menu.addAction(levels_action)
        self.reset_image_action = QAction("Reset Image", self)
        self.levels_menu.addAction(self.reset_image_action)
        display_menu = self.menuBar().addMenu("&View")
        display_menu.addMenu(self.levels_menu)
        display_menu.addMenu(self.scale_menu)
        display_menu.addSeparator()
        self.reference_lines_action = QAction("Reference Lines", self)
        self.reference_lines_action.setCheckable(True)
        self.reference_lines_action.setChecked(True)
        self.reference_lines_action.setToolTip("Horizontal and vertical lines through the cursor while drawing a box")
        self.reference_lines_action.toggled.connect(self.view.set_reference_lines)
        display_menu.addAction(self.reference_lines_action)
        self.selection_color_action = QAction("Selection Color…", self)
        self.selection_color_action.setToolTip("Outline color of the selected box")
        self.selection_color_action.triggered.connect(self._choose_selection_color)
        display_menu.addAction(self.selection_color_action)

        self.adjustment_timer = QTimer(self)
        self.adjustment_timer.setSingleShot(True)
        self.adjustment_timer.setInterval(90)
        self.adjustment_timer.timeout.connect(self._update_display_image)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        navigation = QHBoxLayout()
        navigation.addWidget(self.previous_button, 0, Qt.AlignmentFlag.AlignLeft)
        navigation.addStretch(1)
        self.image_name_label = QLabel()
        self.image_name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        navigation.addWidget(self.image_name_label, 0, Qt.AlignmentFlag.AlignCenter)
        navigation.addStretch(1)
        navigation.addWidget(self.next_button, 0, Qt.AlignmentFlag.AlignRight)
        editor_layout.addLayout(navigation)
        image_panel = QWidget(editor)
        image_grid = QGridLayout(image_panel)
        image_grid.setContentsMargins(0, 0, 0, 0)
        image_grid.setSpacing(0)
        self.coordinate_readout = QLabel("0")
        self.coordinate_readout.setFixedSize(36, 22)
        self.coordinate_readout.setToolTip("Image origin (0, 0); X increases right and Y increases down")
        self.coordinate_readout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.coordinate_readout.setAutoFillBackground(True)
        self.top_ruler = ImageRuler(
            self.view, Qt.Orientation.Horizontal, self.pixel_resolution.value(), image_panel
        )
        self.left_ruler = ImageRuler(
            self.view, Qt.Orientation.Vertical, self.pixel_resolution.value(), image_panel
        )
        image_grid.addWidget(self.coordinate_readout, 0, 0)
        image_grid.addWidget(self.top_ruler, 0, 1)
        image_grid.addWidget(self.left_ruler, 1, 0)
        image_grid.addWidget(self.view, 1, 1)
        editor_layout.addWidget(image_panel, 1)
        self.image_info = QLabel()
        self.image_info.setWordWrap(True)
        self.image_info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        # In the image column, so the line starts at the image's left edge rather than under the ruler.
        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 4, 0, 0)
        info_row.addWidget(self.image_info, 1)
        info_row.addWidget(self.video_controls, 0, Qt.AlignmentFlag.AlignRight)
        image_grid.addLayout(info_row, 2, 1)
        image_grid.setRowStretch(1, 1)

        annotation_panel = QWidget()
        annotation_layout = QVBoxLayout(annotation_panel)
        # Image state: exactly one of three, on one line.
        state_row = QHBoxLayout()
        state_row.addWidget(QLabel("State:"))
        self.state_group = QButtonGroup(self)
        self.state_buttons: dict[str, QRadioButton] = {}
        for state, label in (("unverified", "Unverified"), ("review", "Review"), ("verified", "Verified")):
            button = QRadioButton(label)
            button.setEnabled(False)
            self.state_group.addButton(button)
            self.state_buttons[state] = button
            state_row.addWidget(button)
        state_row.addStretch(1)
        annotation_layout.addLayout(state_row)
        annotation_layout.addWidget(QLabel("Add / edit box"))
        self.draw_button = QPushButton("Draw box")
        self.draw_button.setCheckable(True)
        self.repeat_category_check = QCheckBox("Repeat last category")
        draw_row = QHBoxLayout()  # one line, so the box list keeps its height
        draw_row.addWidget(self.draw_button)
        draw_row.addWidget(self.repeat_category_check)
        draw_row.addStretch(1)
        annotation_layout.addLayout(draw_row)
        self.background_check = QCheckBox("Background")
        self.background_check.setToolTip("The image has no objects; can be ticked only when it has no boxes")
        self.background_check.setEnabled(False)
        annotation_layout.addWidget(self.background_check)
        # Long names are cut short in the panels; the drop-down list shows them in full.
        for combo in (self.annotator_filter, self.frame_verification_filter, self.verification_filter):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
        self.edit_status = QCheckBox("Box verified")
        self.edit_status.setEnabled(False)
        annotation_layout.addWidget(self.edit_status)
        annotation_layout.addWidget(QLabel("Boxes"))
        self.annotation_search = QLineEdit()
        self.annotation_search.setPlaceholderText("Search boxes by category…")
        self.annotation_search.setClearButtonEnabled(True)
        self.annotation_search.setToolTip("Show only boxes whose current category contains this text")
        annotation_layout.addWidget(self.annotation_search)
        # Names the fields of each box row, in order; only the fields this image's boxes have.
        self.box_list_header = QLabel()
        self.box_list_header.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.box_list_header.setToolTip("✓: verified · Ver: saved versions · IoU: highest overlap with another box")
        self.box_list_header.hide()
        annotation_layout.addWidget(self.box_list_header)
        self.annotation_list = QListWidget()
        annotation_layout.addWidget(self.annotation_list, 1)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        annotation_layout.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignBottom)

        splitter.addWidget(sidebar)
        splitter.addWidget(editor)
        splitter.addWidget(annotation_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([260, 900, 280])
        layout.addWidget(splitter)
        self.setCentralWidget(container)

        self.class_filter.itemChanged.connect(self._apply_frame_filters)
        self.unsaved_filter_check.toggled.connect(self._apply_frame_filters)
        self.clear_filters_button.clicked.connect(self._clear_filters)
        self.frame_verification_filter.currentIndexChanged.connect(self._apply_frame_filters)
        self.verification_filter.currentIndexChanged.connect(self._apply_frame_filters)
        self.iou_filter_operator.currentIndexChanged.connect(self._iou_filter_changed)
        self.iou_filter_threshold.valueChanged.connect(
            lambda _value: self.search_timer.start() if self._iou_filter_on() else None  # waits for typing
        )
        self.score_filter_operator.currentIndexChanged.connect(self._score_filter_changed)
        self.score_filter_threshold.valueChanged.connect(
            lambda _value: self.search_timer.start() if self._score_filter_on() else None  # waits for typing
        )
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(150)
        self.search_timer.timeout.connect(self._apply_frame_filters)
        # Lambdas: a signal's argument would otherwise become QTimer.start(msec).
        self.frame_search.textChanged.connect(lambda _text: self.search_timer.start())
        self.video_search.textChanged.connect(lambda _text: self._filter_video_list())
        self.box_search_timer = QTimer(self)
        self.box_search_timer.setSingleShot(True)
        self.box_search_timer.setInterval(150)
        self.box_search_timer.timeout.connect(lambda: self._refresh_annotation_list(self._selected_item()))
        self.annotation_search.textChanged.connect(lambda _text: self.box_search_timer.start())
        self.detail_timer = QTimer(self)
        self.detail_timer.setSingleShot(True)
        self.detail_timer.setInterval(120)
        self.detail_timer.timeout.connect(self._update_detail)
        self.frame_list.currentItemChanged.connect(self._frame_selection_changed)
        self.frame_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.frame_list.customContextMenuRequested.connect(self._frame_list_menu)
        self.video_list.currentItemChanged.connect(self._video_selection_changed)
        self.frame_box.valueChanged.connect(self._show_video_frame)
        self.frame_slider.valueChanged.connect(self._show_video_frame)
        self.frame_back_button.clicked.connect(lambda: self._step_video_frame(-1))
        self.frame_forward_button.clicked.connect(lambda: self._step_video_frame(1))
        self.previous_button.clicked.connect(lambda: self._navigate_frames(-1))
        self.next_button.clicked.connect(lambda: self._navigate_frames(1))
        self.brightness_slider.valueChanged.connect(self._brightness_changed)
        self.contrast_slider.valueChanged.connect(self._contrast_changed)
        self.gamma_slider.valueChanged.connect(self._gamma_changed)
        self.black_point.valueChanged.connect(self._levels_changed)
        self.white_point.valueChanged.connect(self._levels_changed)
        self.auto_levels_button.clicked.connect(self._auto_levels_requested)
        self.invert_check.toggled.connect(self._adjustment_changed)
        self.view.rectangleCreated.connect(self._add_rectangle)
        self.view.calibrationLineCreated.connect(self._calibrate_scale_from_line)
        self.view.middleClicked.connect(self._toggle_auto_levels)  # like L
        self.view.calibrationModeChanged.connect(self.calibrate_scale_button.setChecked)
        self.calibrate_scale_button.toggled.connect(self._set_calibration_mode)
        self.draw_button.toggled.connect(self._set_draw_mode)
        # buttonClicked fires only for user clicks, not for the state shown when an image loads.
        self.state_group.buttonClicked.connect(
            lambda button: self._set_frame_state(
                next(state for state, candidate in self.state_buttons.items() if candidate is button)
            )
        )
        self.background_check.toggled.connect(self._background_changed)
        self.reset_image_action.triggered.connect(self._reset_image_view)
        self.show_ruler.toggled.connect(self._set_ruler_visible)
        self._set_ruler_visible(self.show_ruler.isChecked())  # hidden until asked for
        self.show_scale_bar.toggled.connect(self._update_scale_bar)
        self.pixel_resolution.valueChanged.connect(self._update_scale_bar)
        self.pixel_resolution.valueChanged.connect(self._update_rulers)
        self.pixel_resolution.valueChanged.connect(lambda value: self.show_scale_bar.setEnabled(value > 0))
        self.scale_bar_length.valueChanged.connect(self._update_scale_bar)
        self.view.viewChanged.connect(self._update_rulers)
        self.view.horizontalScrollBar().valueChanged.connect(self._update_rulers)
        self.view.verticalScrollBar().valueChanged.connect(self._update_rulers)
        self.view.viewChanged.connect(lambda: self.detail_timer.start())
        self.view.horizontalScrollBar().valueChanged.connect(lambda _value: self.detail_timer.start())
        self.view.verticalScrollBar().valueChanged.connect(lambda _value: self.detail_timer.start())
        self.scene.selectionChanged.connect(self._selection_changed)
        self.annotation_list.currentRowChanged.connect(self._annotation_row_changed)
        self.annotation_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.annotation_list.customContextMenuRequested.connect(self._annotation_list_menu)
        self.annotation_list.itemDoubleClicked.connect(self._annotation_double_clicked)
        self.view.contextMenuRequested.connect(self._view_menu)
        self.edit_status.toggled.connect(self._status_changed)
        self.edit_labels_action.triggered.connect(self._edit_labels)
        self.import_classes_action.triggered.connect(self._import_classes)
        self.change_annotator_action.triggered.connect(self._change_annotator)
        self.edit_annotators_action.triggered.connect(self._edit_annotators)
        select_all.clicked.connect(lambda: self._set_all_classes(True))
        select_none.clicked.connect(lambda: self._set_all_classes(False))
        self.class_search.textChanged.connect(lambda _text: self._show_matching_classes())
        self.new_project_action.triggered.connect(self.new_project)
        self.open_project_action.triggered.connect(self.open_project)
        self.add_images_action.triggered.connect(self.add_images)
        self.add_folder_action.triggered.connect(self.add_folder)
        self.add_video_action.triggered.connect(self.add_videos)
        self.add_video_folder_action.triggered.connect(self.add_video_folder)
        self.save_project_action.triggered.connect(self.save_project)
        self.save_project_as_action.triggered.connect(self.save_project_as)
        self.merge_project_action.triggered.connect(self.merge_project)
        self.training_export_action.triggered.connect(self.export_training_dataset)
        self.save_image_action.triggered.connect(self.export_frame)
        self.save_rois_action.triggered.connect(self.export_rois)
        self.exit_action.triggered.connect(self.close)
        self.undo_action.triggered.connect(self.undo)
        self.redo_action.triggered.connect(self.redo)

        data_menu = self.menuBar().addMenu("&Data")
        filter_menu = data_menu.addMenu("Filter")
        filter_menu.setToolTipsVisible(True)
        iou_action = QWidgetAction(filter_menu)
        iou_action.setDefaultWidget(iou_widget)
        filter_menu.addAction(iou_action)
        score_action = QWidgetAction(filter_menu)
        score_action.setDefaultWidget(score_widget)
        filter_menu.addAction(score_action)
        filter_menu.addAction(self.annotator_history_action)
        self.changed_since_action = QAction("Changed in Version…", self)
        self.changed_since_action.setCheckable(True)
        self.changed_since_action.setToolTip("List only images changed during a data version (from its creation to "
                                             "the next one's), optionally by one user")
        self.changed_since_action.triggered.connect(self._toggle_changed_since)
        filter_menu.addAction(self.changed_since_action)
        self.favorites_only_action = QAction("Favorites Only", self)
        self.favorites_only_action.setCheckable(True)
        self.favorites_only_action.toggled.connect(lambda _checked: self._apply_frame_filters())
        filter_menu.addAction(self.favorites_only_action)
        filter_menu.addSeparator()
        filter_menu.addAction("Clear Filters").triggered.connect(self._clear_filters)
        # Grouped: looking at the data, then data versions, then history upkeep. Categories, users, merge and
        # exports are in Edit and File.
        data_menu.setToolTipsVisible(True)
        summary_action = data_menu.addAction("Summary…")
        summary_action.setToolTip("Images, boxes, categories and users in numbers")
        summary_action.triggered.connect(self._show_summary)
        listed_state_menu = data_menu.addMenu("Set State of Listed Images")
        listed_state_menu.setToolTipsVisible(True)
        for state in FRAME_STATES:
            action = listed_state_menu.addAction(state.capitalize())
            action.setToolTip(f"Set every image in the list, as currently filtered, to {state.capitalize()}")
            action.triggered.connect(lambda _checked=False, state=state: self._set_listed_frame_state(state))
        data_menu.addSeparator()
        versions_action = data_menu.addAction("Data Versions…")
        versions_action.setToolTip("Name points in time to export the dataset as it was, or to revert to")
        versions_action.triggered.connect(self._edit_dataset_versions)
        revert_action = data_menu.addAction("Show Data Version…")
        revert_action.setToolTip("Show the boxes as they were at a data version (read-only; nothing is edited)")
        revert_action.triggered.connect(self._revert_to)
        self.restore_version_action = data_menu.addAction("Restore This Version…")
        self.restore_version_action.setToolTip("Put the project back as the data version shown, starting a new "
                                               "data version")
        self.restore_version_action.setEnabled(False)
        self.restore_version_action.triggered.connect(self._restore_shown_version)
        compare_action = data_menu.addAction("Compare Data Versions…")
        compare_action.setToolTip("Boxes added, deleted and changed between two data versions")
        compare_action.triggered.connect(self._compare_versions)
        data_menu.addSeparator()
        compact_action = data_menu.addAction("Compact History…")
        compact_action.setToolTip("Keep only the newest of a user's back-to-back versions of their own box")
        compact_action.triggered.connect(self._compact_history)

        # Keys are handled in eventFilter (so typing in a search box is not taken); the menu lists them.
        go_menu = self.menuBar().addMenu("&Navigation")
        self._navigation_actions: dict[str, tuple[QAction, str]] = {}
        for group in (
            (("previous", "Previous Image", lambda: self._navigate_frames(-1)),
             ("next", "Next Image", lambda: self._navigate_frames(1)),
             ("previous_unverified", "Previous Unverified Image", lambda: self._jump_to_unverified(-1)),
             ("next_unverified", "Next Unverified Image", lambda: self._jump_to_unverified(1))),
            (("previous_box", "Previous Box", lambda: self._step_box(-1)),
             ("next_box", "Next Box", lambda: self._step_box(1))),
        ):
            if go_menu.actions():
                go_menu.addSeparator()
            for name, text, slot in group:
                action = go_menu.addAction(text)
                action.triggered.connect(slot)
                self._navigation_actions[name] = (action, text)  # named with its key by _show_shortcut_keys

        help_menu = self.menuBar().addMenu("&Help")
        self.user_guide_action = QAction("User Guide", self)
        self.user_guide_action.setShortcut(QKeySequence("F1"))
        self.user_guide_action.triggered.connect(self._show_user_guide)
        help_menu.addAction(self.user_guide_action)
        self._user_guide: QDialog | None = None

        self.find_action = QAction("Find…", self)
        self.find_action.setToolTip("Search videos and images by name")
        self.find_action.triggered.connect(lambda: (self.frame_search.setFocus(), self.frame_search.selectAll()))
        edit_menu.insertSeparator(self._edit_menu_tail)
        edit_menu.insertAction(self._edit_menu_tail, self.find_action)
        # Keyboard shortcuts for menu commands; menus show them next to each item.
        for action, keys in (
            (self.new_project_action, ["Ctrl+N"]),
            (self.open_project_action, ["Ctrl+O"]),
            (self.merge_project_action, ["Ctrl+M"]),
            (self.add_images_action, ["Ctrl+I"]),
            (self.add_folder_action, ["Ctrl+Shift+I"]),
            (self.add_video_action, ["Ctrl+Alt+V"]),
            (self.add_video_folder_action, ["Ctrl+Shift+V"]),
            (self.training_export_action, ["Ctrl+Shift+E"]),
            (self.redo_action, ["Ctrl+Y"]),
            (self.find_action, ["Ctrl+F"]),
            (self.edit_labels_action, ["Ctrl+Shift+C"]),
            (self.change_annotator_action, ["Ctrl+U"]),
            (self.edit_annotators_action, ["Ctrl+Shift+U"]),
            (self.reference_lines_action, ["Ctrl+R"]),
        ):
            action.setShortcuts([QKeySequence(key) for key in keys])
        self._show_shortcut_keys()

    def _show_user_guide(self) -> None:
        """Show the user guide (assets/user_guide.md) in a window that can stay open while working."""
        if self._user_guide is None:
            path = Path(__file__).parent / "assets" / "user_guide.md"
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as error:
                QMessageBox.critical(self, "User guide", f"Could not open the user guide:\n{error}")
                return
            dialog = QDialog(self)
            dialog.setWindowTitle("Stingray Labeler — User Guide")
            layout = QVBoxLayout(dialog)
            browser = QTextBrowser(dialog)
            browser.setMarkdown(text)
            browser.setOpenExternalLinks(True)
            layout.addWidget(browser)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
            buttons.rejected.connect(dialog.close)
            layout.addWidget(buttons)
            dialog.resize(760, 820)
            self._user_guide = dialog
        self._user_guide.show()
        self._user_guide.raise_()
        self._user_guide.activateWindow()

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

    def _set_dataset(self, root: Path | None, annotation_data: dict[str, Any], source_path: Path | None = None,
                     *, keep_output: bool = False, keep_history: bool = False,
                     visible_image_ids: set[Any] | None = None) -> None:
        if not isinstance(annotation_data, dict) or not isinstance(annotation_data.get("images", []), list):
            raise ValueError("Project data must be a JSON object with an images array")
        if not isinstance(annotation_data.get("categories", []), list):
            raise ValueError("Categories must be a JSON array")
        images = annotation_data.get("images", [])
        if any(
            not isinstance(image, dict) or "id" not in image
            or not isinstance(image.get("file_name"), str)
            for image in images
        ):
            raise ValueError("Each image entry must have an id and file_name")
        image_ids = [image["id"] for image in images]
        names: dict[str, str] = {}
        clashes = []
        for image in images:
            key = image_key(image["file_name"])
            if key in names:
                clashes.append(f"{names[key]} and {image['file_name']}")
            names.setdefault(key, image["file_name"])
        if clashes:
            raise ValueError(
                "Images are identified by file name, but this file lists different images with the same name:\n"
                + "\n".join(clashes[:20]) + ("\n…" if len(clashes) > 20 else "")
                + "\n\nRename them so every image has its own name, then open the file again."
            )
        if len(set(image_ids)) != len(image_ids):
            raise ValueError("Image IDs must be unique")
        categories = annotation_data.get("categories", [])
        if any(not isinstance(category, dict) or "id" not in category or "name" not in category
               for category in categories):
            raise ValueError("Each category entry must have an id and name")
        annotations = annotation_data.get("annotations", [])
        if not isinstance(annotations, list):
            raise ValueError("Annotations must be a JSON array")
        known_ids = set(image_ids)
        for annotation in annotations:
            if not isinstance(annotation, dict) or annotation.get("image_id") not in known_ids:
                raise ValueError("An annotation refers to an unknown image id")
        history = annotation_data.get("annotation_history", [])
        if not isinstance(history, list) or any(not isinstance(record, dict) for record in history):
            raise ValueError("annotation_history must be a JSON array of objects")
        self.view.clear_drawing_guides()
        self.draw_button.setChecked(False)
        self._cancel_image_adjustment()
        self._detail_generation += 1
        self.current_items = []  # before clear(): removing a selected box fires selectionChanged, which reads it
        self._close_history_dialog()  # its preview outline belongs to the image being cleared
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.base_image = None
        self.background_item = None
        self.detail_item = None
        self.scale_bar_item = None
        self.current_image_id = None
        self._loaded = None
        self._pending_image_id = None
        self._load_workers.clear()
        self._dataset_generation += 1
        self._leave_video_mode()
        self.videos = {}
        self.video_frame_by_image = {}
        self._listed_video_frames = set()
        self._video_positions = {}
        self.video_list.clear()
        self.video_section.setVisible(False)
        self.root = root.resolve() if root else None
        self._crop_export_settings.pop("classes", None)  # class ids belong to the previous project
        BoxItem.class_colors.clear()
        self.brightness_slider.setValue(100)
        self.contrast_slider.setValue(100)
        self.gamma_slider.setValue(100)
        self.black_point.setValue(0)
        self.white_point.setValue(255)
        self.invert_check.setChecked(False)
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(False)
        self.add_images_action.setEnabled(self.root is not None)
        self.add_folder_action.setEnabled(self.root is not None)
        self.add_video_action.setEnabled(self.root is not None)
        self.add_video_folder_action.setEnabled(self.root is not None)
        self.save_project_action.setEnabled(self.root is not None)
        self.save_project_as_action.setEnabled(self.root is not None)
        self.merge_project_action.setEnabled(self.root is not None)
        self.training_export_action.setEnabled(self.root is not None)
        self.change_annotator_action.setEnabled(self.root is not None)
        self.change_annotator_action.setText(
            f"Change User… ({self.current_annotator})"
            if self.current_annotator else "Choose User…"
        )
        self._show_user_name()
        self.visible_image_ids = visible_image_ids
        self.annotations_path = source_path.resolve() if source_path else None
        self._file_signature = None
        self._base_json = None  # set by the caller once the opened file is known
        if not keep_output:
            self.output_path = None
        self.annotation_data = annotation_data
        self.images = self.annotation_data.setdefault("images", [])
        self.categories = {
            category["id"]: category["name"]
            for category in self.annotation_data.setdefault("categories", [])
        }
        self.image_by_id = {image["id"]: image for image in self.images}
        self._next_image_id = max(
            (int(image_id) for image_id in self.image_by_id if str(image_id).isdigit()), default=0
        ) + 1
        self._rebuild_user_cache()
        self.source_paths_by_image = {}
        if self.root is not None:
            for image in self.images:
                try:
                    self.source_paths_by_image[image["id"]] = dataset_image_path(
                        self.root, image["file_name"]
                    )
                except ValueError:
                    continue
        self.annotations_by_image = {image["id"]: [] for image in self.images}
        imported_annotations = source_path is not None
        # Opened files have frame_state on every image by now (_fill_missing_frame_state).
        self.frame_verified_by_image = {
            image["id"]: imported_annotations and image.get("frame_state") == "verified"
            for image in self.images
        }
        self.review_by_image = {
            image["id"]: imported_annotations and image.get("frame_state") == "review"
            for image in self.images
        }
        self.background_by_image = {image["id"]: False for image in self.images}
        self.persisted_image_ids = set(self.image_by_id) if imported_annotations else set()
        self.touched_image_ids = set()
        self._folder_settled = set()
        self.roi_counts_by_image = {image["id"]: {} for image in self.images}
        self.roi_totals = [0, 0]
        load_count = len(self.images) + len(self.annotation_data.setdefault("annotations", []))
        progress = None
        if load_count >= 100:
            progress = QProgressDialog("Indexing images and boxes…", "Cancel", 0, load_count, self)
            progress.setWindowTitle("Loading annotations")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            progress.show()
            QApplication.processEvents()
        done = 0
        for image in self.images:
            done += 1
            if progress and done % 100 == 0:
                progress.setValue(done)
                progress.setLabelText(f"Indexing images and boxes…  {done:,} / {load_count:,}")
                QApplication.processEvents()
        for annotation in self.annotation_data["annotations"]:
            image_id = annotation.get("image_id")
            if image_id not in self.annotations_by_image:
                if progress:
                    progress.close()
                raise ValueError(f"Annotation refers to unknown image_id {image_id!r}")
            self.annotations_by_image[image_id].append(annotation)
            status_index = 0 if is_verified(annotation.get("verified", True)) else 1
            category_counts = self.roi_counts_by_image[image_id].setdefault(
                annotation.get("category_id"), [0, 0]
            )
            category_counts[status_index] += 1
            self.roi_totals[status_index] += 1
            done += 1
            if progress and done % 100 == 0:
                progress.setValue(done)
                progress.setLabelText(f"Indexing images and boxes…  {done:,} / {load_count:,}")
                QApplication.processEvents()
        if imported_annotations:
            self.background_by_image = {
                image["id"]: is_verified(image["background"])
                if "background" in image
                else (
                    self.frame_verified_by_image[image["id"]]
                    and not self.annotations_by_image[image["id"]]
                )
                for image in self.images
            }
        if progress:
            progress.setValue(load_count)
            progress.close()
        self.next_annotation_id = max(  # numeric ids only; a COCO file from elsewhere may use strings
            (annotation["id"] for annotation in self.annotation_data["annotations"]
             if isinstance(annotation.get("id"), int)), default=0
        ) + 1
        # Boxes from a plain COCO file get box ids here; they become rev 1 at the first save.
        self._purged_boxes = set()
        self._trash_on_save = set()
        self._newest_time = None
        self._viewing = None
        if hasattr(self, "restore_version_action"):
            self.restore_version_action.setEnabled(False)
        self._history_unsaved = False
        self._history_index_key = None
        self._stats_key = None
        self._highest_ids = {}
        self._box_list_last = None
        self._user_remap = {}
        self._deleted_by = {}
        self._state_by = {}
        normalize_box_ids(self.annotation_data)
        self.dirty = False
        if imported_annotations:
            # An opened file follows the Verified rule: a Verified image with an unverified box becomes Review.
            settled = self._settle_verified_states(list(self.image_by_id), credit_nobody=True)
            if settled:
                self.dirty = True
                self._load_notes.append(f"set {settled:,} Verified image(s) with an unverified box to Review")
        if not keep_history:
            self._undo_history.clear()
            self._redo_history.clear()
        # _set_dataset builds the image list itself once every filter is reset.
        self._refresh_annotator_filter(reset=True, refresh_frames=False)
        self.class_filter.blockSignals(True)
        self._fill_class_filter()
        self.class_filter.blockSignals(False)
        self.frame_search.blockSignals(True)
        self.frame_search.clear()
        self.frame_search.blockSignals(False)
        self.video_search.blockSignals(True)
        self.video_search.clear()
        self.video_search.blockSignals(False)
        self.verification_filter.blockSignals(True)
        self.verification_filter.setCurrentIndex(0)
        self.verification_filter.blockSignals(False)
        self.iou_filter_operator.blockSignals(True)
        self.iou_filter_operator.setCurrentIndex(0)  # Off
        self.iou_filter_operator.blockSignals(False)
        self.iou_filter_threshold.setEnabled(False)
        self.score_filter_operator.blockSignals(True)
        self.score_filter_operator.setCurrentIndex(0)  # Off
        self.score_filter_operator.blockSignals(False)
        self.score_filter_threshold.setEnabled(False)
        self.frame_verification_filter.blockSignals(True)
        self.frame_verification_filter.setCurrentIndex(0)
        self.frame_verification_filter.blockSignals(False)
        self.unsaved_filter_check.blockSignals(True)
        self.unsaved_filter_check.setChecked(False)
        self.unsaved_filter_check.blockSignals(False)
        self._sync_frame_controls()
        self._set_annotation_editing_enabled(self.current_annotator is not None)
        self._update_window_title()
        self._rebuild_frame_list(show_progress=True)

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

    def _shutdown_background_work(self) -> None:
        """Stop timers and wait for workers so nothing reports to a closing window."""
        for timer in (self.autosave_timer, self.detail_timer, self.adjustment_timer, self.search_timer):
            timer.stop()
        self._dataset_generation += 1
        self._adjust_generation += 1
        self._detail_generation += 1
        self._auto_level_generation += 1
        if self._adjust_cancel is not None:
            self._adjust_cancel.set()
        for pool in (self.image_pool, self.adjustment_pool):
            pool.clear()
            pool.waitForDone(3000)
        if self._video_reader is not None:
            self._video_reader.close()
        if self._autosave_thread is not None:
            self._autosave_thread.join(timeout=3)

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

    def _highest_id(self, field: str) -> int:
        """The highest ``field`` id ever used here (boxes, saved versions, undo states); found once per project
        and then kept up to date as ids are given out."""
        if field not in self._highest_ids:
            self._highest_ids[field] = max(
                (value for value in self._used_ids(field) if isinstance(value, int)), default=0)
        return self._highest_ids[field]

    def _used_ids(self, field: str) -> set[Any]:
        """Every value of ``field`` on boxes, saved versions and undo states, so a new id never reuses one."""
        return {
            record.get(field) for record in (
                *self._all_annotations(), *self._history_annotations(),
                *(self.annotation_data.get("annotation_history") or []),
            )
        }

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

    def _editing_allowed(self) -> bool:
        """Project changes are read-only until a user is chosen, and while a data version is shown."""
        return self.root is not None and self.current_annotator is not None and self._viewing is None

    def _set_annotation_editing_enabled(self, enabled: bool) -> None:
        enabled = enabled and self._viewing is None  # a data version is shown read-only
        self.view.setInteractive(enabled)
        self.undo_action.setEnabled(enabled)
        self.redo_action.setEnabled(enabled)
        project_edits = enabled and self.root is not None
        self.edit_labels_action.setEnabled(project_edits)
        self.import_classes_action.setEnabled(project_edits)
        self.edit_annotators_action.setEnabled(project_edits)
        if not enabled:
            self.draw_button.setChecked(False)
            self.view.set_draw_mode(False)
            self.edit_status.setEnabled(False)
            self._sync_frame_controls()
        else:
            self._selection_changed()
            self._sync_frame_controls()

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

    def _all_annotations(self) -> list[dict[str, Any]]:
        """Return the live annotation records, including boxes drawn this session."""
        return [
            annotation for annotations in self.annotations_by_image.values()
            for annotation in annotations
        ]

    def _usage(self, matches) -> tuple[int, int, int]:
        """(boxes, images, saved earlier versions) that ``matches`` picks out, for confirmations."""
        self._commit_current_scene()
        boxes = images = 0
        for annotations in self.annotations_by_image.values():
            count = sum(1 for annotation in annotations if matches(annotation))
            boxes += count
            images += bool(count)
        earlier = sum(1 for record in self.annotation_data.get("annotation_history") or [] if matches(record))
        return boxes, images, earlier

    @staticmethod
    def _usage_text(boxes: int, images: int, earlier: int = 0) -> str:
        text = f"{boxes:,} box(es) on {images:,} image(s)"
        return text + (f" and {earlier:,} earlier saved version(s)" if earlier else "")

    def _confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _mark_images_unsaved(self, image_ids) -> None:
        """Mark images whose boxes a project-wide change touched, without changing their state."""
        for image_id in image_ids:
            self.touched_image_ids.add(image_id)
            self._update_frame_item_text(image_id)

    def _history_annotations(self) -> list[dict[str, Any]]:
        """Return annotation records stored in undo/redo snapshots."""
        return [
            annotation
            for _image_id, state in (*self._undo_history, *self._redo_history)
            for annotation in state
        ]

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

    def _frame_state(self, image_id: Any) -> str:
        if self.frame_verified_by_image.get(image_id, False):
            return "verified"
        return "review" if self.review_by_image.get(image_id, False) else "unverified"

    def _has_work_or_verified(self, image_id: Any) -> bool:
        return self._frame_has_work(image_id) or self._frame_state(image_id) != "unverified"

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

    def _update_rulers(self, *_args) -> None:
        resolution = self.pixel_resolution.value()
        self.top_ruler.resolution = resolution
        self.left_ruler.resolution = resolution
        self.top_ruler.update()
        self.left_ruler.update()

    def _set_calibration_mode(self, enabled: bool) -> None:
        if enabled and self.base_image is None:
            self.calibrate_scale_button.setChecked(False)
            QMessageBox.information(self, "Calibrate scale", "Open an image before calibrating its scale.")
            return
        if enabled and self.draw_button.isChecked():
            self.draw_button.setChecked(False)
        self.view.set_calibration_mode(enabled)
        if enabled:
            self.status_label.setText("Draw a line across a known dimension. Press Esc to cancel.")

    def _calibrate_scale_from_line(self, pixel_length: float) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Calibrate scale")
        form = QFormLayout(dialog)
        form.addRow("Measured line", QLabel(f"{pixel_length:.2f} pixels"))
        length_input = QDoubleSpinBox()
        length_input.setRange(0.000001, 100000000.0)
        length_input.setDecimals(6)
        length_input.setValue(1.0)
        form.addRow("Known length", length_input)
        unit_input = QComboBox()
        unit_input.addItems(["µm", "mm", "cm", "m"])
        unit_input.setCurrentText("mm")
        form.addRow("Unit", unit_input)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.status_label.setText("Scale calibration canceled.")
            return
        known_length = length_input.value()
        unit = unit_input.currentText()
        micrometres_per_unit = {"µm": 1.0, "mm": 1000.0, "cm": 10000.0, "m": 1000000.0}
        resolution = known_length * micrometres_per_unit[unit] / pixel_length
        self.pixel_resolution.setValue(resolution)
        self.status_label.setText(
            f"Scale calibrated: {resolution:.3f} µm/px from {pixel_length:.2f} pixels = "
            f"{known_length:g} {unit}. You can adjust Pixel resolution manually."
        )

    def _set_ruler_visible(self, visible: bool) -> None:
        self.coordinate_readout.setVisible(visible)
        self.top_ruler.setVisible(visible)
        self.left_ruler.setVisible(visible)

    def _frame_selection_changed(self, current, _previous) -> None:
        if current is not None and not self._refreshing:
            self._leave_video_mode()  # choosing an image puts the arrow keys back on the image list
            self._load_frame(current.data(Qt.ItemDataRole.UserRole))

    # ---------- keyboard shortcuts ----------

    def _load_shortcuts(self) -> None:
        """The single-key shortcuts: the defaults, with the changes saved on this PC laid over them."""
        self.shortcuts = {name: default for name, _what, default in SHORTCUTS}
        try:
            saved = json.loads(str(QSettings("StingrayLabeler", "StingrayLabeler").value("shortcuts", "{}")))
        except ValueError:
            saved = {}
        if isinstance(saved, dict):
            self.shortcuts.update({name: QKeySequence(keys).toString() for name, keys in saved.items()
                                   if name in self.shortcuts and isinstance(keys, str)})
        self._shortcut_names = {QKeySequence(keys).toString(): name for name, keys in self.shortcuts.items() if keys}

    def _key(self, name: str) -> str:
        """A shortcut's key as shown in menus and tips ("" when it has none)."""
        return QKeySequence(self.shortcuts[name]).toString(QKeySequence.SequenceFormat.NativeText)

    @staticmethod
    def _event_keys(event) -> str:
        """The key pressed, in the form shortcuts are kept in (keypad keys count as the plain keys)."""
        combination = event.keyCombination()
        modifiers = combination.keyboardModifiers() & ~Qt.KeyboardModifier.KeypadModifier
        return QKeySequence(QKeyCombination(modifiers, combination.key())).toString()

    def _show_shortcut_keys(self) -> None:
        """Name the current keys in the menus and tips that mention them."""
        def tip(text: str, *names: str) -> str:
            keys = " / ".join(key for key in (self._key(name) for name in names) if key)
            return f"{text} ({keys})" if keys else text

        def menu_text(text: str, name: str) -> str:
            return f"{text}\t{self._key(name)}" if self._key(name) else text

        self.change_category_action.setText(tip("Change Category of Selected Box…", "change_category"))
        self.frame_back_button.setToolTip(tip("Previous frame", "previous"))
        self.frame_forward_button.setToolTip(tip("Next frame", "next"))
        self.frame_box.setToolTip(
            tip("Type a frame number and press Enter", "go_to_frame") + ". "
            + tip("±1 frame", "previous", "next") + ", " + tip("±10", "back_10", "forward_10") + ", "
            "Home/End: first/last, " + tip("previous/next frame with work", "previous_unverified", "next_unverified")
            + ", Esc: back to the image"
        )
        self.auto_levels_button.setToolTip(
            tip("Estimate black and white points from this image only when requested; press again to go back "
                "to the default levels", "auto_levels"))
        self.reset_image_action.setText(menu_text("Reset Image", "reset_image"))
        self.state_buttons["unverified"].setToolTip(
            "Not reviewed. Saved when it holds boxes or other work; an empty one is left out.")
        self.state_buttons["review"].setToolTip(
            tip("Worth another look", "image_review") + ". Set automatically on the first edit of an unverified image.")
        self.state_buttons["verified"].setToolTip(
            tip("Confident in the boxes", "image_verified") + ". Needs every box verified, or a background mark.")
        self.draw_button.setToolTip(tip("Draw a box", "draw") + "; press Esc to cancel")
        self.repeat_category_check.setToolTip(
            "New boxes get the category of the last box drawn, without asking"
            + (f" ({self._key('change_category')} changes a box's category)" if self._key("change_category") else ""))
        self.edit_status.setToolTip(tip("Toggle verification for the selected box", "verify_box"))
        self.favorites_only_action.setToolTip(tip("List only images marked as favorites", "favorite"))
        for name, (action, text) in self._navigation_actions.items():
            action.setText(menu_text(text, name))

    def _edit_shortcuts(self) -> None:
        """Edit → Keyboard Shortcuts…: change the single-key shortcuts; kept on this PC, for every project."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Keyboard Shortcuts")
        layout = QVBoxLayout(dialog)
        note = QLabel("Click a key and press the new one. Shortcuts are kept on this PC. Arrows, Home / End, "
                      "Delete, Esc, Enter and Space cannot be changed.")
        note.setWordWrap(True)
        layout.addWidget(note)
        table = QTableWidget(len(SHORTCUTS), 2)
        table.setHorizontalHeaderLabels(["Action", "Key"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        editors: dict[str, QKeySequenceEdit] = {}
        for row, (name, what, _default) in enumerate(SHORTCUTS):
            table.setItem(row, 0, QTableWidgetItem(what))
            editor = QKeySequenceEdit(QKeySequence(self.shortcuts[name]))
            editor.setMaximumSequenceLength(1)
            editor.setClearButtonEnabled(True)
            table.setCellWidget(row, 1, editor)
            editors[name] = editor
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
                                   | QDialogButtonBox.StandardButton.RestoreDefaults)

        def restore_defaults() -> None:
            for name, _what, default in SHORTCUTS:
                editors[name].setKeySequence(QKeySequence(default))

        def save() -> None:
            chosen = {name: editor.keySequence().toString() for name, editor in editors.items()}
            menu_keys = {sequence.toString() for action in self.findChildren(QAction) for sequence in action.shortcuts()}
            what = {name: text for name, text, _default in SHORTCUTS}
            used: dict[str, str] = {}
            for name, keys in chosen.items():
                if not keys:
                    continue
                if keys in FIXED_KEYS or keys in menu_keys:
                    QMessageBox.information(dialog, "Keyboard Shortcuts",
                                            f"{keys} is already used by the app; choose another key for "
                                            f"“{what[name]}”.")
                    return
                if keys in used:
                    QMessageBox.information(dialog, "Keyboard Shortcuts",
                                            f"{keys} is set for both “{what[used[keys]]}” and “{what[name]}”.")
                    return
                used[keys] = name
            changed = {name: keys for name, keys in chosen.items() if keys != QKeySequence(
                next(default for other, _what, default in SHORTCUTS if other == name)).toString()}
            QSettings("StingrayLabeler", "StingrayLabeler").setValue("shortcuts", json.dumps(changed))
            self._load_shortcuts()
            self._show_shortcut_keys()
            dialog.accept()

        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(restore_defaults)
        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(560, 600)
        dialog.exec()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if (event.type() not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
                or QApplication.activeWindow() is not self):
            return super().eventFilter(watched, event)
        shortcut = self._shortcut_names.get(self._event_keys(event))
        # Automatic black/white levels for this image; again: back to the default levels.
        if (shortcut == "auto_levels"
                and not isinstance(QApplication.focusWidget(),
                                   (QLineEdit, QComboBox, QSlider, QDoubleSpinBox, QSpinBox, QKeySequenceEdit))):
            if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
                self._toggle_auto_levels()
            return True
        if event.type() == QEvent.Type.KeyPress:
            focus = QApplication.focusWidget()

            def focus_within(widget: QWidget) -> bool:
                return focus is widget or (focus is not None and widget.isAncestorOf(focus))

            if focus_within(self.frame_box):
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self.frame_box.interpretText()  # jumps to the typed frame
                    self.view.setFocus()
                    return True
                if event.key() == Qt.Key.Key_Escape:
                    self.frame_box.blockSignals(True)
                    self.frame_box.setValue(self._video_positions.get(
                        path_key(self._active_video.path), 0) if self._active_video else 0)
                    self.frame_box.blockSignals(False)
                    self.view.setFocus()
                    return True
            if isinstance(focus, (QLineEdit, QComboBox, QSlider, QDoubleSpinBox, QSpinBox, QKeySequenceEdit)):
                return super().eventFilter(watched, event)
            in_annotation_ui = focus_within(self.centralWidget())
            video = self._active_video is not None
            # Left hand on the keyboard, right hand on the mouse; the keys are set in Edit → Keyboard Shortcuts….
            if in_annotation_ui and shortcut is not None:
                has_image = self.current_image_id in self.image_by_id
                if shortcut in ("previous", "next"):
                    step = -1 if shortcut == "previous" else 1
                    self._step_video_frame(step) if video else self._navigate_frames(step)
                    return True
                if shortcut in ("previous_unverified", "next_unverified"):
                    step = -1 if shortcut == "previous_unverified" else 1
                    self._jump_to_video_work(step) if video else self._jump_to_unverified(step)
                    return True
                if shortcut in ("previous_box", "next_box"):
                    self._step_box(-1 if shortcut == "previous_box" else 1)
                    return True
                if shortcut == "favorite":
                    self._toggle_favorite()
                    return True
                if shortcut in ("back_10", "forward_10") and video:
                    self._step_video_frame(-10 if shortcut == "back_10" else 10)
                    return True
                if shortcut == "reset_image":
                    self._reset_image_view()
                    return True
                if shortcut == "image_verified" and has_image:
                    verified = self._frame_state(self.current_image_id) == "verified"
                    self._set_frame_state("review" if verified else "verified")
                    return True
                if shortcut == "draw" and self.draw_button.isEnabled():
                    self.draw_button.toggle()
                    return True
                if shortcut == "verify_box" and self.edit_status.isEnabled():
                    self.edit_status.toggle()
                    return True
                if shortcut == "change_category" and self.change_category_action.isEnabled():
                    self._change_category()
                    return True
                if shortcut == "image_review" and has_image:
                    review = self._frame_state(self.current_image_id) == "review"
                    self._set_frame_state("unverified" if review else "review")
                    return True
                if shortcut == "delete_box" and self._selected_item() is not None:
                    self._delete_selected()
                    return True
            if video and shortcut == "go_to_frame":
                self.frame_box.setFocus()
                self.frame_box.selectAll()
                return True
            if video and self._video_key(event):
                return True
            if event.modifiers() == Qt.KeyboardModifier.NoModifier:
                if event.key() == Qt.Key.Key_Left:
                    self._navigate_frames(-1)
                    return True
                if event.key() == Qt.Key.Key_Right:
                    self._navigate_frames(1)
                    return True
                if in_annotation_ui and event.key() == Qt.Key.Key_Escape and self.draw_button.isChecked():
                    self.draw_button.setChecked(False)
                    return True
                if in_annotation_ui and event.key() == Qt.Key.Key_Delete and self._selected_item() is not None:
                    self._delete_selected()
                    return True
        return super().eventFilter(watched, event)

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

    def _brightness_changed(self, value: int) -> None:
        self.brightness_value.setText(f"{value}%")
        self.adjustment_timer.start()

    def _contrast_changed(self, value: int) -> None:
        self.contrast_value.setText(f"{value}%")
        self.adjustment_timer.start()

    def _gamma_changed(self, value: int) -> None:
        self.gamma_value.setText(f"{value / 100:.2f}")
        self.adjustment_timer.start()

    def _levels_changed(self, value: int) -> None:
        if self.sender() is self.black_point and value >= self.white_point.value():
            self.white_point.setValue(min(255, value + 1))
        elif self.sender() is self.white_point and value <= self.black_point.value():
            self.black_point.setValue(max(0, value - 1))
        self.adjustment_timer.start()

    def _adjustment_changed(self, _checked: bool) -> None:
        self.adjustment_timer.start()

    def _auto_levels_requested(self) -> None:
        if self._loaded is None:
            return
        self._auto_level_generation += 1
        generation = self._auto_level_generation
        worker = AutoLevelsWorker(generation, self._loaded.preview, self.pipeline_signals)
        self._auto_level_workers[generation] = worker
        self.auto_levels_button.setEnabled(False)
        self.status_label.setText("Estimating levels for current image…")
        self.adjustment_pool.start(worker)

    def _toggle_auto_levels(self) -> None:
        """L: with the default levels (black 0, white 255), set automatic black/white levels for this image;
        with any other levels, go back to the defaults."""
        if (self.black_point.value(), self.white_point.value()) != (0, 255):
            self._set_levels(0, 255)
            self.status_label.setText("Levels back to the defaults (black 0, white 255)")
            return
        if self._loaded is None:
            return
        black, white = auto_levels(self._loaded.preview.convert("L").histogram())
        self._set_levels(black, white)
        again = f" ({self._key('auto_levels')} again: defaults)" if self._key("auto_levels") else ""
        self.status_label.setText(f"Automatic levels for this image: black {black}, white {white}{again}")

    def _set_levels(self, black: int, white: int) -> None:
        for box in (self.black_point, self.white_point):
            box.blockSignals(True)
        self.black_point.setValue(black)
        self.white_point.setValue(white)
        for box in (self.black_point, self.white_point):
            box.blockSignals(False)
        self.adjustment_timer.start()

    def _auto_levels_finished(self, generation: int, black: int, white: int) -> None:
        self._auto_level_workers.pop(generation, None)
        if generation != self._auto_level_generation:
            return
        self.white_point.setValue(white)
        self.black_point.setValue(black)
        self.auto_levels_button.setEnabled(self._loaded is not None)
        self.status_label.setText(f"Auto levels set for this image: black {black}, white {white}.")

    def _current_adjustments(self) -> Adjustments:
        return Adjustments(
            self.brightness_slider.value() / 100,
            self.contrast_slider.value() / 100,
            self.gamma_slider.value() / 100,
            self.invert_check.isChecked(),
            self.black_point.value(),
            self.white_point.value(),
        )

    def _update_display_image(self) -> None:
        """Re-adjust the cached preview (no resize) and the zoomed-in detail patch."""
        if self._loaded is None or self.background_item is None:
            return
        if self._adjust_cancel is not None:
            self._adjust_cancel.set()
        self._adjust_generation += 1
        generation = self._adjust_generation
        cancelled = threading.Event()
        self._adjust_cancel = cancelled
        worker = PreviewAdjustmentWorker(
            generation, self._loaded, self._current_adjustments(), cancelled, self.pipeline_signals
        )
        self._adjust_workers[generation] = worker
        self.adjustment_pool.start(worker)
        self.detail_timer.start()

    def _apply_display_adjustments(self, image: Image.Image) -> Image.Image:
        return apply_image_adjustments(image, self._current_adjustments())

    def _adjustment_finished(self, generation: int, image: QImage | None) -> None:
        self._adjust_workers.pop(generation, None)
        if (generation != self._adjust_generation or image is None
                or self.background_item is None or self._loaded is None):
            return
        self.background_item.setPixmap(QPixmap.fromImage(image))
        self.background_item.setScale(self._loaded.preview_scale)

    def _update_detail(self) -> None:
        """Show full-resolution pixels for the visible area once the preview is magnified."""
        self._detail_generation += 1
        loaded, detail = self._loaded, self.detail_item
        if loaded is None or detail is None:
            return
        zoom = abs(self.view.transform().m11())
        if loaded.preview_scale <= 1.0 or zoom * loaded.preview_scale <= 1.0:
            detail.hide()
            return
        visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        area = visible.intersected(QRectF(0, 0, loaded.full.width, loaded.full.height))
        if area.isEmpty():
            detail.hide()
            return
        pad_x, pad_y = area.width() * 0.25, area.height() * 0.25
        box = (
            max(0, math.floor(area.left() - pad_x)),
            max(0, math.floor(area.top() - pad_y)),
            min(loaded.full.width, math.ceil(area.right() + pad_x)),
            min(loaded.full.height, math.ceil(area.bottom() + pad_y)),
        )
        generation = self._detail_generation
        worker = DetailWorker(generation, loaded, box, self._current_adjustments(), self.pipeline_signals)
        self._detail_workers[generation] = worker
        self.adjustment_pool.start(worker)

    def _detail_finished(self, generation: int, image: QImage, left: int, top: int) -> None:
        self._detail_workers.pop(generation, None)
        if generation != self._detail_generation or self.detail_item is None:
            return
        self.detail_item.setPixmap(QPixmap.fromImage(image))
        self.detail_item.setPos(left, top)
        self.detail_item.show()

    def _update_scale_bar(self, *_args) -> None:
        if self.scale_bar_item is None:
            return
        self.scale_bar_item.visible = self.show_scale_bar.isChecked()
        self.scale_bar_item.pixel_resolution = self.pixel_resolution.value()
        self.scale_bar_item.length_mm = self.scale_bar_length.value()
        self.scale_bar_item.update()

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

    def _frame_is_exportable(self, image_id: Any) -> bool:
        return self.frame_verified_by_image.get(image_id, False)

    def _frame_has_work(self, image_id: Any) -> bool:
        return bool(
            self.annotations_by_image.get(image_id)
            or self.background_by_image.get(image_id, False)
            or self.image_by_id.get(image_id, {}).get("favorite")
        )

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

    def _save_first(self, what: str) -> bool:
        """``what`` works on saved history, so unsaved changes are saved first (the user is asked)."""
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Save first", f"{what} works on saved work, and you have unsaved changes. Save them first?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save,
        )
        return answer == QMessageBox.StandardButton.Save and self.save_project()

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

    def _request_image(self, image_id: Any) -> None:
        """Start decoding on a worker unless it is cached or already loading."""
        key = (self._dataset_generation, image_id)
        if key in self._load_workers:
            return
        path = self._image_path(image_id)
        if path is None:
            return
        video_frame = self._video_source(image_id)
        if video_frame is not None:
            video, index = video_frame
            reader = self._video_reader
            if reader is None or reader.info != video:
                reader = AviReader(video)  # opens the file just for this one read
            worker = ImageLoadWorker(
                key, path, self.pipeline_signals, reader, index,
                still_wanted=lambda: key == (self._dataset_generation, self._pending_image_id),
            )
        else:
            worker = ImageLoadWorker(key, path, self.pipeline_signals)
        self._load_workers[key] = worker
        self.image_pool.start(worker)

    def _image_skipped(self, key: tuple[int, Any]) -> None:
        self._load_workers.pop(key, None)
        # The frame became wanted again after its worker gave up; start it afresh.
        if key == (self._dataset_generation, self._pending_image_id):
            self._request_image(key[1])

    def _image_loaded(self, key: tuple[int, Any], loaded: LoadedImage) -> None:
        self._load_workers.pop(key, None)
        generation, image_id = key
        # Only the image the user selected is shown; anything else that finishes late is dropped.
        if generation == self._dataset_generation and image_id == self._pending_image_id:
            self._show_image(image_id, loaded)

    def _image_failed(self, key: tuple[int, Any], message: str) -> None:
        self._load_workers.pop(key, None)
        generation, image_id = key
        if generation != self._dataset_generation or image_id != self._pending_image_id:
            return
        self._pending_image_id = None
        self.image_info.setText("Image could not be loaded")
        QMessageBox.critical(self, "Cannot load image", message)

    def _load_frame(self, image_id: Any) -> None:
        if image_id in (self.current_image_id, self._pending_image_id):
            return
        # Undo covers the image on screen only: leaving it ends its undo steps (data versions cover the rest).
        self._undo_history.clear()
        self._redo_history.clear()
        previous_id = self._pending_image_id if self._pending_image_id is not None else self.current_image_id
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(False)
        if self.view.calibration_mode:
            self.calibrate_scale_button.setChecked(False)
        self._commit_current_scene()
        self.view.clear_drawing_guides()
        self._cancel_image_adjustment()
        self._detail_generation += 1
        self.current_items = []  # before clear(): removing a selected box fires selectionChanged, which reads it
        self._close_history_dialog()  # its preview outline belongs to the image being cleared
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.scale_bar_item = None
        self.background_item = None
        self.detail_item = None
        self.base_image = None
        self._loaded = None
        self.current_image_id = None
        self.annotation_list.clear()
        self.box_list_header.hide()
        if self._image_path(image_id) is None:
            QMessageBox.critical(
                self, "Invalid image path",
                f"Cannot resolve the image file for {self.image_by_id[image_id]['file_name']!r}.",
            )
            self._pending_image_id = None
            self._sync_frame_controls()
            return
        self._pending_image_id = image_id
        self._sync_frame_controls()
        name = self._display_name(image_id)
        self.image_name_label.setText(name)
        self.image_info.setText(f"Loading {name}…")
        self._request_image(image_id)
        if previous_id is not None and previous_id != image_id:
            self._drop_unused_video_frame(previous_id)

    def _show_image(self, image_id: Any, loaded: LoadedImage) -> None:
        self._pending_image_id = None
        self._loaded = loaded
        self.base_image = loaded.full
        self.auto_levels_button.setEnabled(True)
        image = self.image_by_id[image_id]
        width, height = loaded.full.size
        image["width"] = width
        image["height"] = height
        self.background_item = QGraphicsPixmapItem()
        self.background_item.setZValue(-10)
        self.background_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.background_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene.addItem(self.background_item)
        self.detail_item = QGraphicsPixmapItem()
        self.detail_item.setZValue(-9)
        self.detail_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.detail_item.hide()
        self.scene.addItem(self.detail_item)
        self.scene.setSceneRect(QRectF(0, 0, width, height))
        self.scale_bar_item = ScaleBarItem(width, height)
        self.scene.addItem(self.scale_bar_item)
        self._update_scale_bar()
        if self._current_adjustments().is_identity:
            self.background_item.setPixmap(QPixmap.fromImage(loaded.preview_qimage))
            self.background_item.setScale(loaded.preview_scale)
        else:
            # Leave the view blank for the few ms the adjusted preview takes, rather than flash raw pixels.
            self._update_display_image()
        self.current_image_id = image_id
        self._add_box_items(image_id)
        self._refresh_annotation_list()
        self.view._manual_zoom = False
        self.view.resetTransform()
        self.view.fit_image()
        self._update_rulers()
        self._update_image_info()
        self._sync_frame_controls()
        self._selection_changed()
        self._update_image_info()  # an image switch changes no counts

    def _clear_scene(self) -> None:
        if self.view.calibration_mode:
            self.calibrate_scale_button.setChecked(False)
        self._commit_current_scene()
        previous_id = self._pending_image_id if self._pending_image_id is not None else self.current_image_id
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(False)
        self.view.clear_drawing_guides()
        self._cancel_image_adjustment()
        self._detail_generation += 1
        self.current_items = []  # before clear(): removing a selected box fires selectionChanged, which reads it
        self._close_history_dialog()  # its preview outline belongs to the image being cleared
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.base_image = None
        self._loaded = None
        self.background_item = None
        self.detail_item = None
        self.scale_bar_item = None
        self.current_image_id = None
        self._pending_image_id = None
        self.edit_status.setEnabled(False)
        self.annotation_list.clear()
        self.box_list_header.hide()
        if previous_id is not None:
            self._drop_unused_video_frame(previous_id)
        self._sync_frame_controls()
        self._update_rulers()
        self._update_image_info()

    def _cancel_image_adjustment(self) -> None:
        self._adjust_generation += 1
        if self._adjust_cancel is not None:
            self._adjust_cancel.set()
            self._adjust_cancel = None

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

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value
        if value:
            self._edit_count = getattr(self, "_edit_count", 0) + 1
        self._update_window_title()

    def _update_window_title(self) -> None:
        """The window title names the project and its latest data version, and says when it has changes
        not saved yet."""
        root = getattr(self, "root", None)
        title = "Stingray Labeler" + (f" — {root}" if root else "")
        data = getattr(self, "annotation_data", None)
        versions = (data or {}).get("dataset_versions") if isinstance(data, dict) else None
        if getattr(self, "_viewing", None):
            title += f" — Data version: {self._viewing['tag'].get('name')} (read-only)"
        elif versions:
            latest = max(versions, key=lambda tag: str(tag.get("time")))
            title += f" — Data version: {latest.get('name')}"
        self.setWindowTitle(title + ("*" if getattr(self, "_dirty", False) else ""))

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

    def _mark_dirty(self, *, counts_changed: bool = False, touch_image: bool = True) -> None:
        self.dirty = True
        if touch_image and self.current_image_id is not None:
            self._touch(self.current_image_id)
            # An edit can only change whether this one image matches the filters.
            self._commit_current_scene()
            self._update_frame_visibility(self.current_image_id)
        self.status_label.setText("Unsaved annotation edits")
        if counts_changed:
            self._refresh_current_roi_counts()
        # One edited image updates its share of the counts line; a project-wide change counts again.
        self._update_frame_stats(self.current_image_id if touch_image else None)  # also refreshes the info line

    def _touch(self, image_id: Any) -> None:
        """Record an edit; the first edit of an unverified image marks it Review, so it is saved."""
        self.touched_image_ids.add(image_id)
        self._update_frame_item_text(image_id)
        if self._frame_state(image_id) == "unverified":
            self.review_by_image[image_id] = True
            self._note_state_change(image_id)
            if image_id == self.current_image_id:
                self._sync_frame_controls()

    def _annotation_changed(
        self, annotation: dict[str, Any], *, counts_changed: bool = False,
    ) -> None:
        if self.current_annotator:
            self._set_annotation_annotator(annotation)
        self._mark_dirty(counts_changed=counts_changed)
        self._refresh_annotation_list(self._selected_item())

    def _sync_frame_controls(self) -> None:
        image_id = self.current_image_id
        active = image_id in self.image_by_id
        has_rois = bool(self.current_items) if active else False
        can_edit = active and self.current_annotator is not None
        state = self._frame_state(image_id) if active else None
        self.state_group.setExclusive(False)  # so that no button is on when there is no image
        for button_state, button in self.state_buttons.items():
            button.setChecked(button_state == state)
            button.setEnabled(can_edit)
        self.state_group.setExclusive(True)
        # Verifying needs every box verified or a background mark; leaving Verified is always allowed.
        self.state_buttons["verified"].setEnabled(
            can_edit and (state == "verified" or self._has_verification_basis(image_id))
        )
        self.background_check.blockSignals(True)
        self.background_check.setChecked(
            self.background_by_image.get(image_id, False) if active else False
        )
        self.background_check.setVisible(True)
        self.background_check.setEnabled(can_edit and not has_rois)
        self.background_check.blockSignals(False)
        can_draw = (
            can_edit  # with no categories yet, the first box's category is typed in when it is drawn
            and not self.background_by_image.get(image_id, False)
        )
        self.draw_button.setEnabled(can_draw)
        if not can_draw and self.draw_button.isChecked():
            self.draw_button.setChecked(False)

    def _has_verification_basis(self, image_id: Any) -> bool:
        """An image can be verified only when it is marked background, or has boxes and every one is verified."""
        if self.background_by_image.get(image_id, False):
            return True
        annotations = (
            [item.annotation for item in self.current_items]
            if image_id == self.current_image_id else self.annotations_by_image.get(image_id, [])
        )
        return bool(annotations) and all(is_verified(annotation.get("verified", True)) for annotation in annotations)

    def _settle_verified_states(self, image_ids, credit_nobody: bool) -> int:
        """Set to Review every listed Verified image that breaks the rule (opened files, merges, restored
        versions), read from annotations_by_image; the caller refreshes filters and controls.
        ``credit_nobody``: the save records the change with no user, since no one decided it.
        Returns how many images changed."""
        settled = 0
        for image_id in image_ids:
            if not self.frame_verified_by_image.get(image_id, False) or self.background_by_image.get(image_id, False):
                continue
            annotations = self.annotations_by_image.get(image_id, [])
            if annotations and all(is_verified(annotation.get("verified", True)) for annotation in annotations):
                continue
            self.frame_verified_by_image[image_id] = False
            self.review_by_image[image_id] = True
            self.touched_image_ids.add(image_id)
            if credit_nobody:
                self._state_by[image_key(self.image_by_id[image_id]["file_name"])] = None
            self._update_frame_item_text(image_id)
            settled += 1
        return settled

    def _enforce_image_verification(self, image_id: Any) -> None:
        """Drop image verification once its basis is gone (last verified box removed, background cleared)."""
        if self.frame_verified_by_image.get(image_id, False) and not self._has_verification_basis(image_id):
            self.frame_verified_by_image[image_id] = False
            self.review_by_image[image_id] = True  # it still holds work, so it stays saved
            self.touched_image_ids.add(image_id)
            self._update_frame_item_text(image_id)
            self.dirty = True
            self._update_frame_visibility(image_id)
            self.status_label.setText(
                "Image set back to Review: it has an unverified box, or no boxes and no background mark."
            )
        if image_id == self.current_image_id:
            self._sync_frame_controls()

    def _set_frame_state(self, state: str) -> None:
        """Set the current image to verified, review or unverified (buttons and their shortcut keys)."""
        image_id = self.current_image_id
        if image_id not in self.image_by_id:
            return
        if not self._editing_allowed() or state == self._frame_state(image_id):
            self._sync_frame_controls()
            return
        if state == "verified" and not self._has_verification_basis(image_id):
            self._sync_frame_controls()
            self.status_label.setText(
                "To verify this image, verify every box"
                + (f" ({self._key('verify_box')})" if self._key("verify_box") else "")
                + ", or mark it as background if it has none."
            )
            return
        self.frame_verified_by_image[image_id] = state == "verified"
        self.review_by_image[image_id] = state == "review"
        dropped = self._drop_reviewed_scores(push_undo=True) if state == "verified" else 0
        if dropped:
            self._refresh_annotation_list(self._selected_item())
        self._note_state_change(image_id)
        self.touched_image_ids.add(image_id)
        self._update_frame_item_text(image_id)
        self.dirty = True
        # An Unverified image stays in the project when it holds anything; only an empty one is left out at Save.
        kept = (self._frame_has_work(image_id) or image_id in self._history_index()[0]
                or image_key(self.image_by_id[image_id]["file_name"]) in self._saved_state_keys)
        self.status_label.setText(
            ("Image set to Unverified — unsaved changes" if kept else "Image set to Unverified — left out at Save")
            if state == "unverified" else f"Image set to {state.capitalize()} — unsaved changes"
            + (f"; model score removed from {dropped:,} verified box{'' if dropped == 1 else 'es'}"
               if dropped else "")
        )
        self._sync_frame_controls()
        self._update_frame_visibility(image_id)
        self._update_frame_stats(image_id)

    def _set_listed_frame_state(self, state: str) -> None:
        """Data → Set State of Listed Images: set every image the list shows (its filters applied) to one
        state, by the same rules as setting them one by one. An image that cannot be Verified (an unverified
        box, or no boxes and no background mark) is left as it is. Verifying removes the model score from the
        image's verified boxes, as it does for a single image."""
        if not self._editing_allowed():
            return
        self._commit_current_scene()
        listed = [image_id for image_id in self._shown_ids if self._frame_state(image_id) != state]
        if state == "verified":
            changing = [image_id for image_id in listed if self._has_verification_basis(image_id)]
        else:
            changing = listed
        left = len(listed) - len(changing)
        if not changing:
            QMessageBox.information(self, "Set State of Listed Images",
                                    f"No listed image can be set to {state.capitalize()}."
                                    + (f"\n\n{left:,} cannot be verified: they have an unverified box, "
                                       "or no boxes and no background mark." if left else ""))
            return
        answer = QMessageBox.question(
            self, "Set State of Listed Images",
            f"Set {len(changing):,} listed image(s) to {state.capitalize()}?"
            + (f"\n\n{left:,} other listed image(s) cannot be verified and stay as they are: they have an "
               "unverified box, or no boxes and no background mark." if left else ""),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        dropped = 0
        for image_id in changing:
            self.frame_verified_by_image[image_id] = state == "verified"
            self.review_by_image[image_id] = state == "review"
            if state == "verified":
                for annotation in self.annotations_by_image.get(image_id, []):
                    if "score" in annotation and is_verified(annotation.get("verified", True)):
                        # A new version by whoever verified; the scored one stays in history at save.
                        del annotation["score"]
                        self._set_annotation_annotator(annotation)
                        dropped += 1
            self._note_state_change(image_id)
            self.touched_image_ids.add(image_id)
            self._update_frame_item_text(image_id)
        self.dirty = True
        if self.current_image_id in changing:
            self._refresh_annotation_list(self._selected_item())
        self._sync_frame_controls()
        self._apply_frame_filters(select_first=False)
        self.status_label.setText(
            f"{len(changing):,} image(s) set to {state.capitalize()} — unsaved changes"
            + (f"; model score removed from {dropped:,} verified box{'' if dropped == 1 else 'es'}" if dropped else "")
            + (f"; {left:,} could not be verified" if left else "")
        )

    def _note_state_change(self, image_id: Any) -> None:
        """Remember who changed an image's state or background mark; the save credits them."""
        if self.current_annotator and image_id in self.image_by_id:
            self._state_by[image_key(self.image_by_id[image_id]["file_name"])] = self._annotator_id(self.current_annotator)

    def _background_changed(self, checked: bool) -> None:
        image_id = self.current_image_id
        if image_id not in self.image_by_id or self.current_items:
            return
        if not self._editing_allowed():
            self._sync_frame_controls()
            return
        if self.background_by_image.get(image_id, False) == checked:
            return
        self.background_by_image[image_id] = checked
        self._note_state_change(image_id)
        self._touch(image_id)
        if checked:
            self.draw_button.setChecked(False)
        self.dirty = True
        self.status_label.setText("Background status changed — unsaved changes")
        self._sync_frame_controls()
        self._update_frame_visibility(image_id)
        self._enforce_image_verification(image_id)
        self._update_frame_stats(image_id)

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

    @staticmethod
    def _best_ious(boxes: list[list[float]]) -> list[float]:
        """Each box's highest IoU with another box, comparing only boxes whose spans cross (sorted by x)."""
        best = [0.0] * len(boxes)
        order = sorted(range(len(boxes)), key=lambda index: boxes[index][0])
        for position, index in enumerate(order):
            right = boxes[index][0] + boxes[index][2]
            for other in order[position + 1:]:
                if boxes[other][0] >= right:
                    break
                value = ImageAnnotator._box_iou(boxes[index], boxes[other])
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

    def _choose_selection_color(self) -> None:
        color = QColorDialog.getColor(
            BoxItem.selection_color, self, "Selected box outline color"
        )
        if not color.isValid():
            return
        BoxItem.selection_color = color
        self.view.draw_color = color
        for item in self.scene.items():
            if isinstance(item, BoxItem):
                item.update()
        self.view.viewport().update()

    def _reset_image_view(self) -> None:
        self.brightness_slider.setValue(100)
        self.contrast_slider.setValue(100)
        self.gamma_slider.setValue(100)
        self.black_point.setValue(0)
        self.white_point.setValue(255)
        self.invert_check.setChecked(False)
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(self.base_image is not None)
        self.view._manual_zoom = False
        self.view.resetTransform()
        self.view.fit_image()
        self.view.horizontalScrollBar().setValue(0)
        self.view.verticalScrollBar().setValue(0)
        self._update_rulers()

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

    def _ensure_saved_cache(self) -> None:
        """Index the boxes of the last saved file (the merge base); redone only when that file changes."""
        if self._saved_cache_source is self._base_json:
            return
        try:
            base = json.loads(zlib.decompress(self._base_json)) if self._base_json else None
        except (ValueError, zlib.error):
            base = None
        self._set_saved_cache(base, self._base_json)

    @staticmethod
    def _normalize_user_ids(data: dict[str, Any]) -> None:
        """User ids as one kind of key throughout a file, in place: a number written as text ("3") becomes 3 on
        users, boxes, versions and image states alike, so every record still matches its user."""
        key = ImageAnnotator._user_key
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

    @staticmethod
    def _saved_index(base: dict[str, Any] | None, copy: bool = True) -> tuple:
        """The index of a saved project the "*" markers, grey rows and the next save use; it touches nothing
        else, so it can be built on another thread. User ids in ``base`` are normalized first, in place (see
        _normalize_user_ids). With ``copy`` False the index shares ``base``'s boxes, for data nothing changes
        afterwards (what a save just wrote)."""
        if isinstance(base, dict):
            ImageAnnotator._normalize_user_ids(base)
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

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.dirty:
            answer = QMessageBox.question(
                self,
                "Unsaved changes",
                "Save annotation changes before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.StandardButton.Save:
                self.save_project()
                if self.dirty:
                    event.ignore()
                    return
            else:
                self._discard_autosave()
        self._shutdown_background_work()
        event.accept()
