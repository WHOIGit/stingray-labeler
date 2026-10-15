"""Keyboard shortcuts: the single-key table, its dialog and key handling.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

import json

from PySide6.QtCore import QEvent, QKeyCombination, QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QDialog, QDialogButtonBox, QKeySequenceEdit, QLabel, QLineEdit,
    QMessageBox, QSlider, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .dataset import path_key

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


class ShortcutsMixin:
    """Single-key shortcuts, kept on this PC, and the key handling of the window."""

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
