"""Main image annotation window and user workflows."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import zlib
from base64 import b64decode, b64encode
from pathlib import Path
from typing import Any

try:
    from PySide6.QtCore import QEvent, QEventLoop, QObject, QPoint, QStandardPaths, QPointF, QRectF, QSize, Qt, QRunnable, QThreadPool, QTimer, Signal
    from PySide6.QtGui import QAction, QColor, QBrush, QFont, QIcon, QImage, QKeySequence, QPainter, QPalette, QPen, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QCheckBox,
        QComboBox,
        QColorDialog,
        QDoubleSpinBox,
        QDialog,
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
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMenu,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QProgressDialog,
        QRadioButton,
        QSlider,
        QSpinBox,
        QSplitter,
        QTextBrowser,
        QToolButton,
        QVBoxLayout,
        QWidget,
        QWidgetAction,
    )
    from PIL import Image, ImageDraw, ImageFont
except ImportError as error:
    raise SystemExit("Install the GUI dependencies with: python -m pip install PySide6 Pillow") from error

from .avi import AviInfo, AviReader, VideoFrameRef, read_avi_info
from .dataset import is_verified, dataset_image_path, directory_names, path_key
from .merge import MERGE_IOU_THRESHOLD, MergeResult, image_key, merge, normalize
from .graphics import (
    AnnotationView, BoxItem, ImageRuler, ScaleBarItem, format_length, scale_bar_lengths_mm,
)
from .image_processing import (
    Adjustments, AutoLevelsWorker, DetailWorker, ImageLoadWorker, LoadedImage, PipelineSignals,
    PreviewAdjustmentWorker, apply_image_adjustments,
)

AUTOSAVE_INTERVAL_MS = 5 * 60 * 1000
FRAME_STATES = ("verified", "review", "unverified")


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
        self.persisted_image_ids: set[Any] = set()
        self.touched_image_ids: set[Any] = set()
        # Images whose project-folder file was copied, skipped or overwritten this session from their current source.
        self._folder_settled: set[Any] = set()
        self._project_image_names: set[str] | None = None
        self.roi_counts_by_image: dict[Any, dict[Any, list[int]]] = {}
        self.roi_totals = [0, 0]
        self.image_info = None
        self.next_annotation_id = 1
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
        self._user_name_by_id: dict[int, str] = {}
        self._user_id_by_name: dict[str, int] = {}
        self._load_notes: list[str] = []
        # (modified time, size) of the project file when it was opened or last saved by this session.
        self._file_signature: tuple[int, int] | None = None
        self._autosave_thread: threading.Thread | None = None
        self.dirty = False
        self._refreshing = False
        self._undo_history: list[tuple[Any, list[dict[str, Any]]]] = []
        self._redo_history: list[tuple[Any, list[dict[str, Any]]]] = []
        self._last_category_id: Any = None  # category chosen for the last drawn box
        self._file_sizes: dict[str, int | None] = {}  # source path -> bytes, for the image info line
        # The project file as opened or last saved, zlib-compressed: the base for a three-way merge.
        self._base_json: bytes | None = None
        self._base_missing_state = "review"  # state given to base images saved without frame_state
        self._last_read_base: bytes | None = None
        self._last_missing_state = "review"
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

        self.setWindowTitle("Stingray Labeler")
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "stingray_label_icon.png")))
        self.resize(1400, 900)
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
        self.add_folder_action = QAction("Add Folder…", self)
        self.add_video_action = QAction("Add Video…", self)
        self.add_video_folder_action = QAction("Add Video Folder…", self)
        self.save_project_action = QAction("Save Project", self)
        self.save_project_action.setShortcut(QKeySequence("Ctrl+S"))
        self.save_project_action.setToolTip(
            "Save the project JSON to its current file, or choose a file if it has not been saved yet"
        )
        self.save_project_as_action = QAction("Save Project As…", self)
        self.save_project_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.save_project_as_action.setToolTip("Choose a folder and file name for the project JSON")
        self.merge_project_action = QAction("Merge Project…", self)
        self.merge_project_action.setToolTip(
            "Merge another copy of this project into the open one, given the original both started from"
        )
        self.merge_project_action.setEnabled(False)
        self.training_export_action = QAction("Export Training Dataset…", self)
        self.training_export_action.setToolTip(
            "Export verified images, verified boxes, and explicitly marked background images"
        )
        self.save_image_action = QAction("Export Image…", self)
        self.save_image_action.setToolTip("Save the displayed image with its boxes drawn on it")
        self.save_rois_action = QAction("Export Crops…", self)
        self.save_rois_action.setToolTip("Save each box in the displayed image as a separate image")
        self.exit_action = QAction("Exit", self)
        self.add_images_action.setEnabled(False)
        self.add_folder_action.setEnabled(False)
        self.add_video_action.setEnabled(False)
        self.add_video_folder_action.setEnabled(False)
        self.save_project_action.setEnabled(False)
        self.save_project_as_action.setEnabled(False)
        self.training_export_action.setEnabled(False)
        # Grouped: the project, adding sources, exports, then Exit.
        for group in (
            (self.new_project_action, self.open_project_action, self.save_project_action,
             self.save_project_as_action, self.merge_project_action),
            (self.add_images_action, self.add_folder_action, self.add_video_action,
             self.add_video_folder_action),
            (self.training_export_action, self.save_image_action, self.save_rois_action),
            (self.exit_action,),
        ):
            if file_menu.actions():
                file_menu.addSeparator()
            for action in group:
                file_menu.addAction(action)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        self.redo_action = QAction("Redo", self)
        self.redo_action.setShortcut(QKeySequence("Ctrl+Y"))
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        self.edit_labels_action = QAction("Edit Categories…", self)
        self.import_classes_action = QAction("Import Categories from JSON…", self)
        self.edit_labels_action.setEnabled(False)
        self.import_classes_action.setEnabled(False)
        edit_menu.addAction(self.edit_labels_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.import_classes_action)
        self.change_annotator_action = QAction("Change User…", self)
        self.change_annotator_action.setEnabled(False)
        self.edit_annotators_action = QAction("Edit Users…", self)
        self.edit_annotators_action.setEnabled(False)
        edit_menu.addSeparator()
        edit_menu.addAction(self.change_annotator_action)
        edit_menu.addAction(self.edit_annotators_action)

        container = QWidget()
        layout = QHBoxLayout(container)
        splitter = QSplitter()

        sidebar = QWidget()
        side_layout = QVBoxLayout(sidebar)
        self.video_section = QWidget()
        video_layout = QVBoxLayout(self.video_section)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.addWidget(QLabel("Videos"))
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
        self.frame_back_button.setToolTip("Previous frame (Left)")
        self.frame_box = FrameNumberBox()
        self.frame_box.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.frame_box.setKeyboardTracking(False)  # typing jumps only on Enter
        self.frame_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame_box.setToolTip(
            "Type a frame number and press Enter (G). Left/Right: ±1 frame, Shift+Left/Right: ±10, "
            "Home/End: first/last, N/Shift+N: next/previous frame with work, Esc: back to the image"
        )
        self.frame_forward_button = QToolButton()
        self.frame_forward_button.setArrowType(Qt.ArrowType.RightArrow)
        self.frame_forward_button.setToolTip("Next frame (Right)")
        for button in (self.frame_back_button, self.frame_forward_button):
            button.setAutoRepeat(True)  # hold to keep stepping
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        frame_row.addWidget(self.frame_back_button)
        frame_row.addWidget(self.frame_box)
        frame_row.addWidget(self.frame_forward_button)
        self.video_controls.setVisible(False)
        # Search sits above both lists and filters videos and images by name.
        self.frame_search = QLineEdit()
        self.frame_search.setPlaceholderText("Search videos and images…")
        self.frame_search.setClearButtonEnabled(True)
        side_layout.addWidget(self.frame_search)
        side_layout.addWidget(self.video_section)
        side_layout.addWidget(QLabel("Images"))
        self.frame_list = FrameListWidget()
        # Qt's default per-row scrolling miscalculates positions when filters hide rows and a
        # horizontal scrollbar is shown, making the list jump; per-pixel scrolling does not.
        self.frame_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        side_layout.addWidget(self.frame_list, 1)
        filters_heading = QLabel("Filters")
        heading_font = filters_heading.font()
        heading_font.setBold(True)
        filters_heading.setFont(heading_font)
        side_layout.addWidget(filters_heading)
        side_layout.addWidget(QLabel("User"))
        self.annotator_filter = QComboBox()
        self.annotator_filter.addItem("All users", self.ALL_ANNOTATORS_FILTER_ID)
        self.annotator_filter.currentIndexChanged.connect(self._apply_frame_filters)
        side_layout.addWidget(self.annotator_filter)
        side_layout.addWidget(QLabel("Categories"))
        class_buttons = QHBoxLayout()
        select_all = QPushButton("All")
        select_none = QPushButton("None")
        class_buttons.addWidget(select_all)
        class_buttons.addWidget(select_none)
        side_layout.addLayout(class_buttons)
        self.class_filter = QListWidget()
        self.class_filter.setMaximumHeight(150)
        side_layout.addWidget(self.class_filter)
        side_layout.addWidget(QLabel("Image state"))
        self.frame_verification_filter = QComboBox()
        self.frame_verification_filter.addItem("All images", "all")
        self.frame_verification_filter.addItem("Verified images", "verified")
        self.frame_verification_filter.addItem("Review images", "review")
        self.frame_verification_filter.addItem("Unverified images", "unverified")
        side_layout.addWidget(self.frame_verification_filter)
        side_layout.addWidget(QLabel("Box verification"))
        self.verification_filter = QComboBox()
        self.verification_filter.addItem("All boxes", "all")
        self.verification_filter.addItem("Verified", "verified")
        self.verification_filter.addItem("Unverified", "unverified")
        side_layout.addWidget(self.verification_filter)
        iou_row = QHBoxLayout()
        self.iou_filter_check = QCheckBox("Box overlap IoU")
        self.iou_filter_check.setToolTip(
            "Show images where at least one pair of overlapping boxes has an IoU that meets the comparison"
        )
        self.iou_filter_operator = QComboBox()
        for operator in (">=", ">", "<", "<=", "=="):
            self.iou_filter_operator.addItem(operator, operator)
        self.iou_filter_threshold = QDoubleSpinBox()
        self.iou_filter_threshold.setRange(0.0, 1.0)
        self.iou_filter_threshold.setDecimals(2)
        self.iou_filter_threshold.setSingleStep(0.05)
        self.iou_filter_threshold.setValue(0.5)
        iou_row.addWidget(self.iou_filter_check)
        iou_row.addWidget(self.iou_filter_operator)
        iou_row.addWidget(self.iou_filter_threshold)
        side_layout.addLayout(iou_row)
        # One line of counts at the bottom of the panel; the full breakdown is in its tooltip.
        self.frame_stats = QLabel("No image selected")
        side_layout.addWidget(self.frame_stats)
        self.previous_button = QPushButton("Previous image")
        self.next_button = QPushButton("Next image")
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)

        scale_widget = QWidget()
        scale_layout = QVBoxLayout(scale_widget)
        self.show_ruler = QCheckBox("Show ruler")
        self.show_ruler.setChecked(True)
        scale_layout.addWidget(self.show_ruler)
        self.show_scale_bar = QCheckBox("Show scale bar")
        self.show_scale_bar.setChecked(False)
        scale_layout.addWidget(self.show_scale_bar)
        scale_layout.addWidget(QLabel("Pixel resolution"))
        self.pixel_resolution = QDoubleSpinBox()
        self.pixel_resolution.setRange(0.001, 100000)
        self.pixel_resolution.setDecimals(3)
        self.pixel_resolution.setValue(40.0)
        self.pixel_resolution.setSuffix(" µm/px")
        scale_layout.addWidget(self.pixel_resolution)
        self.pixel_resolution.setToolTip("Physical distance represented by each source-image pixel; editable after calibration")
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
        brightness_row = QHBoxLayout()
        brightness_row.addWidget(QLabel("Brightness"))
        self.brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self.brightness_slider.setRange(0, 300)
        self.brightness_slider.setValue(100)
        self.brightness_value = QLabel("100%")
        brightness_row.addWidget(self.brightness_slider, 1)
        brightness_row.addWidget(self.brightness_value)
        levels_layout.addLayout(brightness_row)
        contrast_row = QHBoxLayout()
        contrast_row.addWidget(QLabel("Contrast"))
        self.contrast_slider = QSlider(Qt.Orientation.Horizontal)
        self.contrast_slider.setRange(0, 300)
        self.contrast_slider.setValue(100)
        self.contrast_value = QLabel("100%")
        contrast_row.addWidget(self.contrast_slider, 1)
        contrast_row.addWidget(self.contrast_value)
        levels_layout.addLayout(contrast_row)
        gamma_row = QHBoxLayout()
        gamma_row.addWidget(QLabel("Gamma"))
        self.gamma_slider = QSlider(Qt.Orientation.Horizontal)
        self.gamma_slider.setRange(25, 400)
        self.gamma_slider.setValue(100)
        self.gamma_value = QLabel("1.00")
        gamma_row.addWidget(self.gamma_slider, 1)
        gamma_row.addWidget(self.gamma_value)
        levels_layout.addLayout(gamma_row)
        levels_layout.addWidget(QLabel("Black / white points"))
        black_white_row = QHBoxLayout()
        self.black_point = QSpinBox()
        self.black_point.setRange(0, 254)
        self.black_point.setPrefix("Black ")
        self.black_point.setValue(0)
        self.white_point = QSpinBox()
        self.white_point.setRange(1, 255)
        self.white_point.setPrefix("White ")
        self.white_point.setValue(255)
        black_white_row.addWidget(self.black_point)
        black_white_row.addWidget(self.white_point)
        levels_layout.addLayout(black_white_row)
        self.auto_levels_button = QPushButton("Auto levels for this image")
        self.auto_levels_button.setToolTip(
            "Estimate black and white points from this image only when requested"
        )
        levels_layout.addWidget(self.auto_levels_button)
        self.invert_check = QCheckBox("Invert")
        levels_layout.addWidget(self.invert_check)
        levels_widget.setMinimumWidth(340)
        levels_action = QWidgetAction(self.levels_menu)
        levels_action.setDefaultWidget(levels_widget)
        self.levels_menu.addAction(levels_action)
        self.reset_image_action = QAction("Reset image settings", self)
        self.levels_menu.addAction(self.reset_image_action)
        display_menu = self.menuBar().addMenu("&View")
        display_menu.addMenu(self.levels_menu)
        display_menu.addMenu(self.scale_menu)
        self.reference_lines_action = QAction("Reference lines while drawing", self)
        self.reference_lines_action.setCheckable(True)
        self.reference_lines_action.setChecked(True)
        self.reference_lines_action.setToolTip("Horizontal and vertical lines through the cursor while drawing a box")
        self.reference_lines_action.toggled.connect(self.view.set_reference_lines)
        display_menu.addAction(self.reference_lines_action)
        self.selection_color_action = QAction("Selected box outline color…", self)
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
        info_row = QHBoxLayout()
        info_row.addWidget(self.image_info, 1)
        info_row.addWidget(self.video_controls, 0, Qt.AlignmentFlag.AlignRight)
        editor_layout.addLayout(info_row)

        annotation_panel = QWidget()
        annotation_layout = QVBoxLayout(annotation_panel)
        # Image state: exactly one of three, on one line.
        state_row = QHBoxLayout()
        state_row.addWidget(QLabel("State:"))
        self.state_group = QButtonGroup(self)
        self.state_buttons: dict[str, QRadioButton] = {}
        for state, label, tooltip in (
            ("unverified", "Unverified", "Not reviewed. Unverified images are not saved."),
            ("review", "Review", "Worth another look (C). Set automatically on the first edit of an unverified image."),
            ("verified", "Verified", "Confident in the boxes (Shift+V). Needs a verified box or a background mark."),
        ):
            button = QRadioButton(label)
            button.setToolTip(tooltip)
            button.setEnabled(False)
            self.state_group.addButton(button)
            self.state_buttons[state] = button
            state_row.addWidget(button)
        state_row.addStretch(1)
        annotation_layout.addLayout(state_row)
        annotation_layout.addWidget(QLabel("Add / edit box"))
        self.draw_button = QPushButton("Draw box")
        self.draw_button.setCheckable(True)
        self.draw_button.setToolTip("Draw a box (B); press Esc to cancel")
        annotation_layout.addWidget(self.draw_button)
        self.background_check = QCheckBox("Background (no annotations)")
        self.background_check.setEnabled(False)
        annotation_layout.addWidget(self.background_check)
        annotation_layout.addWidget(QLabel("Selected annotation"))
        annotation_layout.addWidget(QLabel("Category"))
        self.edit_class = QComboBox()
        self.edit_class.setEnabled(False)
        annotation_layout.addWidget(self.edit_class)
        self.edit_status = QCheckBox("Box verified")
        self.edit_status.setEnabled(False)
        self.edit_status.setToolTip("Toggle verification for the selected box (V)")
        annotation_layout.addWidget(self.edit_status)
        self.delete_button = QPushButton("Delete annotation")
        self.delete_button.setEnabled(False)
        self.delete_button.setToolTip("Delete the selected box (Delete)")
        annotation_layout.addWidget(self.delete_button)
        annotation_layout.addWidget(QLabel("Annotations in image"))
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
        self.frame_verification_filter.currentIndexChanged.connect(self._apply_frame_filters)
        self.verification_filter.currentIndexChanged.connect(self._apply_frame_filters)
        self.iou_filter_check.toggled.connect(lambda _checked: self._apply_frame_filters())
        self.iou_filter_operator.currentIndexChanged.connect(
            lambda _index: self._apply_frame_filters() if self.iou_filter_check.isChecked() else None
        )
        self.iou_filter_threshold.valueChanged.connect(
            lambda _value: self._apply_frame_filters() if self.iou_filter_check.isChecked() else None
        )
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(150)
        self.search_timer.timeout.connect(self._apply_frame_filters)
        # Lambdas: a signal's argument would otherwise become QTimer.start(msec).
        self.frame_search.textChanged.connect(lambda _text: self.search_timer.start())
        self.detail_timer = QTimer(self)
        self.detail_timer.setSingleShot(True)
        self.detail_timer.setInterval(120)
        self.detail_timer.timeout.connect(self._update_detail)
        self.frame_list.currentItemChanged.connect(self._frame_selection_changed)
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
        self.show_scale_bar.toggled.connect(self._update_scale_bar)
        self.pixel_resolution.valueChanged.connect(self._update_scale_bar)
        self.pixel_resolution.valueChanged.connect(self._update_rulers)
        self.scale_bar_length.valueChanged.connect(self._update_scale_bar)
        self.view.viewChanged.connect(self._update_rulers)
        self.view.horizontalScrollBar().valueChanged.connect(self._update_rulers)
        self.view.verticalScrollBar().valueChanged.connect(self._update_rulers)
        self.view.viewChanged.connect(lambda: self.detail_timer.start())
        self.view.horizontalScrollBar().valueChanged.connect(lambda _value: self.detail_timer.start())
        self.view.verticalScrollBar().valueChanged.connect(lambda _value: self.detail_timer.start())
        self.scene.selectionChanged.connect(self._selection_changed)
        self.annotation_list.currentRowChanged.connect(self._annotation_row_changed)
        self.edit_class.currentIndexChanged.connect(self._class_changed)
        self.edit_status.toggled.connect(self._status_changed)
        self.delete_button.clicked.connect(self._delete_selected)
        self.edit_labels_action.triggered.connect(self._edit_labels)
        self.import_classes_action.triggered.connect(self._import_classes)
        self.change_annotator_action.triggered.connect(self._change_annotator)
        self.edit_annotators_action.triggered.connect(self._edit_annotators)
        select_all.clicked.connect(lambda: self._set_all_classes(True))
        select_none.clicked.connect(lambda: self._set_all_classes(False))
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

        help_menu = self.menuBar().addMenu("&Help")
        self.user_guide_action = QAction("User Guide", self)
        self.user_guide_action.setShortcut(QKeySequence("F1"))
        self.user_guide_action.triggered.connect(self._show_user_guide)
        help_menu.addAction(self.user_guide_action)
        self._user_guide: QDialog | None = None

        self.find_action = QAction("Find…", self)
        self.find_action.setToolTip("Search videos and images by name")
        self.find_action.triggered.connect(lambda: (self.frame_search.setFocus(), self.frame_search.selectAll()))
        edit_menu.insertAction(self.edit_labels_action, self.find_action)
        edit_menu.insertSeparator(self.edit_labels_action)
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
            (self.exit_action, ["Ctrl+E"]),
            (self.redo_action, ["Ctrl+Y", "Ctrl+Shift+Z"]),
            (self.find_action, ["Ctrl+F"]),
            (self.edit_labels_action, ["Ctrl+Shift+C"]),
            (self.change_annotator_action, ["Ctrl+U"]),
            (self.edit_annotators_action, ["Ctrl+Shift+U"]),
            (self.reference_lines_action, ["Ctrl+R"]),
            (self.reset_image_action, ["Ctrl+0"]),
        ):
            action.setShortcuts([QKeySequence(key) for key in keys])

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
        self.view.clear_drawing_guides()
        self.draw_button.setChecked(False)
        self._cancel_image_adjustment()
        self._detail_generation += 1
        self.current_items = []  # before clear(): removing a selected box fires selectionChanged, which reads it
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
        BoxItem.class_colors.clear()
        self._project_image_names = None
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
        self.next_annotation_id = max(
            (int(annotation.get("id", 0)) for annotation in self.annotation_data["annotations"]), default=0
        ) + 1
        self.dirty = False
        if not keep_history:
            self._undo_history.clear()
            self._redo_history.clear()
        # _set_dataset builds the image list itself once every filter is reset.
        self._refresh_annotator_filter(reset=True, refresh_frames=False)
        self.class_filter.blockSignals(True)
        self._fill_class_filter()
        self.class_filter.blockSignals(False)
        self.edit_class.blockSignals(True)
        self.edit_class.clear()
        for category_id, name in sorted(self.categories.items(), key=lambda pair: pair[1].lower()):
            self.edit_class.addItem(name, category_id)
        if not self.categories:
            self.edit_class.addItem("object", None)
            self.edit_class.setToolTip("Placeholder only. Draw a box and type a category name, or use the Edit menu.")
        else:
            self.edit_class.setToolTip("")
        self.edit_class.setEnabled(bool(self.categories))
        self.edit_class.blockSignals(False)
        self.frame_search.blockSignals(True)
        self.frame_search.clear()
        self.frame_search.blockSignals(False)
        self.verification_filter.blockSignals(True)
        self.verification_filter.setCurrentIndex(0)
        self.verification_filter.blockSignals(False)
        self.iou_filter_check.blockSignals(True)
        self.iou_filter_check.setChecked(False)
        self.iou_filter_check.blockSignals(False)
        self.frame_verification_filter.blockSignals(True)
        self.frame_verification_filter.setCurrentIndex(0)
        self.frame_verification_filter.blockSignals(False)
        self._sync_frame_controls()
        self._set_annotation_editing_enabled(self.current_annotator is not None)
        self.setWindowTitle("Stingray Labeler" + (f" — {self.root}" if self.root else ""))
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
        path = self._autosave_file(key_path) if key_path is not None else self._current_autosave_file()
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def _autosave(self) -> None:
        """Every few minutes, back up unsaved work so a crash or power loss does not lose it."""
        if not self.dirty or self.root is None:
            return
        if self._autosave_thread is not None and self._autosave_thread.is_alive():
            return
        path = self._current_autosave_file()
        if path is None:
            return
        self._commit_current_scene()
        # Snapshot on the UI thread; only serialising and writing happen in the background.
        project = dict(self.annotation_data)
        project["images"] = [dict(image) for image in self.images]
        project["annotations"] = [
            annotation for image in self.images
            for annotation in self._copy_annotations(self.annotations_by_image[image["id"]])
        ]
        project["categories"] = [dict(category) for category in self.annotation_data.get("categories", [])]
        project["annotators"] = [dict(user) for user in self.annotation_data.get("annotators", [])]
        snapshot = {
            "autosave_version": 1,
            "saved_at": time.time(),
            "root": str(self.root),
            "annotations_path": str(self.annotations_path) if self.annotations_path else None,
            "file_signature": list(self._file_signature) if self._file_signature else None,
            "annotator": self.current_annotator,
            "project": project,
            "videos": [str(info.path) for info in self.videos.values()],
            # The merge base travels with the backup, so a recovered session can still merge.
            "merge_base": b64encode(self._base_json).decode("ascii") if self._base_json else None,
            "merge_base_missing_state": self._base_missing_state,
            "state": [
                {
                    "id": image["id"],
                    "frame_verified": self.frame_verified_by_image.get(image["id"], False),
                    "review": self.review_by_image.get(image["id"], False),
                    "background": self.background_by_image.get(image["id"], False),
                    "source": str(self.source_paths_by_image[image["id"]])
                    if image["id"] in self.source_paths_by_image else None,
                    "video": [str(self.video_frame_by_image[image["id"]][0].path),
                              self.video_frame_by_image[image["id"]][1]]
                    if image["id"] in self.video_frame_by_image else None,
                    "touched": image["id"] in self.touched_image_ids,
                    "persisted": image["id"] in self.persisted_image_ids,
                }
                for image in self.images
            ],
        }

        def write() -> None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = path.with_name(path.name + ".tmp")
                temporary_path.write_text(json.dumps(snapshot), encoding="utf-8")
                temporary_path.replace(path)
            except OSError:
                pass  # a failed backup must never interrupt annotation work

        self._autosave_thread = threading.Thread(target=write, daemon=True)
        self._autosave_thread.start()
        self.statusBar().showMessage(f"Autosaved unsaved work at {time.strftime('%H:%M')}", 8000)

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
        if saved_at <= newer_than:
            self._discard_autosave(key_path)
            return None
        answer = QMessageBox.question(
            self, "Recover unsaved work?",
            f"Unsaved work from {time.strftime('%Y-%m-%d %H:%M', time.localtime(saved_at))} was found "
            f"for this project (it was not saved before the app closed).\n\nRestore it?",
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
        self._set_dataset(root, snapshot["project"], Path(annotations_path) if annotations_path else None)
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

        if snapshot.get("merge_base"):
            self._base_json = b64decode(snapshot["merge_base"])
            self._base_missing_state = snapshot.get("merge_base_missing_state", "review")
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
        self.status_label.setText("Restored unsaved work from autosave — save the project to keep it.")

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
            int(user["id"]): str(user.get("name", ""))
            for user in self.annotation_data.get("annotators", [])
            if isinstance(user, dict) and "id" in user
        }
        self._user_id_by_name = {name: user_id for user_id, name in self._user_name_by_id.items()}

    def _annotator_id(self, name: str) -> int:
        user_id = self._user_id_by_name.get(name)
        if user_id is not None:
            return user_id
        user_id = max(self._user_name_by_id, default=0) + 1
        self.annotation_data.setdefault("annotators", []).append({"id": user_id, "name": name})
        self._user_name_by_id[user_id] = name
        self._user_id_by_name[name] = user_id
        self._refresh_annotator_filter()  # a new user is the only reason to touch the dropdown
        return user_id

    def _set_annotation_annotator(self, annotation: dict[str, Any]) -> None:
        if self.current_annotator:
            annotation["annotator_id"] = self._annotator_id(self.current_annotator)
            annotation.pop("Annotator", None)

    def _annotation_user_name(self, annotation: dict[str, Any]) -> str:
        legacy_name = annotation.get("Annotator")
        if isinstance(legacy_name, str) and legacy_name.strip():
            return legacy_name.strip()
        try:
            user_id = int(annotation.get("annotator_id"))
        except (TypeError, ValueError):
            return "Unassigned"
        return self._user_name_by_id.get(user_id, "Unassigned")

    def _editing_allowed(self) -> bool:
        """Project changes are read-only until a user is chosen."""
        return self.root is not None and self.current_annotator is not None

    def _set_annotation_editing_enabled(self, enabled: bool) -> None:
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
            self.edit_class.setEnabled(False)
            self.edit_status.setEnabled(False)
            self.delete_button.setEnabled(False)
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
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.setMinimumWidth(360)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        value = name_input.currentText().strip()
        if not value:
            QMessageBox.information(
                self, "User required", "Enter or select a user name."
            )
            return None
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
        if not self._editing_allowed():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit users")
        layout = QVBoxLayout(dialog)
        annotator_list = QListWidget(dialog)
        layout.addWidget(annotator_list)

        def refresh(selected_id: Any = None) -> None:
            annotator_list.clear()
            for annotator in sorted(
                self.annotation_data.get("annotators", []),
                key=lambda item: str(item.get("name", "")).casefold(),
            ):
                item = QListWidgetItem(annotator["name"])
                item.setData(Qt.ItemDataRole.UserRole, annotator["id"])
                annotator_list.addItem(item)
            if selected_id is not None:
                for row in range(annotator_list.count()):
                    if annotator_list.item(row).data(Qt.ItemDataRole.UserRole) == selected_id:
                        annotator_list.setCurrentRow(row)
                        break
            if annotator_list.count() and annotator_list.currentRow() < 0:
                annotator_list.setCurrentRow(0)

        def rename_annotator() -> None:
            item = annotator_list.currentItem()
            if item is None:
                return
            source_id = item.data(Qt.ItemDataRole.UserRole)
            source = next(
                (record for record in self.annotation_data["annotators"]
                 if record["id"] == source_id),
                None,
            )
            if source is None:
                return
            old_name = source["name"]
            name, accepted = QInputDialog.getText(
                self, "Rename user", "User name:", text=old_name
            )
            name = name.strip()
            if not accepted or not name or name == old_name:
                return
            target = next(
                (record for record in self.annotation_data["annotators"]
                 if record["id"] != source_id and record["name"].casefold() == name.casefold()),
                None,
            )
            filtered_on_source = self.annotator_filter.currentData() == old_name
            if target is not None:
                self._commit_current_scene()

                def from_source(annotation: dict[str, Any]) -> bool:
                    return (annotation.get("annotator_id") == source_id
                            or annotation.get("Annotator") == old_name)

                count = sum(from_source(annotation) for annotation in self._all_annotations())
                answer = QMessageBox.question(
                    self,
                    "Merge users?",
                    f"Merge {old_name!r} into {target['name']!r}? This will reassign {count:,} ROI(s).",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                for annotation in (*self._all_annotations(), *self._history_annotations()):
                    if from_source(annotation):
                        annotation["annotator_id"] = target["id"]
                        annotation.pop("Annotator", None)
                self.annotation_data["annotators"].remove(source)
                if self.current_annotator == old_name:
                    self.current_annotator = target["name"]
                self._users_changed(target["name"] if filtered_on_source else None)
                refresh(target["id"])
                return

            source["name"] = name
            for annotation in (*self._all_annotations(), *self._history_annotations()):
                if annotation.get("Annotator") == old_name:
                    annotation["Annotator"] = name
            if self.current_annotator == old_name:
                self.current_annotator = name
            self._users_changed(name if filtered_on_source else None)
            refresh(source_id)

        def add_annotator() -> None:
            name, accepted = QInputDialog.getText(self, "Add user", "User name:")
            name = name.strip()
            if not accepted or not name:
                return
            if any(existing.casefold() == name.casefold() for existing in self._user_id_by_name):
                QMessageBox.information(self, "User already exists", f"The user {name!r} already exists.")
                return
            refresh(self._annotator_id(name))
            self._mark_dirty(touch_image=False)

        def delete_annotator() -> None:
            item = annotator_list.currentItem()
            if item is not None:
                self._delete_annotator(item.data(Qt.ItemDataRole.UserRole))
                refresh()

        controls = QHBoxLayout()
        add_button = QPushButton("Add…", dialog)
        rename_button = QPushButton("Edit…", dialog)
        rename_button.setToolTip("Rename; a name that already exists offers to merge into it")
        delete_button = QPushButton("Delete…", dialog)
        controls.addWidget(add_button)
        controls.addWidget(rename_button)
        controls.addWidget(delete_button)
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        add_button.clicked.connect(add_annotator)
        rename_button.clicked.connect(rename_annotator)
        delete_button.clicked.connect(delete_annotator)
        annotator_list.itemDoubleClicked.connect(lambda _item: rename_annotator())
        refresh()
        dialog.setMinimumSize(420, 360)
        dialog.exec()

    def _delete_annotator(self, user_id: Any) -> None:
        """Delete a user record; boxes credited to them are reassigned or left unassigned, never deleted."""
        record = next(
            (user for user in self.annotation_data.get("annotators", []) if user["id"] == user_id), None
        )
        if record is None or not self._editing_allowed():
            return
        name = record["name"]
        if name == self.current_annotator:
            QMessageBox.information(
                self, "Delete user", f"{name!r} is the active user. Switch to another user first."
            )
            return
        self._commit_current_scene()

        def credited(annotation: dict[str, Any]) -> bool:
            return annotation.get("annotator_id") == user_id or annotation.get("Annotator") == name

        count = sum(credited(annotation) for annotation in self._all_annotations())
        target = None
        if count:
            message = QMessageBox(self)
            message.setWindowTitle("Delete user")
            message.setIcon(QMessageBox.Icon.Question)
            message.setText(f"{name!r} is credited on {count:,} box(es).")
            message.setInformativeText(
                "Reassign those boxes to another user, or leave them unassigned. The boxes are kept either way."
            )
            reassign_button = message.addButton("Reassign to…", QMessageBox.ButtonRole.AcceptRole)
            unassign_button = message.addButton("Leave unassigned", QMessageBox.ButtonRole.AcceptRole)
            message.addButton(QMessageBox.StandardButton.Cancel)
            message.exec()
            clicked = message.clickedButton()
            if clicked is reassign_button:
                others = sorted(
                    (user["name"] for user in self.annotation_data["annotators"] if user["id"] != user_id),
                    key=str.casefold,
                )
                if not others:
                    QMessageBox.information(self, "Delete user", "There is no other user to reassign the boxes to.")
                    return
                choice, accepted = QInputDialog.getItem(
                    self, "Reassign boxes", f"Credit the {count:,} box(es) to:", others, 0, False
                )
                if not accepted or not choice:
                    return
                target = next(user for user in self.annotation_data["annotators"] if user["name"] == choice)
            elif clicked is not unassign_button:
                return
        # Undo history is updated too, so Undo never brings back a reference to the deleted user.
        for annotation in (*self._all_annotations(), *self._history_annotations()):
            if credited(annotation):
                annotation.pop("Annotator", None)
                if target is not None:
                    annotation["annotator_id"] = target["id"]
                else:
                    annotation.pop("annotator_id", None)
        filtered_on_user = self.annotator_filter.currentData() == name
        self.annotation_data["annotators"].remove(record)
        self._users_changed(self.ALL_ANNOTATORS_FILTER_ID if filtered_on_user else None)
        self.status_label.setText(
            f"Deleted user {name!r}"
            + (f"; {count:,} box(es) credited to {target['name']!r}" if target is not None
               else f"; {count:,} box(es) left unassigned" if count else "")
            + " — unsaved changes"
        )

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
                if not self._fill_missing_frame_state(annotation_data):
                    return
                self.current_annotator = None
                self._set_dataset(root, annotation_data, annotations_file)
                self._file_signature = signature
                self._base_json, self._base_missing_state = self._last_read_base, self._last_missing_state
                annotator = self._choose_annotator(self.annotation_data)
                loaded_notes = "; ".join(self._load_notes)
                if annotator is None:
                    self.status_label.setText(
                        "Project loaded. Choose a user to enable annotation edits."
                        + (f"\nOn load: {loaded_notes}." if loaded_notes else "")
                    )
                    return
                self._activate_annotator(annotator)
                if loaded_notes:
                    self.status_label.setText(f"On load: {loaded_notes}.")
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
        """The search box filters videos by name, as it does images."""
        text = self.frame_search.text().strip().casefold()
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
        for image in reversed(self.images):  # frames opened this session are at the end
            if image["file_name"].rsplit("/", 1)[-1].casefold() == key:
                return image["id"]
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
            match = re.fullmatch(r"(.+)_(\d+)\.[^./]+", image["file_name"].rsplit("/", 1)[-1])
            if match is None:
                continue
            video = by_stem.get(match.group(1).casefold())
            if video is None or int(match.group(2)) >= video.frame_count:
                continue
            self.video_frame_by_image[image["id"]] = (video, int(match.group(2)))
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
        pattern = re.compile(re.escape(video.path.stem) + r"_(\d+)\.[^./]+", re.IGNORECASE)
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
            match = pattern.fullmatch(image["file_name"].rsplit("/", 1)[-1])
            if match:
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

    def _fill_missing_frame_state(self, annotation_data: dict[str, Any]) -> bool:
        """Give every image a frame_state, asking once for those without one; False cancels opening.

        Older files used a frame_verified flag: true becomes "verified"; images without it or with
        false get the state the user chooses for the whole batch.
        """
        images = annotation_data["images"]
        unknown = [image for image in images
                   if "frame_state" in image and image["frame_state"] not in FRAME_STATES]
        if unknown:
            raise ValueError(
                f"{len(unknown):,} image(s) have an unknown frame_state "
                f"(expected {', '.join(FRAME_STATES)}), e.g. {unknown[0]['file_name']!r}"
            )
        for image in images:
            if "frame_state" not in image and is_verified(image.get("frame_verified", False)):
                image["frame_state"] = "verified"
        missing = [image for image in images if "frame_state" not in image]
        self._last_missing_state = "review"
        if missing:
            message = QMessageBox(self)
            message.setWindowTitle("Image state not recorded")
            message.setIcon(QMessageBox.Icon.Question)
            message.setText(
                f"{len(missing):,} of {len(images):,} image(s) in this file have no image state."
            )
            message.setInformativeText(
                "Choose one state for all of them.\n"
                "Verified: confident in the boxes.\n"
                "Review: worth another look.\n"
                "Unverified: not reviewed (e.g. model output). Unverified images are not saved "
                "unless someone edits them, which marks them Review."
            )
            unverified_button = message.addButton("Unverified", QMessageBox.ButtonRole.AcceptRole)
            review_button = message.addButton("Review", QMessageBox.ButtonRole.AcceptRole)
            verified_button = message.addButton("Verified", QMessageBox.ButtonRole.AcceptRole)
            message.addButton(QMessageBox.StandardButton.Cancel)
            message.setDefaultButton(unverified_button)
            message.exec()
            states = {unverified_button: "unverified", review_button: "review", verified_button: "verified"}
            state = states.get(message.clickedButton())
            if state is None:
                return False
            for image in missing:
                image["frame_state"] = state
            self._last_missing_state = state  # the merge base reads those images the same way
        for image in images:
            image.pop("frame_verified", None)  # frame_state replaces it
        return True

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
            # The file as opened, compressed, is the common starting point for a later merge.
            return json.loads(contents), zlib.compress(bytes(contents), 6)

        try:
            annotation_data, self._last_read_base = self._run_in_background(
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
            try:
                annotator["id"] = int(annotator["id"])
            except (TypeError, ValueError):
                raise ValueError("Each user id must be an integer") from None
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

        # Legacy "Annotator" names become annotator records referenced by annotator_id.
        annotators = annotation_data["annotators"]
        id_by_name = {annotator["name"]: annotator["id"] for annotator in annotators}
        id_by_folded = {name.casefold(): user_id for name, user_id in id_by_name.items()}
        known_ids = set(id_by_name.values())
        converted = 0
        for annotation in annotations:
            name = annotation.get("Annotator")
            if not isinstance(name, str) or not name.strip():
                continue
            if annotation.get("annotator_id") not in known_ids:
                name = name.strip()
                user_id = id_by_folded.get(name.casefold())
                if user_id is None:
                    user_id = max(known_ids, default=0) + 1
                    annotators.append({"id": user_id, "name": name})
                    known_ids.add(user_id)
                    id_by_folded[name.casefold()] = user_id
                annotation["annotator_id"] = user_id
            annotation.pop("Annotator", None)
            converted += 1
        if converted:
            notes.append(f"{converted:,} box(es) had legacy annotator names converted to annotator records")

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
        if self.current_annotator is None or not self._undo_history:
            return
        image_id, state = self._undo_history.pop()
        self._redo_history.append(self._snapshot_image_annotations(image_id))
        self._restore_image_annotations(image_id, state)
        self.dirty = True
        self.status_label.setText("Undo — unsaved changes")

    def redo(self) -> None:
        if self.current_annotator is None or not self._redo_history:
            return
        image_id, state = self._redo_history.pop()
        self._undo_history.append(self._snapshot_image_annotations(image_id))
        self._restore_image_annotations(image_id, state)
        self.dirty = True
        self.status_label.setText("Redo — unsaved changes")

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
        self.class_filter.blockSignals(True)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.class_filter.count()):
            self.class_filter.item(row).setCheckState(state)
        self.class_filter.blockSignals(False)
        self._apply_frame_filters()

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
        for image in self.images:
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
        for image in ordered_images:
            image_name = Path(image["file_name"]).name
            item = QListWidgetItem(image_name)
            item.setData(Qt.ItemDataRole.UserRole, image["id"])
            self.frame_list.addItem(item)
            self._frame_items[image["id"]] = item
            self._frame_names[image["id"]] = image_name.casefold()
            self._refresh_pair_ious(image["id"])
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
        key = self._frame_sort_key(image)
        low, high = 0, self.frame_list.count()
        while low < high:
            middle = (low + high) // 2
            if self._frame_sort_keys[self.frame_list.item(middle).data(Qt.ItemDataRole.UserRole)] <= key:
                low = middle + 1
            else:
                high = middle
        image_name = Path(image["file_name"]).name
        item = QListWidgetItem(image_name)
        item.setData(Qt.ItemDataRole.UserRole, image_id)
        anchor = self.frame_list.itemAt(QPoint(0, 0))
        anchor_top = self.frame_list.visualItemRect(anchor).top() if anchor is not None else 0
        self.frame_list.blockSignals(True)
        self.frame_list.insertItem(low, item)
        self.frame_list.blockSignals(False)
        self._frame_items[image_id] = item
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

    def _frame_filter_context(self) -> dict[str, Any]:
        """Read the filter widgets once, so each row check is plain dictionary work."""
        selected_ids = self._selected_category_ids()
        include_background = self.BACKGROUND_FILTER_ID in selected_ids
        selected_categories = selected_ids - {self.BACKGROUND_FILTER_ID}
        verification = self.verification_filter.currentData()
        annotator = self.annotator_filter.currentData()
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
            "annotator_id": self._user_id_by_name.get(annotator),
            "iou_enabled": self.iou_filter_check.isChecked(),
            "iou_operator": self.iou_filter_operator.currentData(),
            "iou_threshold": round(self.iou_filter_threshold.value(), 2),
        }

    def _image_matches(self, image_id: Any, context: dict[str, Any]) -> bool:
        if self.visible_image_ids is not None and image_id not in self.visible_image_ids:
            return False
        if context["search"] and context["search"] not in self._frame_names.get(image_id, ""):
            return False
        frame_state = context["frame_verification"]
        if frame_state != "all" and self._frame_state(image_id) != frame_state:
            return False
        annotations = self.annotations_by_image.get(image_id, [])
        background = self.background_by_image.get(image_id, False)
        if not (
            context["no_filter"]
            or (context["include_background"] and background)
            or any(
                self._annotation_matches_filter(
                    annotation, context["categories"], context["verification"]
                )
                for annotation in annotations
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
        annotator = context["annotator"]
        if annotator == self.BACKGROUND_ANNOTATOR_FILTER_ID:
            return background and not annotations
        if annotator == self.ALL_ANNOTATORS_FILTER_ID:
            return True
        annotator_id = context["annotator_id"]
        return any(
            (annotator_id is not None and annotation.get("annotator_id") == annotator_id)
            or annotation.get("Annotator") == annotator
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
        active_id = self._pending_image_id if self._pending_image_id is not None else self.current_image_id
        # A frame shown from a video stays on screen; filters only change the image list.
        if select_first and active_id not in shown and self._active_video is None:
            first = self._first_shown_item()
            if first is not None:
                self._select_frame_item(first)
            elif active_id is not None:
                self._clear_scene()
        self._update_navigation_buttons()
        self._update_frame_stats()

    @staticmethod
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
        """Recompute one image's overlapping-pair IoUs for the overlap filter."""
        self._pair_ious_by_image[image_id] = self._pair_ious(self.annotations_by_image.get(image_id, []))

    def _update_frame_visibility(self, image_id: Any) -> None:
        """Re-check one image after it was edited; the rest of the list is untouched."""
        item = self._frame_items.get(image_id)
        if item is None and not (image_id in self.video_frame_by_image and self._has_work_or_verified(image_id)):
            return
        self._refresh_pair_ious(image_id)
        if item is None:
            item = self._add_frame_row(image_id)
        matches = self._image_matches(image_id, self._frame_filter_context())
        if item.isHidden() == matches:
            item.setHidden(not matches)
        if matches:
            self._shown_ids.add(image_id)
        else:
            self._shown_ids.discard(image_id)
        self._update_navigation_buttons()

    def _first_shown_item(self) -> QListWidgetItem | None:
        for row in range(self.frame_list.count()):
            item = self.frame_list.item(row)
            if not item.isHidden():
                return item
        return None

    def _select_frame_item(self, item: QListWidgetItem) -> None:
        self.frame_list.blockSignals(True)
        self.frame_list.setCurrentItem(item)
        self.frame_list.blockSignals(False)
        self._load_frame(item.data(Qt.ItemDataRole.UserRole))

    def _shown_neighbor(self, step: int) -> QListWidgetItem | None:
        """Next visible row in ``step`` direction, wrapping, skipping filtered-out rows."""
        count = self.frame_list.count()
        row = self.frame_list.currentRow()
        if count < 2 or row < 0:
            return None
        for offset in range(1, count):
            item = self.frame_list.item((row + step * offset) % count)
            if not item.isHidden():
                return None if item is self.frame_list.item(row) else item
        return None

    def _update_navigation_buttons(self) -> None:
        enough = len(self.videos) > 1 if self._active_video is not None else len(self._shown_ids) > 1
        self.previous_button.setEnabled(enough)
        self.next_button.setEnabled(enough)

    def _update_frame_stats(self) -> None:
        shown_ids = self._shown_ids
        selected = self._selected_category_ids()
        verification = self.verification_filter.currentData()
        shown_states = {state: 0 for state in FRAME_STATES}
        for image_id in shown_ids:
            shown_states[self._frame_state(image_id)] += 1
        # Totals count listed images, not video frames that were only looked at.
        total_states = {state: 0 for state in FRAME_STATES}
        for image_id in self._frame_items:
            total_states[self._frame_state(image_id)] += 1
        verified_filtered = unverified_filtered = 0
        for image_id in shown_ids:
            for category_id, counts in self.roi_counts_by_image.get(image_id, {}).items():
                if category_id not in selected:
                    continue
                if verification in ("all", "verified"):
                    verified_filtered += counts[0]
                if verification in ("all", "unverified"):
                    unverified_filtered += counts[1]
        if self._frame_items and not shown_ids:
            self.frame_stats.setText(f"No images match these filters (0 / {len(self._frame_items):,})")
        else:
            self.frame_stats.setText(
                f"{len(shown_ids):,} / {len(self._frame_items):,} images · "
                f"{shown_states['verified']:,} verified · {shown_states['review']:,} review · "
                f"{verified_filtered + unverified_filtered:,} boxes"
            )
        self.frame_stats.setToolTip(
            "Shown with the current filters / in the project\n"
            f"Images: {len(shown_ids):,} / {len(self._frame_items):,}\n"
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
        self.image_info.setText(
            f"Dim: {width:,} × {height:,} px  |  Size: {self._image_size_text(image)}  |  "
            f"Boxes: {len(self.current_items)} ({verified} verified, {unverified} unverified)"
        )

    def _image_size_text(self, image: dict[str, Any]) -> str:
        """File size of the image's current source; looked up once per file (metadata only, nothing is read)."""
        source = self._image_source(image)
        if isinstance(source, VideoFrameRef):
            return f"{self._format_bytes(source.video.frame_bytes)} (raw video frame)"
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

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if QApplication.activeWindow() is self and event.type() == QEvent.Type.KeyPress:
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
            if isinstance(focus, (QLineEdit, QComboBox, QSlider, QDoubleSpinBox, QSpinBox)):
                return super().eventFilter(watched, event)
            if self._active_video is not None and self._video_key(event):
                return True

            in_annotation_ui = focus_within(self.centralWidget())
            if event.modifiers() == Qt.KeyboardModifier.ShiftModifier and event.key() == Qt.Key.Key_V:
                if in_annotation_ui and self.current_image_id in self.image_by_id:
                    verified = self._frame_state(self.current_image_id) == "verified"
                    self._set_frame_state("review" if verified else "verified")
                    return True
            if event.modifiers() == Qt.KeyboardModifier.ShiftModifier and event.key() == Qt.Key.Key_N:
                self._jump_to_unverified(-1)
                return True
            if event.modifiers() == Qt.KeyboardModifier.NoModifier:
                if event.key() == Qt.Key.Key_Left:
                    self._navigate_frames(-1)
                    return True
                if event.key() == Qt.Key.Key_Right:
                    self._navigate_frames(1)
                    return True
                if event.key() == Qt.Key.Key_N:
                    self._jump_to_unverified(1)
                    return True
                if in_annotation_ui and event.key() == Qt.Key.Key_B and self.draw_button.isEnabled():
                    self.draw_button.toggle()
                    return True
                if in_annotation_ui and event.key() == Qt.Key.Key_V and self.edit_status.isEnabled():
                    self.edit_status.toggle()
                    return True
                if (in_annotation_ui and event.key() == Qt.Key.Key_C
                        and self.current_image_id in self.image_by_id):
                    review = self._frame_state(self.current_image_id) == "review"
                    self._set_frame_state("unverified" if review else "review")
                    return True
                if in_annotation_ui and event.key() == Qt.Key.Key_Escape and self.draw_button.isChecked():
                    self.draw_button.setChecked(False)
                    return True
                in_roi_ui = focus_within(self.view) or focus_within(self.annotation_list)
                if in_roi_ui and event.key() == Qt.Key.Key_Delete and self.delete_button.isEnabled():
                    self._delete_selected()
                    return True
        return super().eventFilter(watched, event)

    def _video_key(self, event) -> bool:
        """Frame navigation while a video is selected; True when the key was used."""
        key, modifiers = event.key(), event.modifiers()
        if modifiers == Qt.KeyboardModifier.NoModifier:
            steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}
            if key in steps:
                self._step_video_frame(steps[key])
            elif key == Qt.Key.Key_Home:
                self._show_video_frame(0)
            elif key == Qt.Key.Key_End:
                self._show_video_frame(self._active_video.frame_count - 1)
            elif key == Qt.Key.Key_N:
                self._jump_to_video_work(1)
            elif key == Qt.Key.Key_G:
                self.frame_box.setFocus()
                self.frame_box.selectAll()
            else:
                return False
            return True
        if modifiers == Qt.KeyboardModifier.ShiftModifier:
            steps = {Qt.Key.Key_Left: -10, Qt.Key.Key_Right: 10}
            if key in steps:
                self._step_video_frame(steps[key])
            elif key == Qt.Key.Key_N:
                self._jump_to_video_work(-1)
            else:
                return False
            return True
        return False

    def _jump_to_unverified(self, step: int) -> None:
        """Move to the next (or previous) image in the current list that is not verified."""
        count = self.frame_list.count()
        row = self.frame_list.currentRow()
        for offset in range(1, count):
            item = self.frame_list.item((row + step * offset) % count)
            if item.isHidden() or (row >= 0 and item is self.frame_list.item(row)):
                continue
            if not self.frame_verified_by_image.get(item.data(Qt.ItemDataRole.UserRole), False):
                self.frame_list.setCurrentItem(item)
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
        corner: str = "bottom-right",
    ) -> bool:
        pixel_resolution = self.pixel_resolution.value()
        if pixel_resolution <= 0:
            return False
        width, height = image.size
        margin = max(16, width / 80)
        display_length_mm = length_mm or self.scale_bar_length.value()
        available_width = min(width - 2 * margin, width * 0.5)
        bar_width = display_length_mm * 1000 / pixel_resolution
        if bar_width > available_width:
            display_length_mm = next((length for length in scale_bar_lengths_mm()
                                      if length <= display_length_mm
                                      and length * 1000 / pixel_resolution <= available_width), 0)
            if not display_length_mm:
                return False
            bar_width = display_length_mm * 1000 / pixel_resolution
        if bar_width <= 0 or available_width <= 0:
            return False
        bar_height = max(5, width / 450)
        label = format_length(display_length_mm)
        font_size = max(12, min(40, round(width / 50)))
        font = self._label_font(font_size)
        draw = ImageDraw.Draw(image)
        text_box = draw.textbbox((0, 0), label, font=font)
        label_width = text_box[2] - text_box[0]
        label_height = text_box[3] - text_box[1]
        right_corner = corner.endswith("right")
        bottom_corner = corner.startswith("bottom")
        left = width - margin - bar_width if right_corner else margin
        top = height - margin - bar_height if bottom_corner else margin + label_height + 5
        label_left = round(left + bar_width - label_width) if right_corner else round(left)
        label_top = round(top - label_height - 5) if bottom_corner else round(margin)
        draw.rectangle((left, top, left + bar_width, top + bar_height), fill="black")
        draw.text((label_left, label_top), label, fill="black", font=font)
        return True

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

    def _scale_bar_export_options(self, layout: QFormLayout):
        if not self.show_scale_bar.isChecked():
            return None
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
        layout.addRow("Scale bar length", length)
        return corner, length

    def _roi_scale_length(self, roi_width: int, preferred_length_mm: float) -> float:
        resolution = self.pixel_resolution.value()
        maximum_bar_width = roi_width * 0.5
        if preferred_length_mm * 1000 / resolution <= maximum_bar_width:
            return preferred_length_mm
        return next((length for length in scale_bar_lengths_mm()
                     if length <= preferred_length_mm
                     and length * 1000 / resolution <= maximum_bar_width), 0.000001)

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

    def _frame_is_exportable(self, image_id: Any) -> bool:
        return self.frame_verified_by_image.get(image_id, False)

    def _frame_has_work(self, image_id: Any) -> bool:
        return bool(
            self.annotations_by_image.get(image_id)
            or self.background_by_image.get(image_id, False)
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
                              save_path: Path, report, cancelled: threading.Event) -> bytes:
        """Copy images (video frames are written as PNG), then write the JSON; return it compressed.

        Each copy is ``(source, target, overwrite)``. A file being overwritten is first written beside
        it and only swapped in once every copy and the JSON are ready, so a failure or Cancel before
        then removes only files written here and never touches what was already in the folder.
        """
        created: list[Path] = []
        staged: list[tuple[Path, Path]] = []
        try:
            for index, (source, target, overwrite) in enumerate(copies, start=1):
                if cancelled.is_set():
                    raise OperationCancelled()
                if target.exists() and not overwrite:
                    raise OSError(f"{target} already exists and was not overwritten.")
                report(index - 1, f"Copying {source.name} ({index:,} of {len(copies):,})…")
                destination = target.with_name(target.name + ".stingray-new") if overwrite else target
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
            text = json.dumps(data, indent=2) + "\n"
            temporary_path = save_path.with_name(save_path.name + ".tmp")
            temporary_path.write_text(text, encoding="utf-8")
        except BaseException:
            for path in created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        for staged_path, target in staged:
            staged_path.replace(target)
        temporary_path.replace(save_path)
        return zlib.compress(text.encode("utf-8"), 6)  # what was written, kept as the next merge base

    @staticmethod
    def _file_signature_of(path: Path | None) -> tuple[int, int] | None:
        try:
            stat = path.stat() if path is not None else None
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size) if stat else None

    def _changed_by_someone_else(self, path: Path) -> bool:
        """True when the project file on disk is no longer the version this session opened or saved."""
        if self._file_signature is None or not path.exists():
            return False
        return self._file_signature_of(path) != self._file_signature

    def save_project(self) -> bool:
        return self._save_project(self.annotations_path)

    def save_project_as(self) -> bool:
        return self._save_project(None)

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

    def _merge_dialog(self, base: dict[str, Any], theirs: dict[str, Any], theirs_label: str,
                      accept_text: str) -> MergeResult | None:
        """Merge, show what happened, and return the result if the user goes ahead."""
        sides = (
            normalize(base, self._base_missing_state),
            normalize(self._current_project_data()),
            normalize(theirs),
        )
        dialog = QDialog(self)
        dialog.setWindowTitle("Merge project")
        layout = QVBoxLayout(dialog)
        summary_label = QLabel()
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(QLabel("Duplicate boxes: same category and IoU ≥"))
        threshold = QDoubleSpinBox()
        threshold.setRange(0.05, 1.0)
        threshold.setSingleStep(0.05)
        threshold.setDecimals(2)
        threshold.setValue(MERGE_IOU_THRESHOLD)
        threshold_row.addWidget(threshold)
        threshold_row.addStretch(1)
        layout.addLayout(threshold_row)
        details = QListWidget()
        layout.addWidget(details, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton(accept_text, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        result: list[MergeResult] = []
        headings = (
            ("from_theirs", "Images with box changes taken from " + theirs_label),
            ("added", "Images added from " + theirs_label),
            ("removed", "Images no longer saved (set unverified on one side)"),
            ("kept_over_delete", "Edited boxes kept although the other side deleted them"),
            ("duplicates", "Duplicate boxes collapsed to one"),
            ("class_disagreements", "Class disagreements: both boxes kept, image set to Review"),
            ("state_conflicts", "Image state changed on both sides: set to Review"),
        )

        def recalculate() -> None:
            outcome = merge(*sides, threshold=round(threshold.value(), 2))
            result[:] = [outcome]
            summary_label.setText(
                f"Merging your work with {theirs_label}. {len(outcome.changed):,} image(s) in your project "
                "change. Nothing is decided for you beyond the rules below; review images afterwards with "
                "the Review filter."
            )
            details.clear()
            for kind, heading in headings:
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

        threshold.valueChanged.connect(lambda _value: recalculate())
        recalculate()
        dialog.resize(640, 520)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return result[0]

    def _apply_merge(self, outcome: MergeResult) -> None:
        """Bring the merged images and boxes into this session; images only in this session are left alone."""
        self._commit_current_scene()
        category_ids = {name.casefold(): category_id for category_id, name in self.categories.items()}
        local_by_key = {image_key(image["file_name"]): image["id"] for image in self.images}
        boxes_by_key: dict[str, list] = {}
        for entry in outcome.boxes:
            boxes_by_key.setdefault(entry["box"].image, []).append(entry)
        affected = set(outcome.changed)
        # Images this session would have saved that the merge left unverified drop out like any unverified image.
        affected |= {key for key, local_id in local_by_key.items()
                     if key not in outcome.images and self._frame_state(local_id) != "unverified"}
        added = 0
        for key in sorted(affected):
            merged = outcome.images.get(key)
            local_id = local_by_key.get(key)
            if local_id is None:
                if merged is None or merged["state"] == "unverified":
                    continue
                while self._next_image_id in self.image_by_id:
                    self._next_image_id += 1
                local_id = self._next_image_id
                self._next_image_id += 1
                record = merged["record"]
                image = {
                    "id": local_id, "file_name": merged["name"],
                    "width": record.get("width", 0), "height": record.get("height", 0),
                    "frame_state": merged["state"], "background": merged["background"],
                }
                self.images.append(image)
                self.image_by_id[local_id] = image
                if self.root is not None:
                    self.source_paths_by_image[local_id] = self.root / merged["name"]
                self.annotations_by_image[local_id] = []
                self.roi_counts_by_image[local_id] = {}
                self.persisted_image_ids.add(local_id)
                local_by_key[key] = local_id
                added += 1
            state = merged["state"] if merged else "unverified"
            self.frame_verified_by_image[local_id] = state == "verified"
            self.review_by_image[local_id] = state == "review"
            self.background_by_image[local_id] = bool(merged and merged["background"])
            annotations = []
            for entry in boxes_by_key.get(key, []) if merged else []:
                box = entry["box"]
                category_id = category_ids.get(box.category.casefold())
                if category_id is None:
                    category_id = self._create_class(box.category)
                    category_ids[box.category.casefold()] = category_id
                x, y, width, height = box.bbox
                annotation = json.loads(box.extra)
                annotation.update({
                    "id": entry["id"], "image_id": local_id, "category_id": category_id,
                    "bbox": [x, y, width, height], "area": width * height, "verified": box.verified,
                })
                annotation.setdefault("iscrowd", 0)
                if box.user:
                    annotation["annotator_id"] = self._annotator_id(box.user)
                annotations.append(annotation)
            self.touched_image_ids.add(local_id)
            self._restore_image_annotations(local_id, annotations)
        # Box ids changed under the undo history, so it no longer applies.
        self._undo_history.clear()
        self._redo_history.clear()
        self.next_annotation_id = max(
            (int(annotation["id"]) for annotation in self._all_annotations()
             if isinstance(annotation.get("id"), int)), default=0,
        ) + 1
        self.dirty = True
        if added:
            self._rebuild_frame_list(show_progress=True)
        else:
            self._apply_frame_filters(select_first=False)
        self._sync_frame_controls()

    def _merge_with_disk(self, save_path: Path) -> bool:
        """Merge the project file someone else saved with this session's work; True to go on saving."""
        signature = self._file_signature_of(save_path)  # taken before reading, like opening a project
        try:
            theirs_bytes = save_path.read_bytes()
            theirs = json.loads(theirs_bytes)
            base = json.loads(zlib.decompress(self._base_json))
        except (OSError, ValueError, zlib.error) as error:
            QMessageBox.critical(self, "Cannot merge", f"Could not read the project files to merge:\n\n{error}")
            return False
        outcome = self._merge_dialog(base, theirs, "the version on disk", "Merge and save")
        if outcome is None:
            return False
        self._apply_merge(outcome)
        # The disk version is now part of this session's work, so it becomes the base and is no longer "newer".
        self._base_json, self._base_missing_state = zlib.compress(theirs_bytes, 6), "review"
        self._file_signature = signature
        return True

    def merge_project(self) -> None:
        """Merge another copy of this project, given the original both copies started from."""
        if not self._editing_allowed():
            return
        start = str((self.annotations_path or self.root).parent) if (self.annotations_path or self.root) else ""
        other, _ = QFileDialog.getOpenFileName(
            self, "Choose the other copy of the project", start, "JSON files (*.json)"
        )
        if not other:
            return
        original, _ = QFileDialog.getOpenFileName(
            self, "Choose the original both copies started from", str(Path(other).parent), "JSON files (*.json)"
        )
        if not original:
            return
        try:
            theirs = json.loads(Path(other).read_bytes())
            base = json.loads(Path(original).read_bytes())
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Cannot merge", f"Could not read the project files to merge:\n\n{error}")
            return
        saved_state = self._base_missing_state
        self._base_missing_state = "review"  # an original picked here is read like any saved file
        try:
            outcome = self._merge_dialog(base, theirs, Path(other).name, "Merge")
        finally:
            self._base_missing_state = saved_state
        if outcome is None:
            return
        self._apply_merge(outcome)
        self.status_label.setText(
            f"Merged {Path(other).name} — unsaved changes. Images from the other copy open from the project "
            "folder; add their folder if they are elsewhere."
        )

    def _save_project(self, save_path: Path | None) -> bool:
        if self.root is None:
            QMessageBox.information(self, "No project", "Create or open a project folder first.")
            return False
        if save_path is not None and not self.dirty:
            return True
        self._commit_current_scene()
        # Only verified and review images are saved; unverified ones can always be rescanned.
        export_images = [image for image in self.images if self._frame_state(image["id"]) != "unverified"]
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
        if any(
            not all(math.isfinite(float(value)) for value in annotation["bbox"])
            for annotation in export_annotations
        ):
            QMessageBox.critical(self, "Invalid box", "A box contains non-finite coordinates; nothing was saved.")
            return False
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
            merge_button = None
            if self._base_json is not None:
                merge_button = message.addButton("Merge…", QMessageBox.ButtonRole.AcceptRole)
            save_as_button = message.addButton("Save As…", QMessageBox.ButtonRole.AcceptRole)
            message.addButton(QMessageBox.StandardButton.Cancel)
            message.setDefaultButton(merge_button or save_as_button)
            message.exec()
            if merge_button is not None and message.clickedButton() is merge_button:
                if not self._merge_with_disk(save_path):
                    return False
                # The merged project now matches the file on disk as its base; save it (rechecked again).
                return self._save_project(save_path)
            if message.clickedButton() is not save_as_button:
                return False
            save_path = None
        if save_path is None:
            save_path = self._choose_json_save_path(
                "Save project as",
                self.annotations_path or self.root.parent / f"{self.root.name}.json",
            )
            if save_path is None:
                return False

        if self._project_image_names is None:
            self._project_image_names = directory_names(self.root)
        existing_names = self._project_image_names
        # Plan every image first; nothing on disk or in memory changes until all checks pass.
        # Each plan entry is (image, target, action): None (already the folder file), "copy" or "existing".
        plan: list[tuple[dict[str, Any], Path, str | None]] = []
        planned_names: set[str] = set()
        conflicts: list[tuple[Path, Path]] = []
        missing: list[Path] = []
        for image in export_images:
            source = self._image_source(image)
            if source is None:
                missing.append(Path(image["file_name"]))
                continue
            target_name = Path(image["file_name"]).name
            target = self.root / target_name
            key = target_name.casefold()
            same_path = isinstance(source, Path) and path_key(source) == path_key(target)
            if same_path:
                if key not in existing_names:
                    missing.append(source)
                else:
                    plan.append((image, target, None))
                continue
            if key in planned_names:
                conflicts.append((source, target))  # two project images with one name
                continue
            planned_names.add(key)
            if key not in existing_names:
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
                + "\n\nAdd the folder or video they come from (File → Add Folder… / Add Video Folder…) "
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
            record.pop("frame_verified", None)  # replaced by frame_state
            record["file_name"] = target.name
            record["frame_state"] = self._frame_state(image["id"])
            record["background"] = self.background_by_image.get(image["id"], False)
            records.append(record)
            exported_ids.add(image["id"])
        packaged_annotations = [
            annotation for annotation in export_annotations
            if annotation["image_id"] in exported_ids
        ]
        project_data = dict(self.annotation_data)
        project_data["images"] = records
        project_data["annotations"] = packaged_annotations
        copies = [
            (self._image_source(image), target, action == "existing")
            for image, target, action in plan
            if action == "copy" or image["id"] in overwrite_ids
        ]
        try:
            saved_base = self._run_in_background(
                "Saving project", f"Saving {save_path.name}…", len(copies) + 1,
                lambda report, cancelled: self._copy_then_write_json(
                    copies, project_data, save_path, report, cancelled
                ),
            )
        except OperationCancelled:
            self.status_label.setText("Save cancelled; no project files were changed.")
            return False
        except OSError as error:
            QMessageBox.critical(
                self, "Save project failed",
                f"{error}\n\nNo project files were changed; images copied during this save were removed.",
            )
            return False

        for image, target, action in plan:
            image["file_name"] = target.name
            image.pop("frame_verified", None)
            image["frame_state"] = self._frame_state(image["id"])
            image["background"] = self.background_by_image.get(image["id"], False)
            # The source stays as it is: an image keeps opening from the folder or video added this session.
            if action == "copy":
                existing_names.add(target.name.casefold())
            if action is not None:
                self._folder_settled.add(image["id"])  # don't ask about this file again until its source changes
        self._base_json, self._base_missing_state = saved_base, "review"  # the saved file is the new common base
        self._discard_autosave(self.annotations_path or self.root)
        self._discard_autosave(save_path)
        self.output_path = save_path
        self.annotations_path = save_path
        self._file_signature = self._file_signature_of(save_path)
        self.persisted_image_ids = exported_ids
        self.touched_image_ids.clear()
        self.dirty = False
        self.status_label.setText(
            f"Saved {len(records):,} image(s) and {len(packaged_annotations):,} annotation boxes "
            f"to {save_path}"
        )
        self._update_frame_stats()
        return True

    def export_training_dataset(self) -> None:
        if self.root is None:
            QMessageBox.information(self, "No project", "Open a project before exporting training data.")
            return
        self._commit_current_scene()
        training_images = []
        training_annotations = []
        for image in self.images:
            image_id = image["id"]
            if not self._frame_is_exportable(image_id):
                continue
            annotations = self.annotations_by_image[image_id]
            verified_annotations = [
                annotation for annotation in annotations
                if is_verified(annotation.get("verified", True))
            ]
            if verified_annotations:
                training_images.append(image)
                training_annotations.extend(verified_annotations)
            elif self.background_by_image.get(image_id, False) and not annotations:
                training_images.append(image)

        if not training_images:
            QMessageBox.information(
                self, "No verified training images",
                "There are no verified images with verified boxes or an explicit background mark.",
            )
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
        for image in training_images:
            source = self._image_source(image)
            if source is None:
                missing.append(Path(image["file_name"]))
                continue
            name = Path(image["file_name"]).name
            target = destination / name
            key = name.casefold()
            same_path = isinstance(source, Path) and path_key(source) == path_key(target)
            if not same_path and (key in existing_names or key in planned_names):
                conflicts.append((source, target))
                continue
            if same_path and key not in existing_names:
                missing.append(source)
                continue
            if not same_path:
                planned_names.add(key)
            plan.append((image, source, target, not same_path))

        if conflicts:
            self._show_duplicate_summary(conflicts, 0)
            return
        if missing:
            QMessageBox.warning(
                self, "Training images not found",
                "Could not package these image paths:\n\n" + "\n".join(str(path) for path in missing[:20]),
            )
            return

        exported_images = []
        for image, _source, target, _needs_copy in plan:
            record = dict(image)
            try:
                record["file_name"] = Path(os.path.relpath(target, save_path.parent)).as_posix()
            except ValueError:  # Different Windows drives have no relative path.
                record["file_name"] = target.as_posix()
            record.pop("frame_verified", None)
            record["frame_state"] = "verified"
            record["background"] = self.background_by_image.get(image["id"], False)
            exported_images.append(record)
        export_data = dict(self.annotation_data)
        export_data["images"] = exported_images
        exported_ids = {image["id"] for image in exported_images}
        export_data["annotations"] = [
            annotation for annotation in training_annotations
            if annotation["image_id"] in exported_ids
        ]
        source_info = export_data.get("info")
        info = dict(source_info) if isinstance(source_info, dict) else {}
        export_data["info"] = info
        info["description"] = "Verified training subset exported from Stingray Label"
        copies = [(source, target, False) for _image, source, target, needs_copy in plan if needs_copy]
        try:
            self._run_in_background(
                "Exporting training dataset", f"Exporting to {destination}…", len(copies) + 1,
                lambda report, cancelled: self._copy_then_write_json(
                    copies, export_data, save_path, report, cancelled
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
        if not self.current_items:
            QMessageBox.information(self, "No annotations", "This image has no boxes to export.")
            return

        padding = 0
        scale_options = None
        if self.show_scale_bar.isChecked():
            options = QDialog(self)
            options.setWindowTitle("Export Crops for Presentation")
            options_layout = QVBoxLayout(options)
            add_padding = QCheckBox("Add padding around each crop")
            add_padding.setChecked(True)
            options_layout.addWidget(add_padding)
            padding_input = QSpinBox()
            padding_input.setRange(0, 1000)
            padding_input.setValue(50)
            padding_input.setSuffix(" px per side")
            padding_input.setToolTip("Padding is limited to source pixels available at image edges.")
            options_layout.addWidget(padding_input)
            options_layout.addWidget(QLabel(
                "At image edges, padding is reduced to the source pixels available."
            ))
            add_padding.toggled.connect(padding_input.setEnabled)
            scale_options_layout = QFormLayout()
            scale_options = self._scale_bar_export_options(scale_options_layout)
            options_layout.addLayout(scale_options_layout)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            buttons.accepted.connect(options.accept)
            buttons.rejected.connect(options.reject)
            options_layout.addWidget(buttons)
            if options.exec() != QDialog.DialogCode.Accepted:
                return
            padding = padding_input.value() if add_padding.isChecked() else 0

        output_dir = QFileDialog.getExistingDirectory(self, "Choose box output folder", str(self.root))
        if not output_dir:
            return
        self._commit_current_scene()
        image_base = Path(self.image_by_id[self.current_image_id]["file_name"]).stem
        saved = 0
        for item in self.current_items:
            x, y, width, height = item.scene_bbox()
            left = max(0, math.floor(x))
            top = max(0, math.floor(y))
            right = min(self.base_image.width, math.ceil(x + width))
            bottom = min(self.base_image.height, math.ceil(y + height))
            if right <= left or bottom <= top:
                continue
            crop_left = max(0, left - padding)
            crop_top = max(0, top - padding)
            crop_right = min(self.base_image.width, right + padding)
            crop_bottom = min(self.base_image.height, bottom + padding)
            roi = self.base_image.crop((crop_left, crop_top, crop_right, crop_bottom))
            roi = self._apply_display_adjustments(roi)
            if scale_options is not None:
                scale_corner, scale_length = scale_options
                self._draw_scale_bar(
                    roi,
                    self._roi_scale_length(roi.width, scale_length.value()),
                    scale_corner.currentData(),
                )
            category = self.categories.get(item.annotation["category_id"], "unknown")
            safe_class = re.sub(r"[^a-zA-Z0-9_.-]+", "_", category)
            file_name = (
                f"{image_base}_{left}_{top}_{roi.width}_{roi.height}_"
                f"{safe_class}_{item.annotation.get('id', saved + 1)}.png"
            )
            try:
                roi.save(Path(output_dir) / file_name, format="PNG")
            except OSError as error:
                QMessageBox.critical(self, "box export failed", str(error))
                return
            saved += 1
        self.status_label.setText(f"Exported {saved} box image(s) to {output_dir}")

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
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.scale_bar_item = None
        self.background_item = None
        self.detail_item = None
        self.base_image = None
        self._loaded = None
        self.current_image_id = None
        self.annotation_list.clear()
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
        for annotation in self.annotations_by_image[image_id]:
            category_name = self.categories.get(annotation["category_id"], "unknown")
            item = BoxItem(annotation, category_name, self._annotation_changed, self._push_undo)
            self.scene.addItem(item)
            self.current_items.append(item)
        self._refresh_annotation_list()
        self.view._manual_zoom = False
        self.view.resetTransform()
        self.view.fit_image()
        self._update_rulers()
        self._update_image_info()
        self._sync_frame_controls()
        self._selection_changed()
        self._update_frame_stats()

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
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.base_image = None
        self._loaded = None
        self.background_item = None
        self.detail_item = None
        self.scale_bar_item = None
        self.current_image_id = None
        self._pending_image_id = None
        self.edit_class.setEnabled(False)
        self.edit_status.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.annotation_list.clear()
        if previous_id is not None:
            self._drop_unused_video_frame(previous_id)
        self._sync_frame_controls()
        self._update_rulers()
        self._update_frame_stats()

    def _cancel_image_adjustment(self) -> None:
        self._adjust_generation += 1
        if self._adjust_cancel is not None:
            self._adjust_cancel.set()
            self._adjust_cancel = None

    def _commit_current_scene(self) -> None:
        if self.current_image_id is None:
            return
        annotations = []
        image = self.image_by_id[self.current_image_id]
        width, height = image.get("width"), image.get("height")
        for item in self.current_items:
            x, y, box_width, box_height = item.scene_bbox()
            right, bottom = x + box_width, y + box_height
            x, y = max(0.0, x), max(0.0, y)
            if width:
                right = min(float(width), right)
            if height:
                bottom = min(float(height), bottom)
            box_width, box_height = right - x, bottom - y
            if box_width <= 0 or box_height <= 0:
                continue
            annotation = item.annotation
            annotation["bbox"] = [x, y, box_width, box_height]
            annotation["area"] = box_width * box_height
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
        self._update_frame_stats()  # also refreshes the image info line

    def _touch(self, image_id: Any) -> None:
        """Record an edit; the first edit of an unverified image marks it Review, so it is saved."""
        self.touched_image_ids.add(image_id)
        if self._frame_state(image_id) == "unverified":
            self.review_by_image[image_id] = True
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
        # Verifying needs a verified box or a background mark; leaving Verified is always allowed.
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
        """An image can be verified only when it is marked background or has a verified box."""
        if self.background_by_image.get(image_id, False):
            return True
        annotations = (
            [item.annotation for item in self.current_items]
            if image_id == self.current_image_id else self.annotations_by_image.get(image_id, [])
        )
        return any(is_verified(annotation.get("verified", True)) for annotation in annotations)

    def _enforce_image_verification(self, image_id: Any) -> None:
        """Drop image verification once its basis is gone (last verified box removed, background cleared)."""
        if self.frame_verified_by_image.get(image_id, False) and not self._has_verification_basis(image_id):
            self.frame_verified_by_image[image_id] = False
            self.review_by_image[image_id] = True  # it still holds work, so it stays saved
            self.touched_image_ids.add(image_id)
            self.dirty = True
            self._update_frame_visibility(image_id)
            self.status_label.setText(
                "Image set back to Review: it no longer has a verified box or a background mark."
            )
        if image_id == self.current_image_id:
            self._sync_frame_controls()

    def _set_frame_state(self, state: str) -> None:
        """Set the current image to verified, review or unverified (buttons, Shift+V and C)."""
        image_id = self.current_image_id
        if image_id not in self.image_by_id:
            return
        if self.current_annotator is None or state == self._frame_state(image_id):
            self._sync_frame_controls()
            return
        if state == "verified" and not self._has_verification_basis(image_id):
            self._sync_frame_controls()
            self.status_label.setText(
                "To verify this image, verify at least one box (V) or mark it as background."
            )
            return
        self.frame_verified_by_image[image_id] = state == "verified"
        self.review_by_image[image_id] = state == "review"
        self.touched_image_ids.add(image_id)
        self.dirty = True
        self.status_label.setText(
            "Image set to Unverified — it will not be saved" if state == "unverified"
            else f"Image set to {state.capitalize()} — unsaved changes"
        )
        self._sync_frame_controls()
        self._update_frame_visibility(image_id)
        self._update_frame_stats()

    def _background_changed(self, checked: bool) -> None:
        image_id = self.current_image_id
        if image_id not in self.image_by_id or self.current_items:
            return
        if self.current_annotator is None:
            self._sync_frame_controls()
            return
        if self.background_by_image.get(image_id, False) == checked:
            return
        self.background_by_image[image_id] = checked
        self._touch(image_id)
        if checked:
            self.draw_button.setChecked(False)
        self.dirty = True
        self.status_label.setText("Background status changed — unsaved changes")
        self._sync_frame_controls()
        self._update_frame_visibility(image_id)
        self._enforce_image_verification(image_id)
        self._update_frame_stats()

    def _set_draw_mode(self, enabled: bool) -> None:
        if enabled and (self.current_annotator is None or self.current_image_id is None
                        or self.background_by_image.get(self.current_image_id, False)):
            self.draw_button.setChecked(False)
            return
        self.view.set_draw_mode(enabled)
        self.draw_button.setText("Cancel drawing" if enabled else "Draw box")

    def _add_rectangle(self, rect: QRectF) -> None:
        if self.current_annotator is None or self.current_image_id is None:
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
        names = sorted(self.categories.values(), key=str.casefold)
        current_name = self.categories.get(self._last_category_id, names[0] if names else "")
        name, accepted = QInputDialog.getItem(
            self, "Choose category", "Select a category, or type a new name to add it:",
            names, names.index(current_name) if current_name in names else 0, True,
        )
        name = name.strip()
        if not accepted or not name:
            return
        category_id = next(
            (key for key, existing in self.categories.items() if existing.casefold() == name.casefold()),
            None,
        )
        if category_id is None:
            category_id = self._create_class(name)  # a new name becomes a category, like Edit → Add category
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

    def _selected_item(self) -> BoxItem | None:
        selected = self.scene.selectedItems()
        return selected[0] if selected and isinstance(selected[0], BoxItem) else None

    def _selection_changed(self) -> None:
        item = self._selected_item()
        self._refresh_annotation_list(item)
        self.edit_class.blockSignals(True)
        self.edit_status.blockSignals(True)
        enabled = item is not None and self.current_annotator is not None
        self.edit_class.setEnabled(enabled and bool(self.categories))
        self.edit_status.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)
        if item:
            class_index = self.edit_class.findData(item.annotation["category_id"])
            self.edit_class.setCurrentIndex(class_index)
            self.edit_status.setChecked(is_verified(item.annotation.get("verified", True)))
        else:
            self.edit_class.setCurrentIndex(0)
            self.edit_status.setChecked(False)
        self.edit_class.blockSignals(False)
        self.edit_status.blockSignals(False)
        self._sync_frame_controls()

    def _refresh_annotation_list(self, selected_item: BoxItem | None = None) -> None:
        if not hasattr(self, "annotation_list"):
            return
        self.annotation_list.blockSignals(True)
        self.annotation_list.clear()
        selected_row = -1
        boxes = [item.scene_bbox() for item in self.current_items]  # live coordinates while editing
        for row, item in enumerate(self.current_items):
            status = "verified" if is_verified(item.annotation.get("verified", True)) else "unverified"
            user_name = self._annotation_user_name(item.annotation)
            text = f"{item.category_name} · {status} · {user_name}"
            best_iou = max(
                (self._box_iou(boxes[row], other) for other_row, other in enumerate(boxes) if other_row != row),
                default=0.0,
            )
            if best_iou > 0:
                text += f" · IoU {best_iou:.2f}"
            list_item = QListWidgetItem(text)
            color_swatch = QPixmap(12, 12)
            color_swatch.fill(item.color())
            list_item.setIcon(QIcon(color_swatch))
            self.annotation_list.addItem(list_item)
            if item is selected_item:
                selected_row = row
        self.annotation_list.setCurrentRow(selected_row)
        self.annotation_list.blockSignals(False)

    def _annotation_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self.current_items):
            return
        item = self.current_items[row]
        self.scene.clearSelection()
        item.setSelected(True)
        self.view.ensureVisible(item)

    def _class_changed(self, index: int) -> None:
        if self.current_annotator is None:
            return
        item = self._selected_item()
        category_id = self.edit_class.itemData(index)
        if item is None or category_id not in self.categories:
            return
        if item.annotation.get("category_id") == category_id:
            return
        self._push_undo()
        item.annotation["category_id"] = category_id
        item.category_name = self.categories[category_id]
        item.update()
        self._refresh_annotation_list(item)
        self._annotation_changed(item.annotation, counts_changed=True)

    def _status_changed(self, checked: bool) -> None:
        if self.current_annotator is None:
            return
        item = self._selected_item()
        if item is None:
            return
        if is_verified(item.annotation.get("verified", True)) == checked:
            return
        self._push_undo()
        item.annotation["verified"] = checked
        item.update()
        self._refresh_annotation_list(item)
        self._annotation_changed(item.annotation, counts_changed=True)
        self._enforce_image_verification(self.current_image_id)

    def _add_class(self) -> None:
        if not self._editing_allowed():
            return
        name, accepted = QInputDialog.getText(self, "Add category", "New category name:")
        name = name.strip()
        if not accepted or not name:
            return
        if any(existing.casefold() == name.casefold() for existing in self.categories.values()):
            QMessageBox.information(self, "Category already exists", f"The category {name!r} is already available.")
            return
        self._create_class(name)
        self._mark_dirty(touch_image=False)

    def _edit_labels(self) -> None:
        if not self._editing_allowed():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit categories")
        layout = QVBoxLayout(dialog)
        label_list = QListWidget(dialog)
        layout.addWidget(label_list)

        def refresh(selected_id: Any = None) -> None:
            label_list.clear()
            for category_id, name in sorted(
                self.categories.items(), key=lambda pair: pair[1].casefold()
            ):
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, category_id)
                label_list.addItem(item)
            if selected_id is not None:
                for row in range(label_list.count()):
                    if label_list.item(row).data(Qt.ItemDataRole.UserRole) == selected_id:
                        label_list.setCurrentRow(row)
                        break
            if label_list.count() and label_list.currentRow() < 0:
                label_list.setCurrentRow(0)

        def selected_id() -> Any:
            item = label_list.currentItem()
            return item.data(Qt.ItemDataRole.UserRole) if item else None

        controls = QHBoxLayout()
        add_button = QPushButton("Add…", dialog)
        rename_button = QPushButton("Edit…", dialog)
        rename_button.setToolTip("Rename; a name that already exists offers to merge into it")
        delete_button = QPushButton("Delete…", dialog)
        color_button = QPushButton("Color…", dialog)
        controls.addWidget(add_button)
        controls.addWidget(rename_button)
        controls.addWidget(delete_button)
        controls.addWidget(color_button)
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def add_label() -> None:
            existing_ids = set(self.categories)
            self._add_class()
            created_id = next(
                (category_id for category_id in self.categories if category_id not in existing_ids),
                None,
            )
            refresh(created_id)

        def rename_label() -> None:
            category_id = selected_id()
            if category_id is not None:
                self._rename_class_id(category_id)
                refresh(category_id if category_id in self.categories else None)

        def color_label() -> None:
            category_id = selected_id()
            if category_id is not None:
                self._choose_class_color(category_id)

        def delete_label() -> None:
            category_id = selected_id()
            if category_id is not None:
                self._delete_class_id(category_id)
                refresh()

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
        category_id = max(numeric_ids, default=0) + 1
        self.categories[category_id] = name
        self.annotation_data.setdefault("categories", []).append({"id": category_id, "name": name})
        if self.edit_class.count() == 1 and self.edit_class.itemData(0) is None:
            self.edit_class.clear()
        self.edit_class.addItem(name, category_id)
        self._sync_frame_controls()
        self.edit_class.setToolTip("")
        self.draw_button.setToolTip("")
        filter_item = QListWidgetItem(name)
        filter_item.setData(Qt.ItemDataRole.UserRole, category_id)
        filter_item.setFlags(filter_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        filter_item.setCheckState(Qt.CheckState.Checked)
        self.class_filter.addItem(filter_item)
        return category_id

    def _rename_class_id(self, category_id: Any) -> None:
        old_name = self.categories.get(category_id)
        if old_name is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Rename category", "Category name:", text=old_name
        )
        name = name.strip()
        if not accepted or not name or name == old_name:
            return
        target_id = next(
            (
                other_id for other_id, existing in self.categories.items()
                if other_id != category_id and existing.casefold() == name.casefold()
            ),
            None,
        )
        if target_id is not None:
            self._commit_current_scene()
            count = sum(
                annotation.get("category_id") == category_id
                for annotations in self.annotations_by_image.values()
                for annotation in annotations
            )
            answer = QMessageBox.question(
                self,
                "Merge categories?",
                f"The category {name!r} already exists. Merge {old_name!r} into {name!r}?\n\n"
                f"This will move {count:,} annotation boxes to {name!r} and remove {old_name!r} from this dataset.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._merge_classes(category_id, target_id)
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
        index = self.edit_class.findData(category_id)
        if index >= 0:
            self.edit_class.setItemText(index, name)
        for row in range(self.class_filter.count()):
            filter_item = self.class_filter.item(row)
            if filter_item.data(Qt.ItemDataRole.UserRole) == category_id:
                filter_item.setText(name)
                break
        self._refresh_annotation_list(self._selected_item())
        self._mark_dirty(touch_image=False)

    def _merge_classes(self, source_id: Any, target_id: Any) -> None:
        self._commit_current_scene()
        source_name = self.categories[source_id]
        target_name = self.categories[target_id]
        for annotations in self.annotations_by_image.values():
            for annotation in annotations:
                if annotation.get("category_id") == source_id:
                    annotation["category_id"] = target_id
                    if self.current_annotator:
                        self._set_annotation_annotator(annotation)
        # Undo must not restore boxes that point at the removed category.
        for annotation in self._history_annotations():
            if annotation.get("category_id") == source_id:
                annotation["category_id"] = target_id
        self._remove_category_entry(source_id)

        for item in self.current_items:
            if item.annotation.get("category_id") == target_id:
                item.category_name = target_name
                item.update()
        for image_id, annotations in self.annotations_by_image.items():
            self._set_image_roi_counts(image_id, annotations)
        self._selection_changed()
        self._mark_dirty(counts_changed=True, touch_image=False)
        self._apply_frame_filters(select_first=False)
        self.status_label.setText(
            f"Merged category {source_name!r} into {target_name!r} — unsaved changes"
        )

    def _remove_category_entry(self, category_id: Any) -> None:
        """Drop a category from the project and the category widgets; its boxes are handled by the caller."""
        self.annotation_data["categories"] = [
            category for category in self.annotation_data.get("categories", [])
            if category.get("id") != category_id
        ]
        del self.categories[category_id]
        BoxItem.class_colors.pop(category_id, None)
        if self._last_category_id == category_id:
            self._last_category_id = None
        combo_index = self.edit_class.findData(category_id)
        if combo_index >= 0:
            self.edit_class.removeItem(combo_index)
        if not self.categories:
            self.edit_class.addItem("object", None)  # placeholder, as for a project with no categories
            self.edit_class.setEnabled(False)
        for row in range(self.class_filter.count()):
            if self.class_filter.item(row).data(Qt.ItemDataRole.UserRole) == category_id:
                self.class_filter.takeItem(row)
                break

    def _delete_class_id(self, category_id: Any) -> None:
        """Delete a category; boxes using it are moved to another category or deleted, as chosen."""
        if not self._editing_allowed() or category_id not in self.categories:
            return
        self._commit_current_scene()
        name = self.categories[category_id]
        used_by = {
            image_id: sum(annotation.get("category_id") == category_id for annotation in annotations)
            for image_id, annotations in self.annotations_by_image.items()
        }
        used_by = {image_id: count for image_id, count in used_by.items() if count}
        if not used_by:
            self._remove_category_entry(category_id)
            self._mark_dirty(touch_image=False)
            self._apply_frame_filters(select_first=False)
            self.status_label.setText(f"Deleted category {name!r} — unsaved changes")
            return
        box_count = sum(used_by.values())
        message = QMessageBox(self)
        message.setWindowTitle("Delete category")
        message.setIcon(QMessageBox.Icon.Warning)
        message.setText(f"{name!r} is used by {box_count:,} box(es) on {len(used_by):,} image(s).")
        message.setInformativeText("Move those boxes to another category, or delete them with the category.")
        move_button = message.addButton("Move boxes to…", QMessageBox.ButtonRole.AcceptRole)
        delete_button = message.addButton("Delete boxes too", QMessageBox.ButtonRole.DestructiveRole)
        message.addButton(QMessageBox.StandardButton.Cancel)
        message.exec()
        clicked = message.clickedButton()
        if clicked is move_button:
            others = sorted(
                (other for other_id, other in self.categories.items() if other_id != category_id),
                key=str.casefold,
            )
            if not others:
                QMessageBox.information(self, "Delete category", "There is no other category to move the boxes to.")
                return
            target, accepted = QInputDialog.getItem(
                self, "Move boxes", f"Move the {box_count:,} {name!r} box(es) to:", others, 0, False
            )
            if accepted and target:
                target_id = next(other_id for other_id, other in self.categories.items() if other == target)
                self._merge_classes(category_id, target_id)
            return
        if clicked is not delete_button:
            return
        confirm = QMessageBox.question(
            self, "Delete boxes",
            f"Delete {box_count:,} box(es) on {len(used_by):,} image(s) together with {name!r}?\n\n"
            "Undo cannot bring them back. Nothing changes on disk until you save.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        for image_id in used_by:
            remaining = [
                annotation for annotation in self.annotations_by_image[image_id]
                if annotation.get("category_id") != category_id
            ]
            self.annotations_by_image[image_id] = remaining
            self._set_image_roi_counts(image_id, remaining)
        # Undo must not restore boxes that point at the deleted category.
        for _image_id, state in (*self._undo_history, *self._redo_history):
            state[:] = [annotation for annotation in state if annotation.get("category_id") != category_id]
        for item in [item for item in self.current_items if item.annotation.get("category_id") == category_id]:
            self.current_items.remove(item)
            self.scene.removeItem(item)
        self._remove_category_entry(category_id)
        self.dirty = True
        for image_id in used_by:
            self._touch(image_id)  # an edit: an unverified image becomes Review
            self._enforce_image_verification(image_id)  # verified images that lost their verified boxes
            self._update_frame_visibility(image_id)
        self._refresh_annotation_list()
        self._selection_changed()
        self._apply_frame_filters(select_first=False)
        self.status_label.setText(
            f"Deleted category {name!r} and {box_count:,} box(es) — unsaved changes"
        )

    def _choose_class_color(self, category_id: Any) -> None:
        if category_id not in self.categories:
            return
        name = self.categories[category_id]
        current_color = BoxItem.class_colors.get(
            category_id,
            QColor.fromHsv((int(category_id) * 137) % 360, 210, 235),
        )
        color = QColorDialog.getColor(current_color, self, f"Color for {name}")
        if not color.isValid():
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

    def _delete_selected(self) -> None:
        if self.current_annotator is None:
            return
        item = self._selected_item()
        if item is None:
            return
        self._push_undo()
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
