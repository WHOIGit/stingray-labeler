"""Main image annotation window and user workflows."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

try:
    from PySide6.QtCore import QRectF, Qt, QThreadPool, QTimer
    from PySide6.QtGui import QAction, QIcon, QKeySequence, QPalette
    from PySide6.QtWidgets import (
        QApplication, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QDialog, QDialogButtonBox,
        QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
        QListWidget, QListWidgetItem, QMenu, QMainWindow, QMessageBox, QPushButton, QProgressDialog, QRadioButton,
        QSlider, QSpinBox, QSplitter, QTextBrowser, QToolButton, QVBoxLayout, QWidget, QWidgetAction,
    )
    from PIL import Image
except ImportError as error:
    raise SystemExit("Install the GUI dependencies with: python -m pip install PySide6 Pillow") from error

from .avi import AviInfo, AviReader
from .dataset import is_verified, dataset_image_path
from .history import normalize_box_ids
from .merge import FRAME_STATES, image_key
from .graphics import AnnotationView, BoxItem, ImageRuler, ScaleBarItem
from .image_processing import (
    AutoLevelsWorker, DetailWorker, ImageLoadWorker, LoadedImage, PipelineSignals, PreviewAdjustmentWorker,
)
from .shortcuts import ShortcutsMixin
from .users import UsersMixin
from .project_io import ProjectFilesMixin
from .merging import MergingMixin
from .data_versions import DataVersionsMixin
from .video import VideoMixin
from .image_list import ImageListMixin
from .image_states import ImageStatesMixin
from .image_view import ImageViewMixin
from .exports import ExportsMixin
from .boxes import BoxesMixin
from .categories import CategoriesMixin
from .box_history import BoxHistoryMixin

AUTOSAVE_INTERVAL_MS = 5 * 60 * 1000


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


class ImageAnnotator(
    ShortcutsMixin,
    UsersMixin,
    ProjectFilesMixin,
    MergingMixin,
    DataVersionsMixin,
    VideoMixin,
    ImageListMixin,
    ImageStatesMixin,
    ImageViewMixin,
    ExportsMixin,
    BoxesMixin,
    CategoriesMixin,
    BoxHistoryMixin,
    QMainWindow,
):
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
