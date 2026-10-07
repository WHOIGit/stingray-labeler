"""Main image annotation window and user workflows."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

try:
    from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QSize, Qt, QRunnable, QThreadPool, QTimer, Signal
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
from .image_processing import AutoLevelsWorker, ImageAdjustmentWorker, apply_image_adjustments


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
        self._adjust_generation = 0
        self._adjust_cancel: threading.Event | None = None
        self._adjust_workers: dict[int, ImageAdjustmentWorker] = {}
        self._auto_level_workers: dict[int, AutoLevelsWorker] = {}
        self._auto_level_generation = 0
        self.adjustment_pool = QThreadPool(self)
        self.adjustment_pool.setMaxThreadCount(2)
        self.dirty = False
        self._refreshing = False
        self._undo_history: list[tuple[Any, list[dict[str, Any]]]] = []
        self._redo_history: list[tuple[Any, list[dict[str, Any]]]] = []

        self.setWindowTitle("Stingray Labeler")
        self.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "stingray_label_icon.png")))
        self.resize(1400, 900)
        self._build_ui()
        self._set_dataset(None, {"images": [], "annotations": [], "categories": []})

    def _build_ui(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.new_project_action = QAction("New Project…", self)
        self.open_project_action = QAction("Open Project…", self)
        self.add_images_action = QAction("Add Images…", self)
        self.add_folder_action = QAction("Add Folder…", self)
        self.save_project_action = QAction("Save Project", self)
        self.save_project_action.setShortcut(QKeySequence("Ctrl+S"))
        self.save_project_action.setToolTip(
            "Save the project JSON beside the image folder and include verified images and images with annotation work"
        )
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
        self.training_export_action.setEnabled(False)
        for action in (self.new_project_action, self.open_project_action,
                       self.add_images_action, self.add_folder_action,
                       self.save_project_action, self.training_export_action,
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
        self.add_class_action = QAction("Add Label…", self)
        self.rename_class_action = QAction("Rename Label…", self)
        self.class_color_action = QAction("Label Color…", self)
        self.import_classes_action = QAction("Import Labels from JSON…", self)
        self.import_classes_action.setEnabled(False)
        for action in (self.add_class_action, self.rename_class_action, self.class_color_action):
            edit_menu.addAction(action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.import_classes_action)
        self.change_annotator_action = QAction("Change Annotator…", self)
        self.change_annotator_action.setEnabled(False)
        edit_menu.addSeparator()
        edit_menu.addAction(self.change_annotator_action)

        container = QWidget()
        layout = QHBoxLayout(container)
        splitter = QSplitter()

        sidebar = QWidget()
        side_layout = QVBoxLayout(sidebar)
        side_layout.addWidget(QLabel("Find image"))
        self.frame_search = QLineEdit()
        self.frame_search.setPlaceholderText("Search filenames…")
        side_layout.addWidget(self.frame_search)
        side_layout.addWidget(QLabel("Annotator"))
        self.annotator_filter = QComboBox()
        self.annotator_filter.addItem("All annotators", self.ALL_ANNOTATORS_FILTER_ID)
        self.annotator_filter.currentIndexChanged.connect(self.refresh_frame_list)
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
        annotation_layout.addWidget(QLabel("Label"))
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

        self.class_filter.itemChanged.connect(self.refresh_frame_list)
        self.frame_verification_filter.currentIndexChanged.connect(self.refresh_frame_list)
        self.verification_filter.currentIndexChanged.connect(self.refresh_frame_list)
        self.frame_search.textChanged.connect(self.refresh_frame_list)
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
        self.scene.selectionChanged.connect(self._selection_changed)
        self.annotation_list.currentRowChanged.connect(self._annotation_row_changed)
        self.edit_class.currentIndexChanged.connect(self._class_changed)
        self.edit_status.toggled.connect(self._status_changed)
        self.delete_button.clicked.connect(self._delete_selected)
        self.add_class_action.triggered.connect(self._add_class)
        self.rename_class_action.triggered.connect(self._rename_class)
        self.class_color_action.triggered.connect(self._choose_class_color)
        self.import_classes_action.triggered.connect(self._import_classes)
        self.change_annotator_action.triggered.connect(self._change_annotator)
        select_all.clicked.connect(lambda: self._set_all_classes(True))
        select_none.clicked.connect(lambda: self._set_all_classes(False))
        self.new_project_action.triggered.connect(self.new_project)
        self.open_project_action.triggered.connect(self.open_project)
        self.add_images_action.triggered.connect(self.add_images)
        self.add_folder_action.triggered.connect(self.add_folder)
        self.save_project_action.triggered.connect(self.save_project)
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
            raise ValueError("Labels must be a JSON array")
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
            raise ValueError("Each label entry must have an id and name")
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
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.current_items = []
        self.base_image = None
        self.background_item = None
        self.scale_bar_item = None
        self.current_image_id = None
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
        self.training_export_action.setEnabled(self.root is not None)
        self.import_classes_action.setEnabled(self.root is not None)
        self.change_annotator_action.setEnabled(self.root is not None)
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
            image["id"]: is_verified(image.get("frame_verified", True))
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
        self._refresh_annotator_filter(reset=True)
        self.class_filter.blockSignals(True)
        self._fill_class_filter()
        self.class_filter.blockSignals(False)
        self.edit_class.blockSignals(True)
        self.edit_class.clear()
        for category_id, name in sorted(self.categories.items(), key=lambda pair: pair[1].lower()):
            self.edit_class.addItem(name, category_id)
        if not self.categories:
            self.edit_class.addItem("object", None)
            self.edit_class.setToolTip("Placeholder only. Add a real label from the Edit menu.")
            self.draw_button.setToolTip("Add a label from the Edit menu before drawing.")
        else:
            self.edit_class.setToolTip("")
            self.draw_button.setToolTip("")
        self.edit_class.setEnabled(bool(self.categories))
        self.draw_button.setEnabled(bool(self.categories))
        self.edit_class.blockSignals(False)
        self.frame_search.clear()
        self.verification_filter.blockSignals(True)
        self.verification_filter.setCurrentIndex(0)
        self.verification_filter.blockSignals(False)
        self.frame_verification_filter.blockSignals(True)
        self.frame_verification_filter.setCurrentIndex(0)
        self.frame_verification_filter.blockSignals(False)
        self._sync_frame_controls()
        self.setWindowTitle("Stingray Labeler" + (f" — {self.root}" if self.root else ""))
        self.refresh_frame_list(show_progress=True)
        if not self.images:
            self._update_frame_stats()

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

    def _annotator_id(self, name: str) -> int:
        annotators = self.annotation_data.setdefault("annotators", [])
        for annotator in annotators:
            if isinstance(annotator, dict) and annotator.get("name") == name:
                return int(annotator["id"])
        next_id = max(
            (int(annotator.get("id", 0)) for annotator in annotators if isinstance(annotator, dict)),
            default=0,
        ) + 1
        annotators.append({"id": next_id, "name": name})
        return next_id

    def _set_annotation_annotator(self, annotation: dict[str, Any]) -> None:
        if self.current_annotator:
            annotation["annotator_id"] = self._annotator_id(self.current_annotator)
            annotation.pop("Annotator", None)

    def _activate_annotator(self, name: str) -> None:
        existing_names = {
            annotator.get("name")
            for annotator in self.annotation_data.get("annotators", [])
            if isinstance(annotator, dict)
        }
        self.current_annotator = name
        self._annotator_id(name)
        self.change_annotator_action.setText(f"Change Annotator… ({name})")
        self._refresh_annotator_filter(reset=True)
        if name not in existing_names:
            self.dirty = True
            self.status_label.setText(f"Unsaved annotator record: {name}")
        else:
            self.status_label.setText(f"Active annotator: {name}")

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
        if choices:
            dialog = QInputDialog(self)
            dialog.setWindowTitle("Select annotator")
            dialog.setLabelText("Choose an existing annotator or enter a new name:")
            dialog.setInputMode(QInputDialog.InputMode.ComboBoxInput)
            dialog.setComboBoxItems(choices)
            dialog.setComboBoxEditable(True)
            dialog.setTextValue(self.current_annotator if self.current_annotator in choices else choices[0])
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            value = dialog.textValue().strip()
        else:
            value, accepted = QInputDialog.getText(
                self, "Create annotator", "Enter your annotator name:"
            )
            value = value.strip()
        if not accepted:
            return None
        if not value:
            QMessageBox.information(self, "Annotator required", "Enter or select an annotator to open this project.")
            return None
        return value

    def _change_annotator(self) -> None:
        if self.root is None:
            return
        value = self._choose_annotator(self.annotation_data, include_current=True)
        if value is None:
            return
        self._activate_annotator(value)

    def _refresh_annotator_filter(self, *, reset: bool = False) -> None:
        if not hasattr(self, "annotator_filter"):
            return
        previous = (
            self.ALL_ANNOTATORS_FILTER_ID if reset
            else self.annotator_filter.currentData()
        )
        names = {
            value.strip()
            for annotator in self.annotation_data.get("annotators", [])
            if isinstance(annotator, dict)
            and isinstance((value := annotator.get("name")), str)
            and value.strip()
        }
        if self.current_annotator:
            names.add(self.current_annotator)
        self.annotator_filter.blockSignals(True)
        self.annotator_filter.clear()
        self.annotator_filter.addItem("All annotators", self.ALL_ANNOTATORS_FILTER_ID)
        for name in sorted(names, key=str.casefold):
            self.annotator_filter.addItem(name, name)
        self.annotator_filter.addItem(
            "Background / no annotations", self.BACKGROUND_ANNOTATOR_FILTER_ID
        )
        index = self.annotator_filter.findData(previous)
        self.annotator_filter.setCurrentIndex(index if index >= 0 else 0)
        self.annotator_filter.blockSignals(False)

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
        annotator = self._choose_annotator(annotation_data)
        if annotator is None:
            return
        self._set_dataset(root, annotation_data)
        self._activate_annotator(annotator)
        added, duplicates = self._add_source_images(paths, refresh=False)
        if duplicates:
            self._show_duplicate_summary(duplicates, added)
        self.refresh_frame_list(show_progress=True)
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
                annotator = self._choose_annotator(annotation_data)
                if annotator is None:
                    return
                self._set_dataset(root, annotation_data, annotations_file)
                self._activate_annotator(annotator)
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
        annotator = self._choose_annotator(annotation_data)
        if annotator is None:
            return
        self._set_dataset(root, annotation_data)
        self._activate_annotator(annotator)
        added, duplicates = self._add_source_images(paths, refresh=False)
        if duplicates:
            self._show_duplicate_summary(duplicates, added)
        self.refresh_frame_list(show_progress=True)
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
                self.refresh_frame_list(show_progress=True)
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

    def _read_annotation_data(self, path: Path) -> dict[str, Any]:
        size = path.stat().st_size
        progress = QProgressDialog("Reading annotations.json…", "Cancel", 0, max(1, size), self)
        progress.setWindowTitle("Loading annotations")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        progress.raise_()
        QApplication.processEvents()
        contents = bytearray()
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(4 * 1024 * 1024):
                    contents.extend(chunk)
                    progress.setValue(len(contents))
                    QApplication.processEvents()
                    if progress.wasCanceled():
                        raise ValueError("Loading annotations was cancelled")
            progress.setRange(0, 0)
            progress.setLabelText("Parsing annotations.json…")
            QApplication.processEvents()
            annotation_data = json.loads(contents)
        finally:
            progress.close()
        if not isinstance(annotation_data, dict) or not isinstance(annotation_data.get("images"), list):
            raise ValueError("Annotation JSON must contain an images array")
        for image in annotation_data["images"]:
            if not isinstance(image, dict) or not isinstance(image.get("file_name"), str):
                raise ValueError("Each image entry must have a file_name")
            image["file_name"] = image["file_name"].replace("\\", "/")
        return annotation_data

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
        return self.current_image_id, json.loads(json.dumps(
            self.annotations_by_image[self.current_image_id]
        ))

    def _push_undo(self) -> None:
        if self.current_image_id is None:
            return
        self._undo_history.append(self._snapshot())
        if len(self._undo_history) > 100:
            self._undo_history.pop(0)
        self._redo_history.clear()

    def undo(self) -> None:
        if not self._undo_history:
            return
        image_id, state = self._undo_history.pop()
        self._redo_history.append(self._snapshot_image_annotations(image_id))
        self._restore_image_annotations(image_id, state)
        self.dirty = True
        self.status_label.setText("Undo — unsaved changes")

    def redo(self) -> None:
        if not self._redo_history:
            return
        image_id, state = self._redo_history.pop()
        self._undo_history.append(self._snapshot_image_annotations(image_id))
        self._restore_image_annotations(image_id, state)
        self.dirty = True
        self.status_label.setText("Redo — unsaved changes")

    def _snapshot_image_annotations(self, image_id: Any) -> tuple[Any, list[dict[str, Any]]]:
        if image_id == self.current_image_id:
            self._commit_current_scene()
        annotations = self.annotations_by_image.get(image_id, [])
        return image_id, json.loads(json.dumps(annotations))

    def _restore_image_annotations(self, image_id: Any, annotations: list[dict[str, Any]]) -> None:
        if image_id not in self.image_by_id:
            return
        restored = json.loads(json.dumps(annotations))
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
        self.annotation_data["annotations"] = [
            annotation for image in self.images
            for annotation in self.annotations_by_image[image["id"]]
        ]
        self.refresh_frame_list(preserve_scene=True)

    def _set_all_classes(self, checked: bool) -> None:
        self.class_filter.blockSignals(True)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.class_filter.count()):
            self.class_filter.item(row).setCheckState(state)
        self.class_filter.blockSignals(False)
        self.refresh_frame_list()

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

    def refresh_frame_list(
        self, *_args, show_progress: bool = False, preserve_scene: bool = False,
    ) -> None:
        if not hasattr(self, "frame_list") or self._refreshing:
            return
        self._commit_current_scene()
        self._refreshing = True
        previous_name = None
        previous_id = self.current_image_id
        if self.current_image_id in self.image_by_id:
            previous_name = self.image_by_id[self.current_image_id]["file_name"]
        self.frame_list.blockSignals(True)
        self.frame_list.clear()
        filtered = []
        selected_ids = self._selected_category_ids()
        include_background = self.BACKGROUND_FILTER_ID in selected_ids
        selected_categories = selected_ids - {self.BACKGROUND_FILTER_ID}
        verification = self.verification_filter.currentData()
        frame_verification = self.frame_verification_filter.currentData()
        no_filter = (
            len(selected_categories) == len(self.categories)
            and include_background
            and verification == "all"
        )
        progress = None
        if show_progress and len(self.images) >= 100:
            progress = QProgressDialog("Building image list…", "", 0, len(self.images), self)
            progress.setWindowTitle("Loading images")
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(0)
            progress.show()
            progress.raise_()
            QApplication.processEvents()
        ordered_images = sorted(
            self.images,
            key=lambda image: str(
                self.source_paths_by_image.get(
                    image["id"], (self.root or Path()) / image["file_name"]
                )
            ).casefold(),
        )
        for index, image in enumerate(ordered_images, start=1):
            if self.visible_image_ids is not None and image["id"] not in self.visible_image_ids:
                continue
            annotations = self.annotations_by_image[image["id"]]
            frame_verified = self.frame_verified_by_image.get(image["id"], False)
            matches_frame_verification = (
                frame_verification == "all"
                or (frame_verification == "verified" and frame_verified)
                or (frame_verification == "unverified" and not frame_verified)
            )
            image_name = Path(image["file_name"]).name
            matches_search = self.frame_search.text().strip().casefold() in image_name.casefold()
            matches_class_filter = no_filter or (
                include_background
                and self.background_by_image.get(image["id"], False)
            ) or any(
                self._annotation_matches_filter(annotation, selected_categories, verification)
                for annotation in annotations
            )
            annotator_filter = self.annotator_filter.currentData()
            if annotator_filter == self.BACKGROUND_ANNOTATOR_FILTER_ID:
                matches_annotator = (
                    self.background_by_image.get(image["id"], False) and not annotations
                )
            elif annotator_filter == self.ALL_ANNOTATORS_FILTER_ID:
                matches_annotator = True
            else:
                annotator_ids = {
                    annotator.get("name"): annotator.get("id")
                    for annotator in self.annotation_data.get("annotators", [])
                    if isinstance(annotator, dict)
                }
                annotator_id = annotator_ids.get(annotator_filter)
                matches_annotator = any(
                    annotation.get("annotator_id") == annotator_id
                    or annotation.get("Annotator") == annotator_filter
                    for annotation in annotations
                )
            if (
                matches_search and matches_frame_verification
                and matches_class_filter and matches_annotator
            ):
                filtered.append(image)
                item = QListWidgetItem(image_name)
                item.setData(Qt.ItemDataRole.UserRole, image["id"])
                self.frame_list.addItem(item)
            if progress and (index % 100 == 0 or index == len(self.images)):
                progress.setValue(index)
                progress.setLabelText(f"Building image list…  {index:,} / {len(self.images):,}")
                QApplication.processEvents()
        if progress:
            progress.close()
        target_item = None
        for row in range(self.frame_list.count()):
            item = self.frame_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == previous_id:
                target_item = item
                break
        if target_item is None:
            for row in range(self.frame_list.count()):
                item = self.frame_list.item(row)
                if self.image_by_id[item.data(Qt.ItemDataRole.UserRole)]["file_name"] == previous_name:
                    target_item = item
                    break
        if target_item is None and self.frame_list.count() and not preserve_scene:
            target_item = self.frame_list.item(0)
        if target_item is not None:
            self.frame_list.setCurrentItem(target_item)
        self.frame_list.blockSignals(False)
        self._refreshing = False
        self.previous_button.setEnabled(self.frame_list.count() > 1)
        self.next_button.setEnabled(self.frame_list.count() > 1)
        if target_item is not None and not preserve_scene:
            self._load_frame(target_item.data(Qt.ItemDataRole.UserRole))
        elif target_item is None and not preserve_scene:
            self._clear_scene()
        self._update_frame_stats()

    def _update_frame_stats(self) -> None:
        shown_ids = {
            self.frame_list.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(self.frame_list.count())
        }
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
            + f"Images: {self.frame_list.count():,} filtered / {len(self.images):,}\n"
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
        count = self.frame_list.count()
        if count < 2:
            return
        row = self.frame_list.currentRow()
        self.frame_list.setCurrentRow((row + step) % count)

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
        if self.base_image is None:
            return
        self._auto_level_generation += 1
        generation = self._auto_level_generation
        worker = AutoLevelsWorker(generation, self.base_image)
        worker.signals.levels_ready.connect(self._auto_levels_finished)
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
        self.auto_levels_button.setEnabled(self.base_image is not None)
        self.status_label.setText(f"Auto levels set for this image: black {black}, white {white}.")

    def _update_display_image(self) -> None:
        if self.base_image is None or self.background_item is None:
            return
        if self._adjust_cancel is not None:
            self._adjust_cancel.set()
        self._adjust_generation += 1
        generation = self._adjust_generation
        cancelled = threading.Event()
        self._adjust_cancel = cancelled
        worker = ImageAdjustmentWorker(
            generation,
            self.base_image,
            self.brightness_slider.value() / 100,
            self.contrast_slider.value() / 100,
            self.gamma_slider.value() / 100,
            self.invert_check.isChecked(),
            self.black_point.value(),
            self.white_point.value(),
            cancelled,
        )
        worker.signals.finished.connect(self._adjustment_finished)
        self._adjust_workers[generation] = worker
        self.adjustment_pool.start(worker)

    def _apply_display_adjustments(self, image: Image.Image) -> Image.Image:
        return apply_image_adjustments(
            image,
            self.brightness_slider.value() / 100,
            self.contrast_slider.value() / 100,
            self.gamma_slider.value() / 100,
            self.invert_check.isChecked(),
            self.black_point.value(),
            self.white_point.value(),
        )

    def _adjustment_finished(self, generation: int, image: QImage | None, scale: float) -> None:
        self._adjust_workers.pop(generation, None)
        if generation != self._adjust_generation or image is None or self.background_item is None:
            return
        self.background_item.setPixmap(QPixmap.fromImage(image))
        self.background_item.setScale(scale)

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
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", font_size)
        except OSError:
            font = ImageFont.load_default()
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

    def save_project(self) -> bool:
        if self.root is None:
            QMessageBox.information(self, "No project", "Create or open a project folder first.")
            return False
        if not self.dirty and self.annotations_path is not None:
            return True
        self._commit_current_scene()
        export_images = [
            image for image in self.images
            if self._frame_is_exportable(image["id"])
            or self._frame_has_work(image["id"])
            or image["id"] in self.persisted_image_ids
            or image["id"] in self.touched_image_ids
        ]
        if not export_images:
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

        if self._project_image_names is None:
            self._project_image_names = directory_names(self.root)
        existing_names = self._project_image_names
        exported_images = []
        exported_ids = set()
        conflicts: list[tuple[Path, Path]] = []
        missing: list[Path] = []
        for image in export_images:
            image_id = image["id"]
            source = self._source_image_path(image)
            if source is None:
                missing.append(Path(image["file_name"]))
                continue
            target_name = Path(image["file_name"]).name
            target = self.root / target_name
            same_path = path_key(source) == path_key(target)
            if target_name.casefold() in existing_names and not same_path:
                conflicts.append((source, target))
                continue
            if same_path and target_name.casefold() not in existing_names:
                missing.append(source)
                continue
            try:
                if not same_path:
                    shutil.copy2(source, target)
            except OSError as error:
                QMessageBox.critical(self, "Cannot package image", str(error))
                return False

            image["file_name"] = target.name
            image["frame_verified"] = self.frame_verified_by_image.get(image_id, False)
            image["background"] = self.background_by_image.get(image_id, False)
            self.source_paths_by_image[image_id] = target
            existing_names.add(target.name.casefold())
            exported_images.append(dict(image))
            exported_ids.add(image_id)

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

        packaged_annotations = [
            annotation for annotation in export_annotations
            if annotation["image_id"] in exported_ids
        ]
        project_data = dict(self.annotation_data)
        project_data["images"] = exported_images
        project_data["annotations"] = packaged_annotations
        save_path = self.root.parent / "annotations.json"
        temporary_path = save_path.with_name(save_path.name + ".tmp")
        try:
            temporary_path.write_text(json.dumps(project_data, indent=2) + "\n", encoding="utf-8")
            temporary_path.replace(save_path)
        except OSError as error:
            QMessageBox.critical(self, "Save project failed", str(error))
            return False
        self.output_path = save_path
        self.annotations_path = save_path
        self.persisted_image_ids = exported_ids
        self.touched_image_ids.clear()
        self.dirty = False
        self.status_label.setText(
            f"Saved {len(exported_images):,} image(s) and {len(packaged_annotations):,} annotation boxes "
            f"to {save_path}"
        )
        self.refresh_frame_list(preserve_scene=True)
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
        if "annotations.json" in directory_names(destination.parent):
            QMessageBox.warning(
                self, "Destination already contains annotations.json",
                "Choose an empty output folder so the training export does not replace another dataset.",
            )
            return

        existing_names = directory_names(destination)
        exported_images = []
        conflicts = []
        missing = []
        for image in training_images:
            source = self._source_image_path(image)
            if source is None:
                missing.append(Path(image["file_name"]))
                continue
            name = Path(image["file_name"]).name
            target = destination / name
            same_path = path_key(source) == path_key(target)
            if name.casefold() in existing_names and not same_path:
                conflicts.append((source, target))
                continue
            if same_path and name.casefold() not in existing_names:
                missing.append(source)
                continue
            try:
                if not same_path:
                    shutil.copy2(source, target)
            except OSError as error:
                QMessageBox.critical(self, "Training export failed", str(error))
                return
            record = dict(image)
            record["file_name"] = (Path(destination.name) / name).as_posix()
            record["frame_verified"] = True
            record["background"] = self.background_by_image.get(image["id"], False)
            exported_images.append(record)
            existing_names.add(name.casefold())

        if conflicts:
            self._show_duplicate_summary(conflicts, 0)
            return
        if missing:
            QMessageBox.warning(
                self, "Training images not found",
                "Could not package these image paths:\n\n" + "\n".join(str(path) for path in missing[:20]),
            )
            return

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
        save_path = destination.parent / "annotations.json"
        temporary_path = save_path.with_name(save_path.name + ".tmp")
        try:
            temporary_path.write_text(
                json.dumps(export_data, indent=2) + "\n", encoding="utf-8"
            )
            temporary_path.replace(save_path)
        except OSError as error:
            QMessageBox.critical(self, "Training export failed", str(error))
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

    def _load_frame(self, image_id: Any) -> None:
        if image_id == self.current_image_id:
            return
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(False)
        if self.view.calibration_mode:
            self.calibrate_scale_button.setChecked(False)
        self._commit_current_scene()
        self.view.clear_drawing_guides()
        self._cancel_image_adjustment()
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.current_items = []
        self.scale_bar_item = None
        self.background_item = None
        self.base_image = None
        image = self.image_by_id[image_id]
        path = self.source_paths_by_image.get(image_id)
        if path is None:
            try:
                if self.root is None:
                    raise ValueError("Choose a project folder first")
                path = dataset_image_path(self.root, image["file_name"])
            except ValueError as error:
                QMessageBox.critical(self, "Invalid image path", str(error))
                self.current_image_id = None
                return
        try:
            with Image.open(path) as source_image:
                self.base_image = source_image.convert("RGB")
        except OSError:
            self.base_image = None
        if self.base_image is None:
            QMessageBox.critical(self, "Cannot load image", f"Could not open image:\n{path}")
            self.current_image_id = None
            return
        self.auto_levels_button.setEnabled(True)
        image["width"] = self.base_image.width
        image["height"] = self.base_image.height
        self.background_item = QGraphicsPixmapItem()
        self.background_item.setZValue(-10)
        self.background_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.scene.addItem(self.background_item)
        self.scene.setSceneRect(QRectF(0, 0, self.base_image.width, self.base_image.height))
        self.scale_bar_item = ScaleBarItem(self.base_image.width, self.base_image.height)
        self.scene.addItem(self.scale_bar_item)
        self._update_scale_bar()
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
        self._auto_level_generation += 1
        self.auto_levels_button.setEnabled(False)
        self.view.clear_drawing_guides()
        self._cancel_image_adjustment()
        self.scene.clear()
        self.scene.setSceneRect(QRectF())
        self.current_items = []
        self.base_image = None
        self.background_item = None
        self.scale_bar_item = None
        self.current_image_id = None
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

    def _mark_dirty(self, *, counts_changed: bool = False) -> None:
        self.dirty = True
        if self.current_image_id is not None:
            self.touched_image_ids.add(self.current_image_id)
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
        self._refresh_annotator_filter()
        self.refresh_frame_list(preserve_scene=True)

    def _sync_frame_controls(self) -> None:
        image_id = self.current_image_id
        active = image_id in self.image_by_id
        has_rois = bool(self.current_items) if active else False
        self.frame_verified_check.blockSignals(True)
        self.frame_verified_check.setChecked(
            self.frame_verified_by_image.get(image_id, False) if active else False
        )
        self.frame_verified_check.setEnabled(active)
        self.frame_verified_check.blockSignals(False)
        self.background_check.blockSignals(True)
        self.background_check.setChecked(
            self.background_by_image.get(image_id, False) if active else False
        )
        self.background_check.setVisible(True)
        self.background_check.setEnabled(active and not has_rois)
        self.background_check.blockSignals(False)
        can_draw = active and bool(self.categories) and not self.background_by_image.get(image_id, False)
        self.draw_button.setEnabled(can_draw)
        if not can_draw and self.draw_button.isChecked():
            self.draw_button.setChecked(False)

    def _frame_verified_changed(self, checked: bool) -> None:
        image_id = self.current_image_id
        if image_id not in self.image_by_id:
            return
        if self.frame_verified_by_image.get(image_id, False) == checked:
            return
        self.frame_verified_by_image[image_id] = checked
        self.touched_image_ids.add(image_id)
        self.dirty = True
        self.status_label.setText("Image verification changed — unsaved changes")
        self.refresh_frame_list(preserve_scene=True)

    def _background_changed(self, checked: bool) -> None:
        image_id = self.current_image_id
        if image_id not in self.image_by_id or self.current_items:
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
        self.refresh_frame_list(preserve_scene=True)

    def _set_draw_mode(self, enabled: bool) -> None:
        if enabled and (self.current_image_id is None
                        or self.background_by_image.get(self.current_image_id, False)):
            self.draw_button.setChecked(False)
            return
        self.view.set_draw_mode(enabled)
        self.draw_button.setText("Cancel drawing" if enabled else "Draw box")

    def _add_rectangle(self, rect: QRectF) -> None:
        if self.current_image_id is None:
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
            self, "Choose label", "Select an existing label:",
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
            "Annotator": self.current_annotator,
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
        enabled = item is not None
        self.edit_class.setEnabled(enabled)
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
            label = f"{item.category_name} · {status} · #{item.annotation.get('id', '')}"
            list_item = QListWidgetItem(label)
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
        name, accepted = QInputDialog.getText(self, "Add label", "New label name:")
        name = name.strip()
        if not accepted or not name:
            return
        if any(existing.casefold() == name.casefold() for existing in self.categories.values()):
            QMessageBox.information(self, "Label already exists", f"The label {name!r} is already available.")
            return
        self._create_class(name)
        self._mark_dirty()
        self.refresh_frame_list()

    def _import_classes(self) -> None:
        if self.root is None:
            return
        start_path = self.annotations_path or self.root
        json_path, _ = QFileDialog.getOpenFileName(
            self, "Import labels from annotation JSON", str(start_path), "JSON files (*.json)"
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
                    raise ValueError("Every label must have a name.")
                name = category["name"].strip()
                if not name:
                    raise ValueError("Label names cannot be empty.")
                names.append(name)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            QMessageBox.critical(self, "Cannot import labels", str(error))
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
            self._mark_dirty()
            self.refresh_frame_list()
            QMessageBox.information(
                self, "Labels imported", f"Imported {len(added):,} label(s):\n" + ", ".join(added)
            )
        else:
            QMessageBox.information(self, "Labels imported", "All labels already exist in this project.")

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

    def _rename_class(self) -> None:
        names = sorted(self.categories.values(), key=str.casefold)
        if not names:
            return
        old_name, accepted = QInputDialog.getItem(
            self, "Rename label", "Select the label to rename:", names, 0, False
        )
        if not accepted or not old_name:
            return
        category_id = next(
            key for key, existing in self.categories.items() if existing == old_name
        )
        name, accepted = QInputDialog.getText(
            self, "Rename label", "Label name:", text=old_name
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
                "Merge labels?",
                f"The label {name!r} already exists. Merge {old_name!r} into {name!r}?\n\n"
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
        self._mark_dirty()

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
        self.annotation_data["annotations"] = [
            annotation for image in self.images
            for annotation in self.annotations_by_image[image["id"]]
        ]
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
        self._mark_dirty(counts_changed=True)
        self._refresh_annotator_filter()
        self.refresh_frame_list(preserve_scene=True)
        self.status_label.setText(
            f"Merged label {source_name!r} into {target_name!r} — unsaved changes"
        )

    def _choose_class_color(self) -> None:
        names = sorted(self.categories.values(), key=str.casefold)
        if not names:
            return
        name, accepted = QInputDialog.getItem(
            self, "Label color", "Select a label:", names, 0, False
        )
        if not accepted or not name:
            return
        category_id = next(
            key for key, existing in self.categories.items() if existing == name
        )
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




