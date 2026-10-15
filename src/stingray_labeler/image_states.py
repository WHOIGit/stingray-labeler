"""Image states: Unverified, Review, Verified and the Background mark.

A mixin of ImageAnnotator (window.py): its methods use the window's state through self.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QMessageBox

from .dataset import is_verified
from .merge import image_key


class ImageStatesMixin:
    """Image states and the Background mark, and the rules that tie Verified to its boxes."""

    def _frame_state(self, image_id: Any) -> str:
        if self.frame_verified_by_image.get(image_id, False):
            return "verified"
        return "review" if self.review_by_image.get(image_id, False) else "unverified"

    def _has_work_or_verified(self, image_id: Any) -> bool:
        return self._frame_has_work(image_id) or self._frame_state(image_id) != "unverified"

    def _frame_is_exportable(self, image_id: Any) -> bool:
        return self.frame_verified_by_image.get(image_id, False)

    def _frame_has_work(self, image_id: Any) -> bool:
        return bool(
            self.annotations_by_image.get(image_id)
            or self.background_by_image.get(image_id, False)
            or self.image_by_id.get(image_id, {}).get("favorite")
        )

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
