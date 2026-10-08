"""Three-way merge of project annotation files.

Two copies of a project that started from the same original ("base") are merged
image by image and box by box:

* a change made on one side only is taken;
* a box deleted on one side and untouched on the other is deleted;
* a box deleted on one side and edited on the other is kept;
* boxes from the two sides on the same image that overlap at IoU >= threshold are
  duplicates when their category matches (one is kept: the verified box, then the
  box on the side whose image is verified, then the merging user's), and a class
  disagreement when it differs (both are kept and the image is set to review);
* an image state changed differently on the two sides becomes review.

Images are matched by file name, boxes by annotation id, categories and users by
name. Unverified images are not part of a saved project, so they and their boxes
count as absent. Nothing here touches the file system or the UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .dataset import is_verified

FRAME_STATES = ("verified", "review", "unverified")
MERGE_IOU_THRESHOLD = 0.80
# Fields compared or rebuilt by the merge; everything else on a box is carried along unchanged.
_BOX_FIELDS = {"id", "image_id", "category_id", "bbox", "area", "verified", "annotator_id", "Annotator"}


@dataclass(frozen=True)
class Box:
    image: str  # casefolded image name
    category: str
    bbox: tuple[float, float, float, float]
    verified: bool
    user: str | None
    extra: str  # remaining fields as sorted JSON, so boxes compare by value

    def content(self) -> tuple:
        """Everything except the image, for comparing an image's boxes."""
        return self.category, self.bbox, self.verified, self.user, self.extra


@dataclass
class Side:
    images: dict[str, dict[str, Any]]  # name key -> {"name", "state", "background", "record"}
    boxes: dict[Any, Box]


@dataclass
class MergeResult:
    images: dict[str, dict[str, Any]]
    boxes: list[dict[str, Any]]  # {"id", "box": Box}
    changed: set[str]  # image keys whose result differs from the merging user's copy
    summary: dict[str, list[str]] = field(default_factory=dict)  # kind -> image names


def image_key(file_name: str) -> str:
    return str(file_name).replace("\\", "/").rsplit("/", 1)[-1].casefold()


def legacy_state(image: dict[str, Any], missing_state: str) -> str:
    state = image.get("frame_state")
    if state in FRAME_STATES:
        return state
    return "verified" if is_verified(image.get("frame_verified", False)) else missing_state


def normalize(data: dict[str, Any], missing_state: str = "review") -> Side:
    """Index a project file by image name and box id; unverified images and their boxes are left out.

    ``missing_state`` is the state of images saved before frame_state existed and not verified;
    the app used to save those because they had work, so "review" is their meaning by default.
    """
    images: dict[str, dict[str, Any]] = {}
    key_by_id: dict[Any, str] = {}
    for image in data.get("images", []):
        if not isinstance(image, dict) or "file_name" not in image:
            continue
        state = legacy_state(image, missing_state)
        if state == "unverified":
            continue
        key = image_key(image["file_name"])
        images[key] = {
            "name": str(image["file_name"]).replace("\\", "/").rsplit("/", 1)[-1],
            "state": state,
            "background": is_verified(image.get("background", False)),
            "record": image,
        }
        key_by_id[image.get("id")] = key
    categories = {
        category["id"]: str(category["name"]) for category in data.get("categories", [])
        if isinstance(category, dict) and "id" in category and "name" in category
    }
    users = {
        user["id"]: str(user["name"]) for user in data.get("annotators", []) or []
        if isinstance(user, dict) and "id" in user and "name" in user
    }
    boxes: dict[Any, Box] = {}
    for index, annotation in enumerate(data.get("annotations", [])):
        if not isinstance(annotation, dict):
            continue
        key = key_by_id.get(annotation.get("image_id"))
        if key is None:
            continue
        legacy_user = annotation.get("Annotator")
        user = legacy_user.strip() if isinstance(legacy_user, str) and legacy_user.strip() else users.get(
            annotation.get("annotator_id")
        )
        category_id = annotation.get("category_id")
        extra = {name: value for name, value in annotation.items() if name not in _BOX_FIELDS}
        box_id = annotation.get("id", ("no id", index))
        boxes[box_id] = Box(
            key,
            categories.get(category_id, f"category {category_id}"),
            tuple(round(float(value), 6) for value in annotation["bbox"]),
            is_verified(annotation.get("verified", True)),
            user,
            json.dumps(extra, sort_keys=True, default=str),
        )
    return Side(images, boxes)


def box_iou(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    x1, y1, w1, h1 = first
    x2, y2, w2, h2 = second
    overlap_w = min(x1 + w1, x2 + w2) - max(x1, x2)
    overlap_h = min(y1 + h1, y2 + h2) - max(y1, y2)
    if overlap_w <= 0 or overlap_h <= 0:
        return 0.0
    intersection = overlap_w * overlap_h
    union = w1 * h1 + w2 * h2 - intersection
    return intersection / union if union > 0 else 0.0


def _three_way(base: Any, mine: Any, theirs: Any) -> tuple[Any, bool]:
    """(merged value, conflict). A value changed on one side wins; both changed differently is a conflict."""
    if mine == theirs or theirs == base:
        return mine, False
    if mine == base:
        return theirs, False
    return mine, True


def merge(base: Side, mine: Side, theirs: Side, threshold: float = MERGE_IOU_THRESHOLD) -> MergeResult:
    summary: dict[str, set[str]] = {
        "from_theirs": set(), "kept_over_delete": set(), "duplicates": set(),
        "class_disagreements": set(), "state_conflicts": set(), "added": set(), "removed": set(),
    }

    # Boxes: (origin, id, box); origin "both" is unchanged, "mine" or "theirs" is that side's version.
    kept: list[tuple[str, Any, Box]] = []
    for box_id in base.boxes.keys() | mine.boxes.keys() | theirs.boxes.keys():
        b, m, t = base.boxes.get(box_id), mine.boxes.get(box_id), theirs.boxes.get(box_id)
        if b is None:  # new on one or both sides; the same new id on both sides is two different boxes
            if m is not None:
                kept.append(("mine", box_id, m))
            if t is not None:
                kept.append(("theirs", box_id, t))
                summary["from_theirs"].add(t.image)
            continue
        mine_changed, theirs_changed = m != b, t != b
        if not mine_changed and not theirs_changed:
            kept.append(("both", box_id, b))
        elif not theirs_changed:
            if m is not None:
                kept.append(("mine", box_id, m))
        elif not mine_changed:
            summary["from_theirs"].add(b.image)
            if t is not None:
                kept.append(("theirs", box_id, t))
        elif m is None and t is None:
            pass  # deleted on both sides
        elif m is None or t is None:  # deleted on one side, edited on the other: keep the edit
            survivor = t if m is None else m
            kept.append(("theirs" if m is None else "mine", box_id, survivor))
            summary["kept_over_delete"].add(survivor.image)
        elif m == t:
            kept.append(("mine", box_id, m))
        else:  # edited differently on both sides: both versions go through the duplicate rule below
            kept.append(("mine", box_id, m))
            kept.append(("theirs", box_id, t))

    # Image state and background, before duplicates (the duplicate rule looks at image states).
    images: dict[str, dict[str, Any]] = {}
    for key in base.images.keys() | mine.images.keys() | theirs.images.keys():
        b, m, t = base.images.get(key), mine.images.get(key), theirs.images.get(key)
        state, conflict = _three_way(
            b["state"] if b else "unverified", m["state"] if m else "unverified", t["state"] if t else "unverified"
        )
        if conflict:
            state = "review"
            summary["state_conflicts"].add(key)
        background, background_conflict = _three_way(
            bool(b and b["background"]), bool(m and m["background"]), bool(t and t["background"])
        )
        if background_conflict:
            background = False
        source = m or t or b
        images[key] = {"name": source["name"], "state": state, "background": background,
                       "record": source["record"]}
        if m is None and t is not None and state != "unverified":
            summary["added"].add(key)

    def side_state(origin: str, key: str) -> str:
        side = mine if origin == "mine" else theirs
        return side.images[key]["state"] if key in side.images else "unverified"

    # Duplicates: only boxes contributed by different sides are compared.
    dropped: set[int] = set()
    by_image: dict[str, list[int]] = {}
    for index, (origin, _box_id, box) in enumerate(kept):
        by_image.setdefault(box.image, []).append(index)
    for key, indexes in by_image.items():
        mine_indexes = [index for index in indexes if kept[index][0] == "mine"]
        theirs_indexes = [index for index in indexes if kept[index][0] == "theirs"]
        for theirs_index in theirs_indexes:
            t = kept[theirs_index][2]
            for mine_index in mine_indexes:
                if mine_index in dropped or theirs_index in dropped:
                    continue
                m = kept[mine_index][2]
                if box_iou(m.bbox, t.bbox) < threshold:
                    continue
                if m.category != t.category:
                    summary["class_disagreements"].add(key)
                    continue
                # Same object boxed by both: keep one.
                rank_mine = (m.verified, side_state("mine", key) == "verified", True)
                rank_theirs = (t.verified, side_state("theirs", key) == "verified", False)
                dropped.add(theirs_index if rank_mine >= rank_theirs else mine_index)
                summary["duplicates"].add(key)

    # Ids: unchanged and merging-user boxes keep theirs; the other copy's boxes are renumbered on a clash.
    result_boxes: list[dict[str, Any]] = []
    used: set[Any] = set()
    pending: list[Box] = []
    for index, (origin, box_id, box) in enumerate(kept):  # first everything except the other copy's boxes
        if index not in dropped and origin != "theirs":
            used.add(box_id)
            result_boxes.append({"id": box_id, "box": box})
    for index, (origin, box_id, box) in enumerate(kept):
        if index in dropped or origin != "theirs":
            continue
        if box_id in used:
            pending.append(box)
        else:
            used.add(box_id)
            result_boxes.append({"id": box_id, "box": box})
    next_id = max((value for value in used if isinstance(value, int)), default=0) + 1
    for box in pending:
        while next_id in used:
            next_id += 1
        used.add(next_id)
        result_boxes.append({"id": next_id, "box": box})

    # Final image states: disagreements need another look; verification needs a basis; boxes clear background.
    boxes_by_image: dict[str, list[Box]] = {}
    for entry in result_boxes:
        boxes_by_image.setdefault(entry["box"].image, []).append(entry["box"])
    for key, image in images.items():
        image_boxes = boxes_by_image.get(key, [])
        if image_boxes:
            image["background"] = False
            if image["state"] == "unverified":  # dropped by one side but holding boxes the other changed
                image["state"] = "review"
        if key in summary["class_disagreements"]:
            image["state"] = "review"
        if image["state"] == "verified" and not image["background"] and not any(box.verified for box in image_boxes):
            image["state"] = "review"
        if image["state"] == "unverified" and key in mine.images:
            summary["removed"].add(key)

    mine_by_image: dict[str, list[Box]] = {}
    for box in mine.boxes.values():
        mine_by_image.setdefault(box.image, []).append(box)
    changed = set()
    for key, image in images.items():
        mine_image = mine.images.get(key)
        mine_boxes = sorted(box.content() for box in mine_by_image.get(key, [])) if mine_image else []
        result = sorted(box.content() for box in boxes_by_image.get(key, []))
        if (mine_image is None or mine_image["state"] != image["state"]
                or mine_image["background"] != image["background"] or mine_boxes != result):
            changed.add(key)
    names = {key: image["name"] for key, image in images.items()}
    return MergeResult(
        images, result_boxes, changed,
        {kind: sorted((names.get(key, key) for key in keys), key=str.casefold) for kind, keys in summary.items()},
    )
