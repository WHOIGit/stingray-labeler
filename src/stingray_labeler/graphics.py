"""Qt graphics items and image view controls."""

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
        QFileDialog,
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
        QSplitter,
        QVBoxLayout,
        QWidget,
        QWidgetAction,
    )
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont
except ImportError as error:
    raise SystemExit("Install the GUI dependencies with: python -m pip install PySide6 Pillow") from error

class BoxItem(QGraphicsRectItem):
    """Selectable, movable, and corner/edge-resizable COCO rectangle."""

    HANDLE_RADIUS = 5.0
    MIN_SIZE = 2.0
    selection_color = QColor(255, 255, 0)
    class_colors: dict[Any, QColor] = {}

    def __init__(self, annotation: dict[str, Any], category_name: str, changed, before_change=None):
        x, y, width, height = annotation["bbox"]
        super().__init__(QRectF(float(x), float(y), float(width), float(height)))
        self.annotation = annotation
        self.category_name = category_name
        self.changed = changed
        self.before_change = before_change
        self._resize_handle: str | None = None
        self._undo_started = False
        self._geometry_changed = False
        self._resize_start = QPointF()
        self._resize_rect = QRectF()
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(5)

    def color(self) -> QColor:
        category_id = self.annotation["category_id"]
        return self.class_colors.get(
            category_id,
            QColor.fromHsv((int(category_id) * 137) % 360, 210, 235),
        )

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self.rect().adjusted(
            -self.HANDLE_RADIUS, -self.HANDLE_RADIUS,
            self.HANDLE_RADIUS, self.HANDLE_RADIUS,
        )

    def paint(self, painter: QPainter, option, widget=None) -> None:
        color = self.color()
        outline = QPen(self.selection_color if self.isSelected() else color, 3.0)
        if self.isSelected():
            outline.setStyle(Qt.PenStyle.DashLine)
        outline.setCosmetic(True)
        painter.setPen(outline)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect())
        if self.isSelected():
            handle_pen = QPen(color, 1.0)
            handle_pen.setCosmetic(True)
            painter.setPen(handle_pen)
            painter.setBrush(QBrush(color))
            for point in self._handles().values():
                radius = self.HANDLE_RADIUS
                painter.drawEllipse(QRectF(point.x() - radius, point.y() - radius,
                                           radius * 2, radius * 2))

    def _handles(self) -> dict[str, QPointF]:
        rect = self.rect()
        return {"nw": rect.topLeft(), "se": rect.bottomRight()}

    def _handle_at(self, point: QPointF) -> str | None:
        for name, handle in self._handles().items():
            if ((point - handle).manhattanLength() <= self.HANDLE_RADIUS * 2):
                return name
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        handle = self._handle_at(event.pos()) if self.isSelected() else None
        if handle and event.button() == Qt.MouseButton.LeftButton:
            self._undo_started = False
            self._geometry_changed = False
            self._resize_handle = handle
            self._resize_start = event.pos()
            self._resize_rect = self.rect()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._undo_started = False
            self._geometry_changed = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._resize_handle:
            if event.buttons() & Qt.MouseButton.LeftButton and not self._undo_started:
                if self.before_change:
                    self.before_change()
                self._undo_started = True
            super().mouseMoveEvent(event)
            return
        if not self._undo_started:
            if self.before_change:
                self.before_change()
            self._undo_started = True
        delta = event.pos() - self._resize_start
        rect = QRectF(self._resize_rect)
        if "w" in self._resize_handle:
            rect.setLeft(min(rect.left() + delta.x(), rect.right() - self.MIN_SIZE))
        if "e" in self._resize_handle:
            rect.setRight(max(rect.right() + delta.x(), rect.left() + self.MIN_SIZE))
        if "n" in self._resize_handle:
            rect.setTop(min(rect.top() + delta.y(), rect.bottom() - self.MIN_SIZE))
        if "s" in self._resize_handle:
            rect.setBottom(max(rect.bottom() + delta.y(), rect.top() + self.MIN_SIZE))
        if rect != self.rect():
            self.setRect(rect)
            self._geometry_changed = True
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._resize_handle:
            self._resize_handle = None
            self._undo_started = False
            if self._geometry_changed:
                self.changed()
            self._geometry_changed = False
            event.accept()
            return
        self._undo_started = False
        super().mouseReleaseEvent(event)
        if self._geometry_changed:
            self.changed()
        self._geometry_changed = False

    def itemChange(self, change, value):  # noqa: N802
        result = super().itemChange(change, value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._geometry_changed = True
        return result

    def scene_bbox(self) -> list[float]:
        rect = self.mapRectToScene(self.rect()).normalized()
        return [rect.left(), rect.top(), rect.width(), rect.height()]


class ScaleBarItem(QGraphicsItem):
    """Draw a scale bar over the image in source-pixel coordinates."""

    def __init__(self, width: int, height: int, parent=None):
        super().__init__(parent)
        self.width = width
        self.height = height
        self.visible = True
        self.pixel_resolution = 40.0
        self.length_mm = 10.0
        self.setZValue(2)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(0, 0, self.width, self.height)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        bar_width = self.length_mm * 1000 / self.pixel_resolution
        margin = max(16, self.width / 80)
        if not self.visible or bar_width <= 0 or bar_width > self.width - 2 * margin:
            return
        bar_height = max(5, self.width / 450)
        left = self.width - margin - bar_width
        top = self.height - margin - bar_height
        label = format_length(self.length_mm)
        font = QFont()
        font.setPixelSize(max(12, min(40, round(self.width / 50))))
        painter.save()
        painter.setFont(font)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(Qt.GlobalColor.black))
        painter.drawRect(QRectF(left, top, bar_width, bar_height))
        metrics = painter.fontMetrics()
        label_rect = metrics.boundingRect(label).adjusted(-3, -2, 3, 2)
        label_rect.moveBottomLeft(QPoint(round(left), round(top - 3)))
        painter.setPen(QPen(Qt.GlobalColor.black, 1))
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        painter.restore()


class ImageRuler(QWidget):
    """Fixed display ruler whose tick spacing follows zoom and pixel resolution."""

    def __init__(self, view: "AnnotationView", orientation: Qt.Orientation,
                 resolution: float, parent=None):
        super().__init__(parent)
        self.view = view
        self.orientation = orientation
        self.resolution = resolution
        self.setMinimumSize(1, 1)

    def sizeHint(self):
        return QSize(1, 22) if self.orientation == Qt.Orientation.Horizontal else QSize(36, 1)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        palette = self.palette()
        background = palette.color(QPalette.ColorRole.Window)
        foreground = palette.color(QPalette.ColorRole.WindowText)
        background = background.darker(112)
        painter.fillRect(self.rect(), background)
        painter.setPen(QPen(foreground, 1))
        scene_rect = self.view.scene().sceneRect()
        if scene_rect.isEmpty():
            return
        viewport_origin = self.view.viewport().mapTo(self.parentWidget(), QPoint(0, 0))
        ruler_origin = self.mapFrom(self.parentWidget(), viewport_origin)
        if self.orientation == Qt.Orientation.Horizontal:
            edge = self.height() - 1
            painter.drawLine(0, edge, self.width(), edge)
            origin = ruler_origin.x()
            available = self.width() - origin
            zoom = abs(self.view.transform().m11())
        else:
            edge = self.width() - 1
            painter.drawLine(edge, 0, edge, self.height())
            origin = ruler_origin.y()
            available = self.height() - origin
            zoom = abs(self.view.transform().m22())
        tick_px = 1000.0 / max(self.resolution, 1e-12)
        if available <= 0 or tick_px <= 0 or zoom <= 0:
            return
        px_per_tick = tick_px * abs(zoom)
        if px_per_tick <= 0:
            return
        last = math.floor(available / px_per_tick)
        show_minor = px_per_tick >= 4
        major_spacing = px_per_tick * 10
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        if not show_minor:
            major_step = max(1, math.ceil(4 / max(major_spacing, 1e-12)))
            last_major = math.floor(last / 10)
            ticks = (centimetres * 10 for centimetres in range(0, last_major + 1, major_step))
        else:
            ticks = iter(range(last + 1))
        for tick in ticks:
            major = tick % 10 == 0
            position = round(origin + tick * px_per_tick)
            tick_length = 9 if major else 4
            if self.orientation == Qt.Orientation.Horizontal:
                painter.drawLine(position, edge, position, edge - tick_length)
            else:
                painter.drawLine(edge, position, edge - tick_length, position)
            if major:
                label = f"{tick / 10:g}"
                if self.orientation == Qt.Orientation.Horizontal:
                    painter.drawText(position + 2, 11, label)
                else:
                    painter.drawText(2, position - 2, label)

        if self.orientation == Qt.Orientation.Horizontal:
            painter.drawText(self.width() - 22, 11, "cm")
        else:
            painter.drawText(2, self.height() - 2, "cm")


def format_length(length_mm: float) -> str:
    """Format scale lengths using the viewer's mm/cm/µm conventions."""
    if length_mm >= 10:
        return f"{length_mm / 10:g} cm"
    if length_mm < 1:
        return f"{length_mm * 1000:g} µm"
    return f"{length_mm:g} mm"


class AnnotationView(QGraphicsView):
    """Image view with rectangle editing, panning, and cursor-centered zoom."""

    rectangleCreated = Signal(QRectF)

    def __init__(self, scene: QGraphicsScene, parent=None):
        super().__init__(scene, parent)
        self.draw_mode = False
        self._space_pressed = False
        self._pan_last: QPoint | None = None
        self._draw_start: QPointF | None = None
        self._rubber_band: QGraphicsRectItem | None = None
        self._crosshair_pos: QPointF | None = None
        self.draw_color = QColor(255, 255, 0)
        self._manual_zoom = False
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setBackgroundBrush(QBrush(QColor(36, 36, 36)))
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    viewChanged = Signal()

    def set_draw_mode(self, enabled: bool) -> None:
        self.draw_mode = enabled
        if not enabled:
            self.clear_drawing_guides()

    def clear_drawing_guides(self) -> None:
        if self._rubber_band is not None and self._rubber_band.scene() is self.scene():
            self.scene().removeItem(self._rubber_band)
        self._rubber_band = None
        self._draw_start = None
        previous_crosshair = self._crosshair_pos
        self._crosshair_pos = None
        if previous_crosshair is not None:
            self.updateScene(self._crosshair_damage_rects(previous_crosshair))

    def _update_crosshair(self, point: QPointF) -> None:
        previous = self._crosshair_pos
        self._crosshair_pos = point
        damage = []
        if previous is not None:
            damage.extend(self._crosshair_damage_rects(previous))
        damage.extend(self._crosshair_damage_rects(point))
        self.updateScene(damage)

    def _crosshair_damage_rects(self, point: QPointF) -> list[QRectF]:
        scene_rect = self.scene().sceneRect()
        if scene_rect.isEmpty():
            return []
        zoom_x = max(abs(self.transform().m11()), 1e-12)
        zoom_y = max(abs(self.transform().m22()), 1e-12)
        pad_x = 2.0 / zoom_x
        pad_y = 2.0 / zoom_y
        return [
            QRectF(scene_rect.left(), point.y() - pad_y,
                   scene_rect.width(), 2 * pad_y),
            QRectF(point.x() - pad_x, scene_rect.top(),
                   2 * pad_x, scene_rect.height()),
        ]

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        if not self.draw_mode or self._crosshair_pos is None:
            return
        pen = QPen(QColor(0, 0, 0), 1.5)
        pen.setCosmetic(True)
        painter.save()
        painter.setPen(pen)
        point = self._crosshair_pos
        painter.drawLine(QPointF(rect.left(), point.y()), QPointF(rect.right(), point.y()))
        painter.drawLine(QPointF(point.x(), rect.top()), QPointF(point.x(), rect.bottom()))
        painter.restore()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pressed = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_pressed = False
            if self._pan_last is None:
                self.unsetCursor()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton or (
            self._space_pressed and event.button() == Qt.MouseButton.LeftButton
        ):
            self._pan_last = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        item = self.itemAt(event.position().toPoint())
        if (
            not self.draw_mode
            and event.button() == Qt.MouseButton.LeftButton
            and not isinstance(item, BoxItem)
            and (self.horizontalScrollBar().maximum() > 0 or self.verticalScrollBar().maximum() > 0)
        ):
            self._pan_last = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if self.draw_mode and event.button() == Qt.MouseButton.LeftButton:
            self._draw_start = self.mapToScene(event.position().toPoint())
            self._rubber_band = self.scene().addRect(
                QRectF(self._draw_start, self._draw_start),
                QPen(self.draw_color, 2.0, Qt.PenStyle.DashLine),
            )
            self._rubber_band.setZValue(20)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._pan_last is not None:
            current = event.position().toPoint()
            delta = current - self._pan_last
            self._pan_last = current
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        current_scene = self.mapToScene(event.position().toPoint())
        if self.draw_mode:
            self._update_crosshair(current_scene)
        if self._draw_start is not None and self._rubber_band is not None:
            self._rubber_band.setRect(QRectF(self._draw_start, current_scene).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._pan_last is not None and event.button() in {
            Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton,
        }:
            self._pan_last = None
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._space_pressed else Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        if self._draw_start is not None and self._rubber_band is not None:
            rect = self._rubber_band.rect().normalized()
            self.scene().removeItem(self._rubber_band)
            self._rubber_band = None
            self._draw_start = None
            if rect.width() >= 2 and rect.height() >= 2:
                self.rectangleCreated.emit(rect)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self.draw_mode:
            self.clear_drawing_guides()
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        self._manual_zoom = True
        self.viewChanged.emit()
        event.accept()

    def fit_image(self) -> None:
        if not self._manual_zoom and not self.scene().sceneRect().isEmpty():
            self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.fit_image()
        self.viewChanged.emit()




