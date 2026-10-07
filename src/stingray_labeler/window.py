"""Main image annotation window and user workflows."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

try:
    from PySide6.QtCore import QEvent, QEventLoop, QObject, QPoint, QPointF, QRectF, QSize, Qt, QRunnable, QThreadPool, QTimer, Signal
    from PySide6.QtGui import QAction, QColor, QBrush, QFont, QIcon, QImage, QKeySequence, QPainter, QPalette, QPen, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
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
        QSlider,
        QSpinBox,
        QSplitter,
        QToolButton,
        QVBoxLayout,
        QWidget,
        QWidgetAction,
    )
    from PIL import Image, ImageDraw, ImageFont
except ImportError as error:
    raise SystemExit("Install the GUI dependencies with: python -m pip install PySide6 Pillow") from error

from .dataset import is_verified, dataset_image_path, directory_names, path_key
from .graphics import (
    AnnotationView, BoxItem, ImageRuler, ScaleBarItem, format_length, scale_bar_lengths_mm,
)
from .image_processing import (
    Adjustments, AutoLevelsWorker, DetailWorker, ImageLoadWorker, LoadedImage, PipelineSignals,
    PreviewAdjustmentWorker, apply_image_adjustments,
)

IMAGE_CACHE_SIZE = 3  # current image plus the previous and next ones


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
        self.frame_verified_by_image: dict[Any, bool] = {}
        self.background_by_image: dict[Any, bool] = {}
        self.persisted_image_ids: set[Any] = set()
        self.touched_image_ids: set[Any] = set()
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
        self._image_cache: OrderedDict[Any, LoadedImage] = OrderedDict()
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
        self._shown_ids: set[Any] = set()
        self._user_name_by_id: dict[int, str] = {}
        self._user_id_by_name: dict[str, int] = {}
        self._load_notes: list[str] = []
        self.dirty = False
        self._refreshing = False
        self._undo_history: list[tuple[Any, list[dict[str, Any]]]] = []
        self._redo_history: list[tuple[Any, list[dict[str, Any]]]] = []

        self.setWindowTitle("Stingray Labeler")
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "stingray_label_icon.png")))
        self.resize(1400, 900)
        self._build_ui()
        signals = self.pipeline_signals
        signals.image_loaded.connect(self._image_loaded)
        signals.image_failed.connect(self._image_failed)
        signals.preview_adjusted.connect(self._adjustment_finished)
        signals.detail_ready.connect(self._detail_finished)
        signals.levels_ready.connect(self._auto_levels_finished)
        self._set_dataset(None, {"images": [], "annotations": [], "categories": []})

    def _build_ui(self) -> None:
        self.user_button = QToolButton()
        self.user_button.setText("User: —")
        self.user_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.user_button.setAutoRaise(True)
        self.user_button.setContentsMargins(8, 0, 28, 0)
        self.user_button.setToolTip("Change the active user")
        self.user_button.clicked.connect(self._change_annotator)
        self.menuBar().setCornerWidget(self.user_button, Qt.Corner.TopRightCorner)
        file_menu = self.menuBar().addMenu("&File")
        self.new_project_action = QAction("New Project…", self)
        self.open_project_action = QAction("Open Project…", self)
        self.add_images_action = QAction("Add Images…", self)
        self.add_folder_action = QAction("Add Folder…", self)
        self.save_project_action = QAction("Save Project", self)
        self.save_project_action.setShortcut(QKeySequence("Ctrl+S"))
        self.save_project_action.setToolTip(
            "Save the project JSON to its current file, or choose a file if it has not been saved yet"
        )
        self.save_project_as_action = QAction("Save Project As…", self)
        self.save_project_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.save_project_as_action.setToolTip("Choose a folder and file name for the project JSON")
        self.training_export_action = QAction("Export Training Dataset…", self)
        self.training_export_action.setToolTip(
            "Export verified images, verified boxes, and explicitly marked background images"
        )
        self.save_image_action = QAction("Save Image…", self)
        self.save_rois_action = QAction("Export Crops…", self)
        self.exit_action = QAction("Exit", self)
        self.add_images_action.setEnabled(False)
        self.add_folder_action.setEnabled(False)
        self.save_project_action.setEnabled(False)
        self.save_project_as_action.setEnabled(False)
        self.training_export_action.setEnabled(False)
        for action in (self.new_project_action, self.open_project_action,
                       self.add_images_action, self.add_folder_action,
                       self.save_project_action, self.save_project_as_action,
                       self.training_export_action,
                       self.save_image_action,
                       self.save_rois_action):
            file_menu.addAction(action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

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
        side_layout.addWidget(QLabel("Find image"))
        self.frame_search = QLineEdit()
        self.frame_search.setPlaceholderText("Search filenames…")
        side_layout.addWidget(self.frame_search)
        side_layout.addWidget(QLabel("User"))
        self.annotator_filter = QComboBox()
        self.annotator_filter.addItem("All users", self.ALL_ANNOTATORS_FILTER_ID)
        self.annotator_filter.currentIndexChanged.connect(self._apply_frame_filters)
        side_layout.addWidget(self.annotator_filter)
        side_layout.addWidget(QLabel("Images"))
        self.frame_list = QListWidget()
        side_layout.addWidget(self.frame_list, 1)
        self.frame_stats = QLabel("No image selected")
        self.frame_stats.setWordWrap(True)
        side_layout.addWidget(self.frame_stats)
        class_buttons = QHBoxLayout()
        select_all = QPushButton("All")
        select_none = QPushButton("None")
        class_buttons.addWidget(select_all)
        class_buttons.addWidget(select_none)
        side_layout.addLayout(class_buttons)
        self.class_filter = QListWidget()
        self.class_filter.setMaximumHeight(150)
        side_layout.addWidget(self.class_filter)
        side_layout.addWidget(QLabel("Image verification"))
        self.frame_verification_filter = QComboBox()
        self.frame_verification_filter.addItem("All images", "all")
        self.frame_verification_filter.addItem("Verified images", "verified")
        self.frame_verification_filter.addItem("Unverified images", "unverified")
        side_layout.addWidget(self.frame_verification_filter)
        side_layout.addWidget(QLabel("Box verification"))
        self.verification_filter = QComboBox()
        self.verification_filter.addItem("All boxes", "all")
        self.verification_filter.addItem("Verified", "verified")
        self.verification_filter.addItem("Unverified", "unverified")
        side_layout.addWidget(self.verification_filter)
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
        display_menu = self.menuBar().addMenu("&Display")
        display_menu.addMenu(self.levels_menu)
        display_menu.addMenu(self.scale_menu)
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
        editor_layout.addWidget(self.image_info)

        annotation_panel = QWidget()
        annotation_layout = QVBoxLayout(annotation_panel)
        self.frame_verified_check = QCheckBox("Image verified")
        self.frame_verified_check.setEnabled(False)
        self.frame_verified_check.setToolTip("Toggle image verification (Shift+V)")
        annotation_layout.addWidget(self.frame_verified_check)
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
        self.frame_verified_check.toggled.connect(self._frame_verified_changed)
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
        self.save_project_action.triggered.connect(self.save_project)
        self.save_project_as_action.triggered.connect(self.save_project_as)
        self.training_export_action.triggered.connect(self.export_training_dataset)
        self.save_image_action.triggered.connect(self.export_frame)
        self.save_rois_action.triggered.connect(self.export_rois)
        self.exit_action.triggered.connect(self.close)
        self.undo_action.triggered.connect(self.undo)
        self.redo_action.triggered.connect(self.redo)

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
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.current_items = []
        self.base_image = None
        self.background_item = None
        self.detail_item = None
        self.scale_bar_item = None
        self.current_image_id = None
        self._loaded = None
        self._pending_image_id = None
        self._image_cache.clear()
        self._load_workers.clear()
        self._dataset_generation += 1
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
        self.save_project_action.setEnabled(self.root is not None)
        self.save_project_as_action.setEnabled(self.root is not None)
        self.training_export_action.setEnabled(self.root is not None)
        self.change_annotator_action.setEnabled(self.root is not None)
        self.change_annotator_action.setText(
            f"Change User… ({self.current_annotator})"
            if self.current_annotator else "Choose User…"
        )
        self.user_button.setText(f"User: {self.current_annotator or '—'}")
        self.visible_image_ids = visible_image_ids
        self.annotations_path = source_path.resolve() if source_path else None
        if not keep_output:
            self.output_path = None
        self.annotation_data = annotation_data
        self.images = self.annotation_data.setdefault("images", [])
        self.categories = {
            category["id"]: category["name"]
            for category in self.annotation_data.setdefault("categories", [])
        }
        self.image_by_id = {image["id"]: image for image in self.images}
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
        self.frame_verified_by_image = {
            image["id"]: is_verified(image.get("frame_verified", False))
            if imported_annotations else False
            for image in self.images
        }
        self.background_by_image = {image["id"]: False for image in self.images}
        self.persisted_image_ids = set(self.image_by_id) if imported_annotations else set()
        self.touched_image_ids = set()
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
            self.edit_class.setToolTip("Placeholder only. Add a category from the Edit menu.")
            self.draw_button.setToolTip("Add a category from the Edit menu before drawing.")
        else:
            self.edit_class.setToolTip("")
            self.draw_button.setToolTip("")
        self.edit_class.setEnabled(bool(self.categories))
        self.draw_button.setEnabled(bool(self.categories))
        self.edit_class.blockSignals(False)
        self.frame_search.blockSignals(True)
        self.frame_search.clear()
        self.frame_search.blockSignals(False)
        self.verification_filter.blockSignals(True)
        self.verification_filter.setCurrentIndex(0)
        self.verification_filter.blockSignals(False)
        self.frame_verification_filter.blockSignals(True)
        self.frame_verification_filter.setCurrentIndex(0)
        self.frame_verification_filter.blockSignals(False)
        self._sync_frame_controls()
        self._set_annotation_editing_enabled(self.current_annotator is not None)
        self.setWindowTitle("Stingray Labeler" + (f" — {self.root}" if self.root else ""))
        self._rebuild_frame_list(show_progress=True)

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

    def _activate_annotator(self, name: str) -> None:
        is_new = name not in self._user_id_by_name
        self.current_annotator = name
        self._annotator_id(name)
        self.user_button.setText(f"User: {name}")
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
        self.user_button.setText(f"User: {self.current_annotator or '—'}")
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

        controls = QHBoxLayout()
        rename_button = QPushButton("Rename / merge…", dialog)
        controls.addWidget(rename_button)
        layout.addLayout(controls)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        rename_button.clicked.connect(rename_annotator)
        annotator_list.itemDoubleClicked.connect(lambda _item: rename_annotator())
        refresh()
        dialog.setMinimumSize(420, 360)
        dialog.exec()

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
                annotation_data = self._read_annotation_data(annotations_file)
                if not self._fill_missing_image_verification(annotation_data):
                    return
                self.current_annotator = None
                self._set_dataset(root, annotation_data, annotations_file)
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
        added = 0
        used_ids = {image["id"] for image in self.images}
        numeric_ids = [int(value) for value in used_ids if str(value).isdigit()]
        next_id = max(numeric_ids, default=0) + 1
        for source in (Path(path) for path in paths):
            if source.suffix.lower() not in {
                ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
            }:
                continue
            existing_image = existing_by_name.get(source.name.casefold())
            if existing_image is not None:
                image_id = existing_image["id"]
                current_source = self.source_paths_by_image.get(image_id)
                if current_source is None and self.root is not None:
                    current_source = dataset_image_path(self.root, existing_image["file_name"])
                if current_source is None or path_key(current_source) != path_key(source):
                    duplicates.append((source, current_source or Path("(project image path unavailable)")))
                continue
            while next_id in used_ids:
                next_id += 1
            image = {
                "id": next_id, "file_name": source.name, "width": 0, "height": 0,
                "frame_verified": False, "background": False,
            }
            self.images.append(image)
            self.image_by_id[next_id] = image
            self.source_paths_by_image[next_id] = source
            self.annotations_by_image[next_id] = []
            self.frame_verified_by_image[next_id] = False
            self.background_by_image[next_id] = False
            self.roi_counts_by_image[next_id] = {}
            used_ids.add(next_id)
            existing_by_name[source.name.casefold()] = image
            next_id += 1
            added += 1
        if added:
            if refresh:
                self._rebuild_frame_list(show_progress=True)
        if added:
            self.status_label.setText(f"Added {added:,} image(s). Source files remain in place until Save Project.")
        return added, duplicates

    def _show_duplicate_summary(self, duplicates: list[tuple[Path, Path]], added: int) -> None:
        details = "\n\n".join(
            f"Filename: {source.name}\nNew source: {source}\nAlready in project: {existing}"
            for source, existing in duplicates
        )
        message = QMessageBox(self)
        message.setWindowTitle("Images already in project")
        message.setIcon(QMessageBox.Icon.Warning)
        message.setText(f"Added {added:,} image(s); found {len(duplicates):,} duplicate filename(s).")
        message.setInformativeText(
            "Duplicate basenames from different paths were skipped. Review the two paths; "
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

    def _fill_missing_image_verification(self, annotation_data: dict[str, Any]) -> bool:
        """Ask how to treat images saved without a frame_verified value; False cancels opening."""
        missing = [image for image in annotation_data["images"] if "frame_verified" not in image]
        if not missing:
            return True
        message = QMessageBox(self)
        message.setWindowTitle("Image verification not recorded")
        message.setIcon(QMessageBox.Icon.Question)
        message.setText(
            f"{len(missing):,} of {len(annotation_data['images']):,} image(s) in this file "
            "have no image verification status."
        )
        message.setInformativeText(
            "Choose how to treat them. Unverified images are left out of training exports "
            "until someone marks them verified. Verified images without boxes are treated as background."
        )
        unverified_button = message.addButton("Mark unverified", QMessageBox.ButtonRole.AcceptRole)
        verified_button = message.addButton("Mark verified", QMessageBox.ButtonRole.AcceptRole)
        message.addButton(QMessageBox.StandardButton.Cancel)
        message.setDefaultButton(unverified_button)
        message.exec()
        clicked = message.clickedButton()
        if clicked not in (unverified_button, verified_button):
            return False
        for image in missing:
            image["frame_verified"] = clicked is verified_button
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
            return json.loads(contents)

        try:
            annotation_data = self._run_in_background(
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
        progress = QProgressDialog("Scanning image folder…", "Cancel", 0, 0, self)
        progress.setWindowTitle("Loading images")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        progress.raise_()
        QApplication.processEvents()
        paths = []
        count = 0
        for directory, _subdirectories, filenames in os.walk(root):
            for filename in filenames:
                count += 1
                path = Path(directory) / filename
                if path.suffix.lower() in extensions:
                    paths.append(path)
                if count % 100 == 0:
                    progress.setLabelText(f"Scanning image folder…  {len(paths):,} images found")
                    QApplication.processEvents()
                    if progress.wasCanceled():
                        progress.close()
                        return None
        progress.close()
        return sorted(paths, key=lambda path: path.relative_to(root).as_posix().casefold())

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
            for item in self.current_items:
                self.scene.removeItem(item)
            self.current_items = []
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
        self._update_frame_stats()

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
        ordered_images = sorted(
            self.images,
            key=lambda image: str(
                self.source_paths_by_image.get(
                    image["id"], (self.root or Path()) / image["file_name"]
                )
            ).casefold(),
        )
        self._refreshing = True
        self.frame_list.blockSignals(True)
        self.frame_list.setUpdatesEnabled(False)
        self.frame_list.clear()
        self._frame_items = {}
        self._frame_names = {}
        for image in ordered_images:
            image_name = Path(image["file_name"]).name
            item = QListWidgetItem(image_name)
            item.setData(Qt.ItemDataRole.UserRole, image["id"])
            self.frame_list.addItem(item)
            self._frame_items[image["id"]] = item
            self._frame_names[image["id"]] = image_name.casefold()
        self.frame_list.setUpdatesEnabled(True)
        self.frame_list.blockSignals(False)
        self._refreshing = False
        if progress:
            progress.close()
        self._apply_frame_filters()

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
        }

    def _image_matches(self, image_id: Any, context: dict[str, Any]) -> bool:
        if self.visible_image_ids is not None and image_id not in self.visible_image_ids:
            return False
        if context["search"] and context["search"] not in self._frame_names.get(image_id, ""):
            return False
        frame_verified = self.frame_verified_by_image.get(image_id, False)
        frame_verification = context["frame_verification"]
        if (frame_verification == "verified" and not frame_verified) or (
            frame_verification == "unverified" and frame_verified
        ):
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
        if select_first and active_id not in shown:
            first = self._first_shown_item()
            if first is not None:
                self._select_frame_item(first)
            elif active_id is not None:
                self._clear_scene()
        self._update_navigation_buttons()
        self._update_frame_stats()

    def _update_frame_visibility(self, image_id: Any) -> None:
        """Re-check one image after it was edited; the rest of the list is untouched."""
        item = self._frame_items.get(image_id)
        if item is None:
            return
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
        enough = len(self._shown_ids) > 1
        self.previous_button.setEnabled(enough)
        self.next_button.setEnabled(enough)

    def _update_frame_stats(self) -> None:
        shown_ids = self._shown_ids
        selected = self._selected_category_ids()
        verification = self.verification_filter.currentData()
        verified_frames = sum(
            self.frame_verified_by_image.get(image_id, False) for image_id in shown_ids
        )
        unverified_frames = len(shown_ids) - verified_frames
        total_verified_frames = sum(self.frame_verified_by_image.values())
        total_unverified_frames = len(self.images) - total_verified_frames
        verified_filtered = unverified_filtered = 0
        for image_id in shown_ids:
            for category_id, counts in self.roi_counts_by_image.get(image_id, {}).items():
                if category_id not in selected:
                    continue
                if verification in ("all", "verified"):
                    verified_filtered += counts[0]
                if verification in ("all", "unverified"):
                    unverified_filtered += counts[1]
        self.frame_stats.setText(
            ("No images match these filters\n" if self.images and not shown_ids else "")
            + f"Images: {len(shown_ids):,} filtered / {len(self.images):,}\n"
            f"Verified images: {verified_frames:,} / {total_verified_frames:,}\n"
            f"Unverified images: {unverified_frames:,} / {total_unverified_frames:,}\n"
            f"Verified boxes: {verified_filtered:,} / {self.roi_totals[0]:,}\n"
            f"Unverified boxes: {unverified_filtered:,} / {self.roi_totals[1]:,}"
        )
        self._update_image_info()

    def _update_image_info(self) -> None:
        if self.current_image_id not in self.image_by_id:
            self.image_name_label.setText("")
            self.image_info.setText("No image selected")
            return
        image = self.image_by_id[self.current_image_id]
        image_name = Path(image["file_name"]).name
        self.image_name_label.setText(image_name)
        self.image_name_label.setToolTip(image["file_name"])
        width, height = self.base_image.size if self.base_image is not None else (
            image.get("width", 0), image.get("height", 0)
        )
        verified = sum(is_verified(item.annotation.get("verified", True)) for item in self.current_items)
        unverified = len(self.current_items) - verified
        self.image_info.setText(
            f"Image: {width:,} × {height:,} px  |  Boxes: {len(self.current_items)}  |  "
            f"Verified: {verified}  |  Unverified: {unverified}"
        )

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
            self._load_frame(current.data(Qt.ItemDataRole.UserRole))

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if QApplication.activeWindow() is self and event.type() == QEvent.Type.KeyPress:
            focus = QApplication.focusWidget()
            if isinstance(focus, (QLineEdit, QComboBox, QSlider, QDoubleSpinBox)):
                return super().eventFilter(watched, event)

            def focus_within(widget: QWidget) -> bool:
                return focus is widget or (focus is not None and widget.isAncestorOf(focus))

            in_annotation_ui = focus_within(self.centralWidget())
            if event.modifiers() == Qt.KeyboardModifier.ShiftModifier and event.key() == Qt.Key.Key_V:
                if in_annotation_ui and self.frame_verified_check.isEnabled():
                    self.frame_verified_check.toggle()
                    return True
            if event.modifiers() == Qt.KeyboardModifier.NoModifier:
                if event.key() == Qt.Key.Key_Left:
                    self._navigate_frames(-1)
                    return True
                if event.key() == Qt.Key.Key_Right:
                    self._navigate_frames(1)
                    return True
                if in_annotation_ui and event.key() == Qt.Key.Key_B and self.draw_button.isEnabled():
                    self.draw_button.toggle()
                    return True
                if in_annotation_ui and event.key() == Qt.Key.Key_V and self.edit_status.isEnabled():
                    self.edit_status.toggle()
                    return True
                if in_annotation_ui and event.key() == Qt.Key.Key_Escape and self.draw_button.isChecked():
                    self.draw_button.setChecked(False)
                    return True
                in_roi_ui = focus_within(self.view) or focus_within(self.annotation_list)
                if in_roi_ui and event.key() == Qt.Key.Key_Delete and self.delete_button.isEnabled():
                    self._delete_selected()
                    return True
        return super().eventFilter(watched, event)

    def _navigate_frames(self, step: int) -> None:
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
    def _copy_then_write_json(copies: list[tuple[Path, Path]], data: dict[str, Any],
                              save_path: Path, report, cancelled: threading.Event) -> None:
        """Copy images, then write the JSON; on any failure remove the copies made here."""
        created: list[Path] = []
        try:
            for index, (source, target) in enumerate(copies, start=1):
                if cancelled.is_set():
                    raise OperationCancelled()
                if target.exists():
                    raise OSError(f"{target} already exists and was not overwritten.")
                report(index - 1, f"Copying {source.name} ({index:,} of {len(copies):,})…")
                created.append(target)
                shutil.copy2(source, target)
            if cancelled.is_set():
                raise OperationCancelled()
            report(len(copies), f"Writing {save_path.name}…")
            text = json.dumps(data, indent=2) + "\n"
            temporary_path = save_path.with_name(save_path.name + ".tmp")
            temporary_path.write_text(text, encoding="utf-8")
            temporary_path.replace(save_path)
        except BaseException:
            for path in created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise

    def save_project(self) -> bool:
        return self._save_project(self.annotations_path)

    def save_project_as(self) -> bool:
        return self._save_project(None)

    def _save_project(self, save_path: Path | None) -> bool:
        if self.root is None:
            QMessageBox.information(self, "No project", "Create or open a project folder first.")
            return False
        if save_path is not None and not self.dirty:
            return True
        self._commit_current_scene()
        export_images = [
            image for image in self.images
            if self._frame_is_exportable(image["id"])
            or self._frame_has_work(image["id"])
            or image["id"] in self.persisted_image_ids
            or image["id"] in self.touched_image_ids
        ]
        if not export_images and not self.dirty:
            QMessageBox.information(
                self, "Nothing to save yet",
                "No verified images or images with annotation work are ready to save. "
                "Untouched candidates from a folder scan stay out of the project JSON.",
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
        plan: list[tuple[dict[str, Any], Path, bool]] = []
        planned_names: set[str] = set()
        conflicts: list[tuple[Path, Path]] = []
        missing: list[Path] = []
        for image in export_images:
            source = self._source_image_path(image)
            if source is None:
                missing.append(Path(image["file_name"]))
                continue
            target_name = Path(image["file_name"]).name
            target = self.root / target_name
            key = target_name.casefold()
            same_path = path_key(source) == path_key(target)
            if not same_path and (key in existing_names or key in planned_names):
                conflicts.append((source, target))
                continue
            if same_path and key not in existing_names:
                missing.append(source)
                continue
            if not same_path:
                planned_names.add(key)
            plan.append((image, target, not same_path))

        if conflicts:
            self._show_duplicate_summary(conflicts, 0)
            return False
        if missing:
            QMessageBox.warning(
                self, "Images not found in project folder",
                "These image paths were not present in the project image folder and could not be packaged:\n\n"
                + "\n".join(str(path) for path in missing[:20]),
            )
            return False

        records = []
        exported_ids = set()
        for image, target, _needs_copy in plan:
            record = dict(image)
            record["file_name"] = target.name
            record["frame_verified"] = self.frame_verified_by_image.get(image["id"], False)
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
            (self._source_image_path(image), target)
            for image, target, needs_copy in plan if needs_copy
        ]
        try:
            self._run_in_background(
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

        for image, target, needs_copy in plan:
            image["file_name"] = target.name
            image["frame_verified"] = self.frame_verified_by_image.get(image["id"], False)
            image["background"] = self.background_by_image.get(image["id"], False)
            self.source_paths_by_image[image["id"]] = target
            if needs_copy:
                existing_names.add(target.name.casefold())
        self.output_path = save_path
        self.annotations_path = save_path
        self.persisted_image_ids = exported_ids
        self.touched_image_ids.clear()
        self.dirty = False
        self.status_label.setText(
            f"Saved {len(records):,} image(s) and {len(packaged_annotations):,} annotation boxes "
            f"to {save_path}"
        )
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
            source = self._source_image_path(image)
            if source is None:
                missing.append(Path(image["file_name"]))
                continue
            name = Path(image["file_name"]).name
            target = destination / name
            key = name.casefold()
            same_path = path_key(source) == path_key(target)
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
            record["frame_verified"] = True
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
        copies = [(source, target) for _image, source, target, needs_copy in plan if needs_copy]
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
        if image_id in self._image_cache or key in self._load_workers:
            return
        path = self._image_path(image_id)
        if path is None:
            return
        worker = ImageLoadWorker(key, path, self.pipeline_signals)
        self._load_workers[key] = worker
        self.image_pool.start(worker)

    def _cache_image(self, image_id: Any, loaded: LoadedImage) -> None:
        self._image_cache[image_id] = loaded
        self._image_cache.move_to_end(image_id)
        keep = {self.current_image_id, self._pending_image_id}
        for cached_id in list(self._image_cache):
            if len(self._image_cache) <= IMAGE_CACHE_SIZE:
                break
            if cached_id not in keep:
                del self._image_cache[cached_id]

    def _prefetch_neighbors(self) -> None:
        for step in (-1, 1):
            item = self._shown_neighbor(step)
            if item is None:
                continue
            image_id = item.data(Qt.ItemDataRole.UserRole)
            if image_id in self._image_cache:
                self._image_cache.move_to_end(image_id)
            else:
                self._request_image(image_id)

    def _image_loaded(self, key: tuple[int, Any], loaded: LoadedImage) -> None:
        self._load_workers.pop(key, None)
        generation, image_id = key
        if generation != self._dataset_generation or image_id not in self.image_by_id:
            return
        self._cache_image(image_id, loaded)
        if image_id == self._pending_image_id:
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
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(False)
        if self.view.calibration_mode:
            self.calibrate_scale_button.setChecked(False)
        self._commit_current_scene()
        self.view.clear_drawing_guides()
        self._cancel_image_adjustment()
        self._detail_generation += 1
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.current_items = []
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
        cached = self._image_cache.get(image_id)
        if cached is not None:
            self._image_cache.move_to_end(image_id)
            self._show_image(image_id, cached)
            return
        name = Path(self.image_by_id[image_id]["file_name"]).name
        self.image_name_label.setText(name)
        self.image_info.setText(f"Loading {name}…")
        self._request_image(image_id)

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
        self._prefetch_neighbors()

    def _clear_scene(self) -> None:
        if self.view.calibration_mode:
            self.calibrate_scale_button.setChecked(False)
        self._commit_current_scene()
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(False)
        self.view.clear_drawing_guides()
        self._cancel_image_adjustment()
        self._detail_generation += 1
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.current_items = []
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
            self.touched_image_ids.add(self.current_image_id)
            # An edit can only change whether this one image matches the filters.
            self._commit_current_scene()
            self._update_frame_visibility(self.current_image_id)
        self.status_label.setText("Unsaved annotation edits")
        if counts_changed:
            self._refresh_current_roi_counts()
            self._update_frame_stats()
        else:
            self._update_image_info()

    def _annotation_changed(
        self, annotation: dict[str, Any], *, counts_changed: bool = False,
    ) -> None:
        if self.current_annotator:
            self._set_annotation_annotator(annotation)
        self._mark_dirty(counts_changed=counts_changed)

    def _sync_frame_controls(self) -> None:
        image_id = self.current_image_id
        active = image_id in self.image_by_id
        has_rois = bool(self.current_items) if active else False
        can_edit = active and self.current_annotator is not None
        self.frame_verified_check.blockSignals(True)
        self.frame_verified_check.setChecked(
            self.frame_verified_by_image.get(image_id, False) if active else False
        )
        self.frame_verified_check.setEnabled(can_edit)
        self.frame_verified_check.blockSignals(False)
        self.background_check.blockSignals(True)
        self.background_check.setChecked(
            self.background_by_image.get(image_id, False) if active else False
        )
        self.background_check.setVisible(True)
        self.background_check.setEnabled(can_edit and not has_rois)
        self.background_check.blockSignals(False)
        can_draw = (
            can_edit and bool(self.categories)
            and not self.background_by_image.get(image_id, False)
        )
        self.draw_button.setEnabled(can_draw)
        if not can_draw and self.draw_button.isChecked():
            self.draw_button.setChecked(False)

    def _frame_verified_changed(self, checked: bool) -> None:
        image_id = self.current_image_id
        if image_id not in self.image_by_id:
            return
        if self.current_annotator is None:
            self._sync_frame_controls()
            return
        if self.frame_verified_by_image.get(image_id, False) == checked:
            return
        self.frame_verified_by_image[image_id] = checked
        self.touched_image_ids.add(image_id)
        self.dirty = True
        self.status_label.setText("Image verification changed — unsaved changes")
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
        self.touched_image_ids.add(image_id)
        if checked:
            self.draw_button.setChecked(False)
        self.dirty = True
        self.status_label.setText("Background status changed — unsaved changes")
        self._sync_frame_controls()
        self._update_frame_visibility(image_id)
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
        if not names:
            return
        current_name = self.categories.get(self.edit_class.currentData(), names[0])
        name, accepted = QInputDialog.getItem(
            self, "Choose category", "Select an existing category:",
            names, names.index(current_name) if current_name in names else 0, False,
        )
        if not accepted or not name:
            return
        category_id = next(
            (key for key, existing in self.categories.items() if existing.casefold() == name.casefold()),
            None,
        )
        if category_id is None:
            return
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
        for row, item in enumerate(self.current_items):
            status = "verified" if is_verified(item.annotation.get("verified", True)) else "unverified"
            user_name = self._annotation_user_name(item.annotation)
            text = f"{item.category_name} · {status} · User: {user_name}"
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
        add_button = QPushButton("Add category…", dialog)
        rename_button = QPushButton("Rename / merge…", dialog)
        color_button = QPushButton("Color…", dialog)
        controls.addWidget(add_button)
        controls.addWidget(rename_button)
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

        add_button.clicked.connect(add_label)
        rename_button.clicked.connect(rename_label)
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
        self.annotation_data["categories"] = [
            category for category in self.annotation_data.get("categories", [])
            if category.get("id") != source_id
        ]
        del self.categories[source_id]
        BoxItem.class_colors.pop(source_id, None)

        combo_index = self.edit_class.findData(source_id)
        if combo_index >= 0:
            self.edit_class.removeItem(combo_index)
        for row in range(self.class_filter.count()):
            if self.class_filter.item(row).data(Qt.ItemDataRole.UserRole) == source_id:
                self.class_filter.takeItem(row)
                break

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

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self.dirty:
            event.accept()
            return
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "Save annotation changes before closing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            self.save_project()
            event.accept() if not self.dirty else event.ignore()
        elif answer == QMessageBox.StandardButton.Discard:
            event.accept()
        else:
            event.ignore()




