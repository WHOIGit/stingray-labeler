"""Box edit history kept in the project file next to the COCO boxes.

Every box carries a ``box_id``, a ``rev`` and the UTC ``time`` of that version.
The box id only ties a box's versions together: 16 hex characters, random for a
box drawn in the app, never derived from a user, image or count, never changed
and never reused (see new_box_id and normalize_box_ids). Versions are formed when the project is saved, against the file as it
was last saved:

* unchanged boxes keep their version;
* a changed box moves its saved version to ``annotation_history`` and becomes
  the next rev, whoever changed it, so every saved version stays findable (a
  merge recognises the same box in two copies by a version they share);
* a deleted box moves its saved version to history, followed by a
  ``"deleted": true`` record.

A plain COCO file is the first history: its boxes become rev 1 without a time.
Nothing here touches the file system or the UI.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from collections import Counter
from typing import Any, Iterable

from .dataset import is_verified
from .merge import image_key

# Box fields that only describe history; training exports leave them out.
HISTORY_FIELDS = ("box_id", "rev", "time")


def utc_now() -> str:
    """Current UTC time with milliseconds, e.g. 2026-10-09T14:03:22.123Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_BOX_ID = re.compile(r"[0-9a-f]{16}")


def is_box_id(value: Any) -> bool:
    """True for a box id in the app's format: 16 lowercase hex characters."""
    return isinstance(value, str) and _BOX_ID.fullmatch(value) is not None


def new_box_id() -> str:
    """A temporary id for a box drawn in the app; its first save gives it its id by the rule (box_id_for)."""
    return uuid.uuid4().hex[:16]


def _derived_box_id(*parts: Any) -> str:
    """An id computed from fixed facts, so every reading of the same file agrees."""
    return hashlib.sha1("".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:16]


def canonical_time(value: Any) -> str:
    """A time in one standard form (UTC, milliseconds, e.g. 2026-10-09T14:03:22.123Z) whatever form it
    was written in; "" when there is none; the text itself when it cannot be read as a time."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def box_id_for(image_name: str, created: Any, bbox: Iterable[Any], duplicate: int = 0) -> str:
    """The id of a box from facts any platform has: its image's file name, when it was first created and
    where it was then (to 1/1000 pixel). ``duplicate`` counts boxes identical in all three.

    Kept the same in the master-data builder (rebuild_master_data.py), so a box gets one id wherever
    it is first given one.
    """
    name = str(image_name).replace("\\", "/").rsplit("/", 1)[-1].casefold()
    start = ",".join(f"{round(float(value), 3):.3f}" for value in bbox)
    return _derived_box_id("box", name, canonical_time(created), start, duplicate)


def normalize_box_ids(data: dict[str, Any]) -> int:
    """Give every box of a project without a box id in the app's format (a plain COCO file) one, in place:
    box_id_for(image name, its time, its box). Category and user play no part, so derived ids come out
    the same however often or wherever the file is read. Returns how many boxes were given an id.
    """
    name_by_id = {
        image.get("id"): image["file_name"] for image in data.get("images", [])
        if isinstance(image, dict) and isinstance(image.get("file_name"), str)
    }
    pending = []
    for position, record in enumerate([*data.get("annotations", []), *(data.get("annotation_history") or [])]):
        if isinstance(record, dict) and not is_box_id(record.get("box_id")):
            name = name_by_id.get(record.get("image_id"), str(record.get("image_id")))
            start = ",".join(f"{round(float(value), 3):.3f}" for value in record.get("bbox") or [])
            pending.append((name, canonical_time(record.get("time")), start, position, record))
    seen: dict[tuple, int] = {}
    for name, created, start, _position, record in sorted(pending, key=lambda entry: entry[:4]):
        duplicate = seen.get((name.casefold(), created, start), 0)
        seen[(name.casefold(), created, start)] = duplicate + 1
        record["box_id"] = box_id_for(name, created, start.split(",") if start else [], duplicate)
    return len(pending)


def box_note(record: dict[str, Any]) -> str:
    """A box's note ("" when it has none)."""
    return str(record.get("note") or "").strip()


def _content(record: dict[str, Any]) -> tuple:
    """What a version is about: class, geometry (to 1/1000 pixel) and verification. A note alone is not
    a new version."""
    bbox = tuple(round(float(value), 3) for value in record.get("bbox") or ())
    return record.get("category_id"), bbox, is_verified(record.get("verified", True))


def same_content(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """True when two versions have the same class, geometry and verification."""
    return _content(first) == _content(second)


def saved_boxes(base: dict[str, Any] | None, copy: bool = True,
                ) -> tuple[dict[tuple[str, Any], dict], dict[tuple[str, Any], dict]]:
    """The boxes of a saved file by (image key, box id), and by (image key, COCO id) for boxes
    the file gave no box id. Ids are normalized as when the file was opened, so they match.
    Boxes are copied, unless ``copy`` is False: the caller keeps ``base`` unchanged (e.g. the data a
    save just wrote); a box without an id is always copied before it is given one."""
    by_box_id: dict[tuple[str, Any], dict[str, Any]] = {}
    by_number: dict[tuple[str, Any], dict[str, Any]] = {}
    if base is None:
        return by_box_id, by_number
    base_keys = {
        image.get("id"): image_key(image["file_name"]) for image in base.get("images", [])
        if isinstance(image, dict) and isinstance(image.get("file_name"), str)
    }
    boxes = [dict(box) if copy or not is_box_id(box.get("box_id")) else box
             for box in base.get("annotations", []) if isinstance(box, dict)]
    unnumbered_ids = {id(box) for box in boxes if not is_box_id(box.get("box_id"))}
    if unnumbered_ids:  # as when the file was opened (a box's id depends only on the box itself)
        normalize_box_ids({"images": base.get("images", []), "annotations": boxes})
    for box in boxes:
        key = base_keys.get(box.get("image_id"))
        if key is not None:
            by_box_id[(key, box["box_id"])] = box
            if id(box) in unnumbered_ids:
                by_number[(key, box.get("id"))] = box  # in case the box id came out differently
    return by_box_id, by_number


def _history_record(saved: dict[str, Any], image_id: Any, **changes: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"box_id": saved["box_id"], "rev": saved.get("rev") or 1}
    if saved.get("time"):
        record["time"] = saved["time"]
    if saved.get("annotator_id") is not None:
        record["annotator_id"] = saved["annotator_id"]
    record.update({
        "image_id": image_id, "category_id": saved.get("category_id"),
        "bbox": list(saved.get("bbox") or []), "verified": is_verified(saved.get("verified", True)),
    })
    if box_note(saved):
        record["note"] = box_note(saved)
    for name, value in changes.items():
        if value is None:
            record.pop(name, None)
        else:
            record[name] = value
    return record


def record_versions(
    base: dict[str, Any] | None,
    images: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    history: list[dict[str, Any]],
    user_id: Any,
    now: str,
    purged: set[tuple[str, Any]] = frozenset(),
    user_remap: dict[Any, Any] | None = None,
    deleted_by: dict[tuple[str, Any], Any] | None = None,
    saved_index: tuple[dict, dict] | None = None,
    changed: set[Any] | None = None,
) -> list[dict[str, Any]]:
    """Version the boxes about to be saved against ``base``, the file as last saved.

    ``images`` and ``annotations`` are what will be written; each box's rev, time
    and annotator_id are set in place. Returns the new annotation_history, kept to
    the images being saved. Boxes in ``purged`` (image key, box id) were removed
    permanently: they leave no history and no deletion record. ``user_remap`` maps user ids
    merged or deleted since the save to the user now credited (None: unassigned), so a box
    that was only re-credited keeps its new user instead of getting the old one back.
    ``deleted_by`` maps (image key, box id) to the user who deleted that box; a deletion record
    is credited to them, or to ``user_id`` (who saves) when unknown. ``saved_index`` is ``saved_boxes(base)`` when the caller already has it (it is
    not changed here), so the last saved file is not parsed again. ``changed`` (image ids) limits the work
    to the images that can have changed since the save; every other image's boxes already carry their
    saved version and are kept as they are.
    """
    key_by_id = {image["id"]: image_key(image["file_name"]) for image in images}
    name_by_id = {image["id"]: image["file_name"] for image in images}
    taken_ids = {box.get("box_id") for box in annotations} | {record.get("box_id") for record in history}
    id_by_key = {key: image_id for image_id, key in key_by_id.items()}

    saved, saved_by_number = saved_index if saved_index is not None else saved_boxes(base)
    if user_remap:
        # Saved boxes read with users merged since; copies, so the index is not changed.
        copies: dict[int, dict[str, Any]] = {}

        def remapped(old: dict[str, Any]) -> dict[str, Any]:
            if id(old) not in copies:
                new = dict(old)
                if new.get("annotator_id") in user_remap:
                    if user_remap[new["annotator_id"]] is None:
                        new.pop("annotator_id", None)
                    else:
                        new["annotator_id"] = user_remap[new["annotator_id"]]
                copies[id(old)] = new
            return copies[id(old)]

        saved = {key: remapped(old) for key, old in saved.items()}
        saved_by_number = {key: remapped(old) for key, old in saved_by_number.items()}

    new_history = [
        record for record in history
        if record.get("image_id") in key_by_id
        and (key_by_id[record["image_id"]], record.get("box_id")) not in purged
    ]
    last_rev: dict[tuple[Any, Any], int] = {}
    for record in new_history:
        key = (record.get("image_id"), record.get("box_id"))
        last_rev[key] = max(last_rev.get(key, 0), record.get("rev") or 1)
    matched: set[int] = set()
    for box in annotations:
        if changed is not None and box["image_id"] not in changed:
            continue  # untouched since the save: already its saved version
        key = key_by_id[box["image_id"]]
        old = saved.get((key, box.get("box_id"))) or saved_by_number.get((key, box.get("id")))
        if old is None:
            earlier = last_rev.get((box["image_id"], box.get("box_id")))
            if earlier is not None and (box.get("rev") is None or box["rev"] <= earlier):
                box["rev"] = earlier + 1  # a deleted box brought back
                box["time"] = now
            elif box.get("rev") is None:  # drawn since the last save: its id by the rule, from now on
                box["rev"] = 1
                box["time"] = now
                duplicate = 0
                while True:
                    rule_id = box_id_for(name_by_id[box["image_id"]], now, box.get("bbox") or [], duplicate)
                    if rule_id not in taken_ids:
                        break
                    duplicate += 1
                taken_ids.add(rule_id)
                box["box_id"] = rule_id
            continue  # otherwise a version made elsewhere, e.g. brought in by a merge
        matched.add(id(old))
        old_rev = old.get("rev") or 1
        if _content(box) == _content(old):
            box["rev"] = old_rev
            for name in ("time", "annotator_id"):
                if old.get(name) is None:
                    box.pop(name, None)
                else:
                    box[name] = old[name]
        else:
            new_history.append(_history_record(old, box["image_id"]))
            # Above every rev the box has: after a merge its history can hold revs above the box's own.
            box["rev"] = max(old_rev, last_rev.get((box["image_id"], box.get("box_id")), 0)) + 1
            box["time"] = now

    for (key, box_id), old in saved.items():
        if id(old) in matched or key not in id_by_key or (key, box_id) in purged:
            continue  # kept, its image is not being saved (that is not a deletion), or removed for good
        if changed is not None and id_by_key[key] not in changed:
            continue  # untouched since the save: nothing deleted there
        image_id = id_by_key[key]
        new_history.append(_history_record(old, image_id))
        top = max(old.get("rev") or 1, last_rev.get((image_id, box_id), 0))
        new_history.append(_history_record(
            old, image_id, rev=top + 1, time=now, annotator_id=(deleted_by or {}).get((key, box_id), user_id),
            deleted=True,
        ))
    return new_history


def compact_history(
    history: list[dict[str, Any]], annotations: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, set[tuple[Any, Any]]]:
    """Drop a user's back-to-back versions of a box, keeping the newest of each run.

    A saved version is dropped when the box's next version (a later saved one, or the box
    itself) is by the same user and is not a deletion. Versions by other users, deletion
    records and the version before a deletion are kept. Revs are left as they are, so a box
    keeps its rev numbers with gaps. Returns the new history, how many versions were dropped,
    and the (image id, box id) of the boxes that lost versions.
    """
    chains: dict[tuple[Any, Any], list[tuple[int, dict[str, Any], bool]]] = {}
    for position, record in enumerate(history):
        chains.setdefault((record.get("image_id"), record.get("box_id")), []).append((position, record, False))
    for annotation in annotations:
        key = (annotation.get("image_id"), annotation.get("box_id"))
        if key in chains:
            chains[key].append((-1, annotation, True))
    dropped: set[int] = set()
    boxes: set[tuple[Any, Any]] = set()
    for key, chain in chains.items():
        chain.sort(key=lambda entry: (entry[1].get("rev") or 1, str(entry[1].get("time") or ""), entry[2]))
        for (position, record, _current), (_next_position, following, _next_current) in zip(chain, chain[1:]):
            if (not record.get("deleted") and not following.get("deleted")
                    and record.get("annotator_id") is not None
                    and record.get("annotator_id") == following.get("annotator_id")):
                dropped.add(position)
                boxes.add(key)
    kept = [record for position, record in enumerate(history) if position not in dropped]
    return kept, len(dropped), boxes


def record_states(
    base_states: dict[str, dict[str, Any]], images: list[dict[str, Any]], state_history: list[dict[str, Any]],
    user_id: Any, now: str, state_by: dict[str, Any] | None = None, user_remap: dict[Any, Any] | None = None,
) -> list[dict[str, Any]]:
    """Time image states like box versions, against the file as last saved.

    ``base_states`` maps image keys to the saved image's frame_state, background, state_time and
    state_by. An image whose state or background mark changed moves its saved state to the returned
    state history and gets ``state_time`` (now) and ``state_by`` (who changed it, from ``state_by``
    by image key, else ``user_id``) set in place; an unchanged image keeps its saved time and user; an
    image new to the file starts its state now. The history is kept to the images being saved.
    """
    saved_ids = {image["id"] for image in images}
    new_history = [entry for entry in state_history if entry.get("image_id") in saved_ids]
    for image in images:
        key = image_key(image["file_name"])
        old = base_states.get(key)
        state, background = image.get("frame_state"), bool(image.get("background"))
        by = (state_by or {}).get(key, user_id)
        if old is not None and (old["frame_state"], old["background"]) == (state, background):
            for name in ("state_time", "state_by"):
                value = old.get(name)
                if name == "state_by" and user_remap and value in user_remap:
                    value = user_remap[value]
                if value is None or value == "":
                    image.pop(name, None)
                else:
                    image[name] = value
            continue
        if old is not None:
            entry: dict[str, Any] = {"image_id": image["id"], "frame_state": old["frame_state"],
                                     "background": old["background"]}
            if old.get("state_time"):
                entry["time"] = old["state_time"]
            earlier_by = old.get("state_by")
            if user_remap and earlier_by in user_remap:
                earlier_by = user_remap[earlier_by]
            if earlier_by is not None:
                entry["annotator_id"] = earlier_by
            new_history.append(entry)
        image["state_time"] = now
        if by is not None:
            image["state_by"] = by
        else:
            image.pop("state_by", None)
    return new_history


def later_time(newest: str, now: str) -> str:
    """A save time that keeps times moving forward within a project whatever the PC clock says: ``now``,
    or 1 ms after ``newest`` (the newest time the project holds) when the clock is behind it."""
    newest, now = canonical_time(newest), canonical_time(now)
    if not newest or now > newest:
        return now
    moment = datetime.fromisoformat(newest.replace("Z", "+00:00")) + timedelta(milliseconds=1)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def newest_time(data: dict[str, Any]) -> str:
    """The newest time a project holds: of any box version, image state or data version."""
    times = [canonical_time(record.get("time"))
             for records in (data.get("annotations", []), data.get("annotation_history") or [],
                             data.get("state_history") or [], data.get("dataset_versions") or [])
             for record in records if isinstance(record, dict)]
    times += [canonical_time(image.get("state_time")) for image in data.get("images", []) if isinstance(image, dict)]
    return max(times, default="")


def _excluded(data: dict[str, Any], exclude: dict[str, Any] | None) -> tuple[set[tuple], set[tuple]]:
    """A data version's ``exclude`` (box versions and image states another copy made before it, brought
    in by a merge) as (image id, box id, time) and (image id, time)."""
    if not exclude:
        return set(), set()
    id_by_key = {image_key(image["file_name"]): image.get("id") for image in data.get("images", [])
                 if isinstance(image, dict) and isinstance(image.get("file_name"), str)}
    boxes = {(id_by_key.get(key), box_id, canonical_time(time)) for key, box_id, time in exclude.get("boxes", [])}
    states = {(id_by_key.get(key), canonical_time(time)) for key, time in exclude.get("states", [])}
    return boxes, states


def as_of(data: dict[str, Any], cutoff: str, exclude: dict[str, Any] | None = None,
          ) -> tuple[dict[Any, list[dict[str, Any]]], dict[Any, tuple[str, bool]]]:
    """The project as it was saved at ``cutoff`` (a canonical UTC time): each image's boxes, and its state
    and background mark. A box is its newest version saved by then (none, or a deletion: not there); a
    version or state without a time counts as there from the start. Versions and states in ``exclude`` (a
    data version's: made in another copy before it, and brought in by a merge) were not there. Pure:
    nothing is changed.
    """
    from .merge import legacy_state  # merge imports this module

    cutoff = canonical_time(cutoff)
    excluded_boxes, excluded_states = _excluded(data, exclude)
    chains: dict[tuple[Any, Any], list[tuple[tuple, dict[str, Any]]]] = {}
    for records, is_current in ((data.get("annotations", []), True), (data.get("annotation_history") or [], False)):
        for record in records:
            if not isinstance(record, dict):
                continue
            time = canonical_time(record.get("time"))
            if time and time > cutoff:
                continue
            if (record.get("image_id"), record.get("box_id"), time) in excluded_boxes:
                continue
            order = (time, record.get("rev") or 1, is_current)
            chains.setdefault((record.get("image_id"), record.get("box_id")), []).append((order, record))
    boxes: dict[Any, list[dict[str, Any]]] = {}
    for (image_id, _box_id), versions in chains.items():
        _order, newest = max(versions, key=lambda entry: entry[0])
        if not newest.get("deleted"):
            boxes.setdefault(image_id, []).append(newest)
    states: dict[Any, tuple[str, bool]] = {}
    timed: dict[Any, list[tuple[str, str, bool]]] = {}
    for entry in data.get("state_history") or []:
        if isinstance(entry, dict):
            timed.setdefault(entry.get("image_id"), []).append(
                (canonical_time(entry.get("time")), str(entry.get("frame_state")), bool(entry.get("background"))))
    for image in data.get("images", []):
        if not isinstance(image, dict):
            continue
        candidates = [*timed.get(image.get("id"), []),
                      (canonical_time(image.get("state_time")), legacy_state(image), bool(image.get("background")))]
        candidates = [candidate for candidate in candidates if (not candidate[0] or candidate[0] <= cutoff)
                      and (image.get("id"), candidate[0]) not in excluded_states]
        if candidates:
            _time, state, background = max(candidates, key=lambda candidate: candidate[0])
            states[image.get("id")] = (state, background)
        else:
            states[image.get("id")] = ("unverified", False)  # not in the project yet
    return boxes, states


def changed_images_in(data: dict[str, Any], start: str, end: str | None = None, user_id: Any = None,
                      exclude: dict[str, Any] | None = None,
                      previous_exclude: dict[str, Any] | None = None) -> set[Any]:
    """Images with a box version, deletion or image state that belongs to a data version, by ``user_id`` or
    anyone: saved after ``start`` and up to ``end`` (None: no end, the current version), or brought in by a
    merge that started it (in the previous version's ``previous_exclude``: made earlier by the other copy's
    clock). Work in the version's own ``exclude`` belongs to a later version."""
    start = canonical_time(start)
    end = canonical_time(end) if end else None
    excluded_boxes, excluded_states = _excluded(data, exclude)
    merged_boxes, merged_states = _excluded(data, previous_exclude)

    def during(time: str) -> bool:
        return time > start and (end is None or time <= end)

    changed: set[Any] = set()
    for records, is_box in ((data.get("annotations", []), True), (data.get("annotation_history") or [], True),
                            (data.get("state_history") or [], False)):
        for record in records:
            if not isinstance(record, dict) or (user_id is not None and record.get("annotator_id") != user_id):
                continue
            time = canonical_time(record.get("time"))
            key = (record.get("image_id"), record.get("box_id"), time) if is_box else (record.get("image_id"), time)
            if (during(time) or key in (merged_boxes if is_box else merged_states)) \
                    and key not in (excluded_boxes if is_box else excluded_states):
                changed.add(record.get("image_id"))
    for image in data.get("images", []):
        if not isinstance(image, dict) or (user_id is not None and image.get("state_by") != user_id):
            continue
        time = canonical_time(image.get("state_time"))
        key = (image.get("id"), time)
        if (during(time) or key in merged_states) and key not in excluded_states:
            changed.add(image.get("id"))
    return changed


def compare_versions(data: dict[str, Any], before: str, after: str, before_exclude: dict[str, Any] | None = None,
                     after_exclude: dict[str, Any] | None = None) -> dict[str, Any]:
    """What changed between two points in time (canonical UTC, with each data version's ``exclude``):
    boxes added, deleted and changed per category and per user (who made the later version, or deleted
    it), image states changed, and the images involved."""
    boxes_before, states_before = as_of(data, before, before_exclude)
    boxes_after, states_after = as_of(data, after, after_exclude)
    categories = {category.get("id"): str(category.get("name")) for category in
                  [*(data.get("retired_categories") or []), *data.get("categories", [])] if isinstance(category, dict)}
    users = {user.get("id"): str(user.get("name")) for user in data.get("annotators", []) or [] if isinstance(user, dict)}
    deleters: dict[tuple, Any] = {}
    after_time = canonical_time(after)
    for record in data.get("annotation_history") or []:
        if isinstance(record, dict) and record.get("deleted") and canonical_time(record.get("time")) <= after_time:
            deleters[(record.get("image_id"), record.get("box_id"))] = record.get("annotator_id")
    by_category: dict[str, Counter] = {}
    by_user: dict[str, Counter] = {}
    images: dict[Any, Counter] = {}

    def count(kind: str, image_id: Any, category_id: Any, user_id: Any) -> None:
        by_category.setdefault(categories.get(category_id, f"category {category_id}"), Counter())[kind] += 1
        by_user.setdefault(users.get(user_id, "Unassigned"), Counter())[kind] += 1
        images.setdefault(image_id, Counter())[kind] += 1

    for image_id in boxes_before.keys() | boxes_after.keys():
        old = {record.get("box_id"): record for record in boxes_before.get(image_id, [])}
        new = {record.get("box_id"): record for record in boxes_after.get(image_id, [])}
        for box_id in new.keys() - old.keys():
            count("added", image_id, new[box_id].get("category_id"), new[box_id].get("annotator_id"))
        for box_id in old.keys() - new.keys():
            count("deleted", image_id, old[box_id].get("category_id"), deleters.get((image_id, box_id)))
        for box_id in old.keys() & new.keys():
            if not same_content(old[box_id], new[box_id]):
                count("changed", image_id, new[box_id].get("category_id"), new[box_id].get("annotator_id"))
    state_changes = Counter()
    for image_id in states_before.keys() | states_after.keys():
        earlier, later = states_before.get(image_id, ("unverified", False)), states_after.get(image_id, ("unverified", False))
        if earlier != later:
            state_changes[later[0]] += 1
            images.setdefault(image_id, Counter())["state"] += 1
    return {"by_category": by_category, "by_user": by_user, "images": images, "states": state_changes}


def activity(data: dict[str, Any], start: str, end: str) -> list[dict[str, Any]]:
    """Per UTC day and user, between ``start`` and ``end`` (dates, YYYY-MM-DD, inclusive): boxes drawn,
    edited, verified and deleted, and images set Verified, Review or Unverified, from the saved history."""
    users = {user.get("id"): str(user.get("name")) for user in data.get("annotators", []) or [] if isinstance(user, dict)}
    rows: dict[tuple[str, str], Counter] = {}

    def add(time: str, user_id: Any, kind: str) -> None:
        day = time[:10]
        if time and start <= day <= end:
            rows.setdefault((day, users.get(user_id, "Unassigned")), Counter())[kind] += 1

    chains: dict[tuple, list[dict[str, Any]]] = {}
    for records in (data.get("annotations", []), data.get("annotation_history") or []):
        for record in records:
            if isinstance(record, dict):
                chains.setdefault((record.get("image_id"), record.get("box_id")), []).append(record)
    for versions in chains.values():
        versions.sort(key=lambda record: (canonical_time(record.get("time")), record.get("rev") or 1))
        previous = None
        for record in versions:
            time = canonical_time(record.get("time"))
            if record.get("deleted"):
                add(time, record.get("annotator_id"), "boxes_deleted")
            elif previous is None or previous.get("deleted"):
                add(time, record.get("annotator_id"), "boxes_drawn" if previous is None else "boxes_restored")
            else:
                add(time, record.get("annotator_id"), "boxes_edited")
                if is_verified(record.get("verified", True)) and not is_verified(previous.get("verified", True)):
                    add(time, record.get("annotator_id"), "boxes_verified")
            previous = record
    for entry in data.get("state_history") or []:  # earlier states: set at their own time, by their own user
        if isinstance(entry, dict) and entry.get("time"):
            add(canonical_time(entry["time"]), entry.get("annotator_id"), f"images_{entry.get('frame_state')}")
    for image in data.get("images", []):
        if isinstance(image, dict) and image.get("state_time"):
            add(canonical_time(image["state_time"]), image.get("state_by"), f"images_{image.get('frame_state')}")
    return [{"date": day, "user": user, **counts} for (day, user), counts in sorted(rows.items())]
