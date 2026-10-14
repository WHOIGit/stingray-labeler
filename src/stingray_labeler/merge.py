"""Merge two copies of a project by combining their box histories.

Every change is combined: nothing is overwritten and no version is dropped. Each box's
versions from both copies are put together and ordered by time; time is used only for
ordering.

* Categories and users are settled first, by name (ignoring case): where the two copies'
  lists differ, the person merging decides for each name to keep it, merge it into another
  or (a category of the other copy) remove it. The decisions are given to ``merge`` as
  NameMaps and remembered in the project as ``name_aliases``.
* Boxes are matched by box id within an image. A box id is random, or derived once for an
  imported box, and never changes or is reused, so the same id in two copies is the same
  object. Should two copies hold one id with no version in common (ids from before box ids
  were random), they are different objects: both are kept and one gets a new id.
* With names settled, a box's versions from both copies are compared by what they say (box,
  category, user, verified, deleted) to see which are shared and which are new. If only one
  copy changed a box, that copy's state of the box is taken, so a box an earlier merge kept
  stays kept. If both changed it, that is a conflict: the box is kept if either copy still
  has it, shown as its newest kept version, and the image is set to Review for an expert to
  settle through the box history. Boxes changed in only one copy each are no conflict. A note
  is not part of a version: a version both copies hold keeps both copies' notes, joined by a
  comma. Image states are compared the same way, through their state histories: the copy that
  changed an image's state gives it; an image whose state both copies changed is set to Review.
* Images are matched by file name.

Nothing here touches the file system or the UI.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .dataset import is_verified

FRAME_STATES = ("verified", "review", "unverified")
# Fields a version is rebuilt from; anything else on a current box is carried along as "extra".
_BOX_FIELDS = {
    "id", "image_id", "category_id", "bbox", "area", "verified", "annotator_id",
    "box_id", "rev", "time", "deleted", "note",
}


@dataclass
class NameMap:
    """How one copy's category and user names are taken into the merge (keys ignore case).

    A name maps to the name it becomes. A category mapped to None is removed (its boxes are not
    taken); a user mapped to None leaves their boxes unassigned. Names not listed stay as they are.
    """

    categories: dict[str, str | None] = field(default_factory=dict)
    users: dict[str, str | None] = field(default_factory=dict)


@dataclass
class MergeResult:
    # image key -> {"name", "state", "background", "record"}
    images: dict[str, dict[str, Any]]
    # image key -> boxes: {"box_id", "renamed", "versions" (oldest first, rev set), "current": index or None}
    chains: dict[str, list[dict[str, Any]]]
    changed: set[str]  # image keys whose result differs from this copy
    summary: dict[str, list[str]] = field(default_factory=dict)  # kind -> image names
    versions_added: int = 0  # versions that came from the other copy only
    # What one copy alone made: (copy, image key, box id or None for an image state, time); for data versions.
    one_sided: list[tuple[str, str, Any, str]] = field(default_factory=list)
    # Categories the other copy merged into another: retired name (ignoring case) -> category it went to.
    merged_away: dict[str, str] = field(default_factory=dict)


def image_key(file_name: str) -> str:
    return str(file_name).replace("\\", "/").rsplit("/", 1)[-1].casefold()


def legacy_state(image: dict[str, Any], missing_state: str = "unverified") -> str:
    state = image.get("frame_state")
    if state in FRAME_STATES:
        return state
    return missing_state


def _category_names(data: dict[str, Any]) -> dict[Any, str]:
    """Category id -> name, including categories retired by a merge or delete (still named in history)."""
    names = {
        category["id"]: str(category["name"]) for category in data.get("retired_categories") or []
        if isinstance(category, dict) and "id" in category and "name" in category
    }
    names.update({
        category["id"]: str(category["name"]) for category in data.get("categories", [])
        if isinstance(category, dict) and "id" in category and "name" in category
    })
    return names


def name_counts(data: dict[str, Any]) -> tuple[dict[str, list], dict[str, list]]:
    """For the reconcile dialog: {name ignoring case: [name, boxes]} for categories and for users.

    Every listed category and user appears, with how many current boxes carry it.
    """
    category_names = _category_names(data)
    user_names = {user["id"]: str(user["name"]) for user in data.get("annotators", []) or []
                  if isinstance(user, dict) and "id" in user and "name" in user}
    categories = {str(category["name"]).casefold(): [str(category["name"]), 0]
                  for category in data.get("categories", []) if isinstance(category, dict) and "name" in category}
    users = {name.casefold(): [name, 0] for name in user_names.values()}
    for box in data.get("annotations", []):
        if not isinstance(box, dict):
            continue
        name = category_names.get(box.get("category_id"))
        if name is not None:
            categories.setdefault(name.casefold(), [name, 0])[1] += 1
        user = user_names.get(box.get("annotator_id"))
        if user:
            users.setdefault(user.casefold(), [user, 0])[1] += 1
    return categories, users


def shared_name_pairs(mine: dict[str, Any], theirs: dict[str, Any]) -> tuple[Counter, Counter]:
    """How the two copies name the category and user of each box version both hold (same image, box id
    and time), where the names differ: Counter of (name here, name there) for categories and for users.
    Such a pair means one copy renamed or merged that name since the copies parted."""
    from .history import canonical_time  # history imports this module

    def index(data: dict[str, Any]) -> dict[tuple, tuple[str | None, str | None]]:
        keys = {image.get("id"): image_key(image["file_name"]) for image in data.get("images", [])
                if isinstance(image, dict) and isinstance(image.get("file_name"), str)}
        categories = _category_names(data)
        users = {user["id"]: str(user["name"]) for user in data.get("annotators", []) or []
                 if isinstance(user, dict) and "id" in user and "name" in user}
        found = {}
        for record in (*data.get("annotations", []), *(data.get("annotation_history") or [])):
            if not isinstance(record, dict) or not record.get("time") or record.get("image_id") not in keys:
                continue
            found[(keys[record["image_id"]], record.get("box_id"), canonical_time(record["time"]))] = (
                categories.get(record.get("category_id")), users.get(record.get("annotator_id")))
        return found

    here, there = index(mine), index(theirs)
    categories: Counter = Counter()
    users: Counter = Counter()
    for key in here.keys() & there.keys():
        for counter, mine_name, theirs_name in ((categories, here[key][0], there[key][0]),
                                                (users, here[key][1], there[key][1])):
            if mine_name and theirs_name and mine_name.casefold() != theirs_name.casefold():
                counter[(mine_name, theirs_name)] += 1
    return categories, users


def merged_away(data: dict[str, Any]) -> dict[str, str]:
    """Categories this copy merged into another: retired name (ignoring case) -> the category it went to
    now, following later merges."""
    current = {category["id"]: str(category["name"]) for category in data.get("categories", [])
               if isinstance(category, dict) and "id" in category and "name" in category}
    retired = {category["id"]: category for category in data.get("retired_categories") or []
               if isinstance(category, dict) and "id" in category}
    result = {}
    for category in retired.values():
        target, seen = category.get("merged_into"), set()
        while target in retired and target not in seen:
            seen.add(target)
            target = retired[target].get("merged_into")
        if target in current:
            result[str(category["name"]).casefold()] = current[target]
    return result


def _content(version: dict[str, Any]) -> tuple:
    """What a version says, without any generated id, its time or its note (a note alone is no version)."""
    return (
        version["deleted"], (version["user"] or "").casefold(), version["category"].casefold(),
        tuple(round(value, 3) for value in version["bbox"]), version["verified"],
    )


def _joined_notes(*notes: str) -> str:
    """Notes of one version from both copies, joined by a comma; a part already there is not repeated."""
    parts: list[str] = []
    for note in notes:
        for part in (part.strip() for part in note.split(",")):
            if part and part.casefold() not in {kept.casefold() for kept in parts}:
                parts.append(part)
    return ", ".join(parts)


def _mapped(name: str | None, mapping: dict[str, str | None]) -> str | None:
    if not name:
        return name
    return mapping.get(name.casefold(), name)


_MASK = (1 << 64) - 1


def _fingerprints(data: dict[str, Any], names: NameMap) -> tuple[dict[str, int], set[str]]:
    """One number per image summing up its state and every box version (names as mapped), so two copies
    can be compared image by image without building anything; and the images whose names the mapping
    changes (they must be rewritten even if both copies agree)."""
    from .history import is_box_id, normalize_box_ids  # history imports this module

    key_by_id: dict[Any, str] = {}
    prints: dict[str, int] = {}
    for image in data.get("images", []):
        if isinstance(image, dict) and "file_name" in image:
            key = image_key(image["file_name"])
            key_by_id[image.get("id")] = key
            prints[key] = hash(("image", legacy_state(image), is_verified(image.get("background", False)),
                                str(image.get("state_time") or ""), bool(image.get("favorite")))) & _MASK
    categories = _category_names(data)
    users = {user["id"]: str(user["name"]) for user in data.get("annotators", []) or []
             if isinstance(user, dict) and "id" in user and "name" in user}
    records = [
        (record if is_box_id(record.get("box_id")) else dict(record), is_current)
        for records, is_current in ((data.get("annotations", []), True), (data.get("annotation_history") or [], False))
        for record in records if isinstance(record, dict)
    ]
    normalize_box_ids({"images": data.get("images", []), "categories": data.get("categories", []),
                       "annotations": [record for record, current in records if current],
                       "annotation_history": [record for record, current in records if not current]})
    renamed: set[str] = set()
    for record, is_current in records:
        key = key_by_id.get(record.get("image_id"))
        bbox = record.get("bbox")
        if key is None or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        raw_user = users.get(record.get("annotator_id"))
        raw_category = categories.get(record.get("category_id"), f"category {record.get('category_id')}")
        category, user = _mapped(raw_category, names.categories), _mapped(raw_user, names.users)
        if category != raw_category or user != raw_user:
            renamed.add(key)
        if category is None:
            continue
        prints[key] = (prints[key] + hash((
            record.get("box_id"), str(record.get("time") or ""), record.get("rev") or 1, is_current,
            bool(record.get("deleted")) and not is_current, (user or "").casefold(), category.casefold(),
            tuple(round(float(value), 3) for value in bbox), is_verified(record.get("verified", True)),
            str(record.get("note") or "").strip(),
        ))) & _MASK
    for entry in data.get("state_history") or []:  # earlier image states
        key = key_by_id.get(entry.get("image_id")) if isinstance(entry, dict) else None
        if key is not None:
            prints[key] = (prints[key] + hash(("state", str(entry.get("time") or ""), entry.get("frame_state"),
                                               bool(entry.get("background"))))) & _MASK
    return prints, renamed


def _side(data: dict[str, Any], names: NameMap | None = None,
          keys: set[str] | None = None) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Index a project: its images by key, and each image's boxes as chains of versions, oldest first.

    Names are mapped as decided. Each version gets a ``label``: its content plus how many earlier
    versions of the same box said the same thing, so a box that goes back to an earlier state
    still has distinct versions.
    """
    from .history import normalize_box_ids  # history imports this module

    names = names or NameMap()
    images: dict[str, dict[str, Any]] = {}
    key_by_id: dict[Any, str] = {}
    for image in data.get("images", []):
        if not isinstance(image, dict) or "file_name" not in image:
            continue
        key = image_key(image["file_name"])
        if keys is not None and key not in keys:
            continue  # the same in both copies: not looked at
        images[key] = {
            "name": str(image["file_name"]).replace("\\", "/").rsplit("/", 1)[-1],
            "state": legacy_state(image),
            "background": is_verified(image.get("background", False)),
            "record": image,
        }
        key_by_id[image.get("id")] = key
    categories = _category_names(data)
    users = {
        user["id"]: str(user["name"]) for user in data.get("annotators", []) or []
        if isinstance(user, dict) and "id" in user and "name" in user
    }
    # Each image's state: when it was set and by whom, and its earlier states.
    for key, image in images.items():
        record = image["record"]
        image["state_time"] = str(record.get("state_time") or "")
        image["state_by"] = users.get(record.get("state_by"))
        image["state_history"] = []
    for entry in data.get("state_history") or []:
        key = key_by_id.get(entry.get("image_id")) if isinstance(entry, dict) else None
        if key is not None:
            images[key]["state_history"].append({
                "frame_state": entry.get("frame_state"), "background": bool(entry.get("background")),
                "time": str(entry.get("time") or ""), "user": users.get(entry.get("annotator_id")),
            })
    current = [dict(box) for box in data.get("annotations", [])
               if isinstance(box, dict) and box.get("image_id") in key_by_id]
    history = [dict(record) for record in data.get("annotation_history") or []
               if isinstance(record, dict) and record.get("image_id") in key_by_id]
    # Boxes without an id in the app's format get derived ones, the same in every copy of a file.
    normalize_box_ids({"images": data.get("images", []), "categories": data.get("categories", []),
                       "annotations": current, "annotation_history": history})

    grouped: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    for record, is_current in [*((box, True) for box in current), *((record, False) for record in history)]:
        key = key_by_id.get(record.get("image_id"))
        if key is None or not isinstance(record.get("bbox"), list) or len(record["bbox"]) != 4:
            continue
        raw_user = users.get(record.get("annotator_id"))
        category_id = record.get("category_id")
        raw_category = categories.get(category_id, f"category {category_id}")
        category = _mapped(raw_category, names.categories)
        if category is None:
            continue  # a category removed in this merge: its boxes are not taken
        user = _mapped(raw_user, names.users)
        grouped.setdefault((key, record.get("box_id")), []).append({
            "rev": record.get("rev") or 1,
            "time": str(record.get("time") or ""),
            "user": user,
            "category": category,
            "renamed": category != raw_category or user != raw_user,
            "bbox": [float(value) for value in record["bbox"]],
            "verified": is_verified(record.get("verified", True)),
            "note": str(record.get("note") or "").strip(),
            "deleted": bool(record.get("deleted")) and not is_current,
            "current": is_current,
            "extra": {name: value for name, value in record.items() if name not in _BOX_FIELDS} if is_current else {},
            "id": record.get("id") if is_current else None,
        })
    chains: dict[str, list[dict[str, Any]]] = {}
    for (key, box_id), versions in grouped.items():
        versions.sort(key=lambda version: (version["time"], version["rev"], version["current"]))
        seen: dict[tuple, int] = {}
        for version in versions:
            content = _content(version)
            version["label"] = (content, seen.get(content, 0))
            seen[content] = seen.get(content, 0) + 1
        chains.setdefault(key, []).append({
            "box_id": box_id, "versions": versions,
            "current": next((version["label"] for version in versions if version["current"]), None),
        })
    # An unverified image with no boxes holds no work, so it is not part of this copy.
    images = {key: image for key, image in images.items() if image["state"] != "unverified" or key in chains}
    return images, chains


def _other_id(key: str, box_id: Any) -> str:
    """The id a box of the other copy gets when its own id is taken here by a different object; the same
    on every merge, so merging the same copies again finds it rather than adding it again."""
    from .history import _derived_box_id  # history imports this module

    return _derived_box_id("other", key, box_id)


def _state_labels(image: dict[str, Any] | None) -> set[tuple]:
    """An image's states, earlier ones and the current one, each as (state, background, how many times it
    was that before), in time order: what the copy holds, to tell which copy changed the state."""
    if image is None:
        return set()
    entries = sorted([*image.get("state_history", []),
                      {"frame_state": image["state"], "background": image["background"], "time": image["state_time"]}],
                     key=lambda entry: entry["time"] or "")
    seen: dict[tuple, int] = {}
    labels = set()
    for entry in entries:
        content = (entry["frame_state"], bool(entry["background"]))
        labels.add((*content, seen.get(content, 0)))
        seen[content] = seen.get(content, 0) + 1
    return labels


def _pair(mine_chains: list[dict], theirs_chains: list[dict], key: str) -> list[tuple[dict | None, dict | None]]:
    """Pair this copy's boxes on an image with the other copy's by box id.

    The same id is the same object, unless the two share no version at all (ids from before box
    ids were random could collide): then they are kept apart as two boxes, the other copy's under
    the id _other_id gives it (where an earlier merge may already have put it).
    """
    by_id = {chain["box_id"]: chain for chain in mine_chains}
    pairs: list[tuple[dict | None, dict | None]] = []
    taken: set[Any] = set()
    for theirs in theirs_chains:
        labels = {version["label"] for version in theirs["versions"]}
        for candidate in (theirs["box_id"], _other_id(key, theirs["box_id"])):
            mine = by_id.get(candidate)
            if mine is not None and candidate not in taken and labels & {
                    version["label"] for version in mine["versions"]}:
                taken.add(candidate)
                pairs.append((mine, theirs))
                break
        else:
            pairs.append((None, theirs))
    pairs.extend((mine, None) for box_id, mine in by_id.items() if box_id not in taken)
    return pairs


def _combine_image(mine: list[dict], theirs: list[dict], key: str) -> tuple[list[dict], int, bool, bool]:
    """Combine one image's boxes: (boxes, versions added, the other copy has changes, a box changed in both)."""
    boxes: list[dict[str, Any]] = []
    added = 0
    theirs_new_any = conflict_any = False
    used_box_ids = {chain["box_id"] for chain in mine}
    for m, t in _pair(mine, theirs, key):
        mine_versions = m["versions"] if m else []
        theirs_versions = t["versions"] if t else []
        mine_labels = {version["label"] for version in mine_versions}
        theirs_labels = {version["label"] for version in theirs_versions}
        new = [version for version in theirs_versions if version["label"] not in mine_labels]
        mine_new = bool(mine_labels - theirs_labels)
        # A version both copies hold keeps both copies' notes.
        theirs_notes = {version["label"]: version["note"] for version in theirs_versions}
        notes_joined = False
        for index, version in enumerate(mine_versions):
            note = _joined_notes(version["note"], theirs_notes.get(version["label"], ""))
            if note != version["note"]:
                mine_versions = [*mine_versions[:index], dict(version, note=note), *mine_versions[index + 1:]]
                notes_joined = True
        added += len(new)
        theirs_new_any |= bool(new)
        versions = sorted(mine_versions + new, key=lambda version: version["time"])  # stable: ties keep order
        conflict = bool(new and mine_new)
        conflict_any |= conflict
        if conflict:
            # Changed in both copies: kept if either copy still has it, as its newest kept version.
            alive = [version for version in versions if not version["deleted"]]
            kept = (m is not None and m["current"] is not None) or (t is not None and t["current"] is not None)
            current = alive[-1]["label"] if kept and alive else None
        elif new:
            current = t["current"]  # only the other copy changed it
        else:
            current = m["current"]  # only this copy changed it, or neither
        if m is not None:
            box_id, renamed = m["box_id"], False
        else:
            renamed = t["box_id"] in used_box_ids
            box_id = _other_id(key, t["box_id"]) if renamed else t["box_id"]
        used_box_ids.add(box_id)
        # Versions stay in time order, except that the version shown is numbered last: the highest rev is
        # always the box's current state (an older deletion kept over by an edit sits below it).
        shown = next((version for version in versions if version["label"] == current), None)
        if shown is not None:
            versions = [version for version in versions if version is not shown] + [shown]
        numbered = [dict(version, rev=rev, only=("theirs" if version["label"] not in mine_labels else
                                                 "mine" if version["label"] not in theirs_labels else None))
                    for rev, version in enumerate(versions, start=1)]
        boxes.append({
            "box_id": box_id, "renamed": renamed, "versions": numbered,
            "current": next((index for index, version in enumerate(numbered) if version["label"] == current), None),
            # Changed: versions came in, the shown version moved, or this copy's names were remapped.
            "changed": bool(new) or notes_joined or current != (m["current"] if m else None)
            or any(version["renamed"] for version in mine_versions),
        })
    return boxes, added, theirs_new_any, conflict_any


def merge(mine: dict[str, Any], theirs: dict[str, Any],
          mine_names: NameMap | None = None, theirs_names: NameMap | None = None) -> MergeResult:
    """Combine the other copy (``theirs``) with this one (``mine``), names mapped as decided.

    Images are compared by fingerprint first; only images that differ (or whose names were mapped)
    are opened box by box.
    """
    mine_names, theirs_names = mine_names or NameMap(), theirs_names or NameMap()
    mine_prints, mine_renamed = _fingerprints(mine, mine_names)
    theirs_prints, theirs_renamed = _fingerprints(theirs, theirs_names)
    differing = {key for key in mine_prints.keys() | theirs_prints.keys()
                 if mine_prints.get(key) != theirs_prints.get(key)} | mine_renamed | theirs_renamed
    mine_images, mine_chains = _side(mine, mine_names, differing)
    theirs_images, theirs_chains = _side(theirs, theirs_names, differing)
    summary: dict[str, set[str]] = {"review": set(), "added": set(), "from_theirs": set(), "unverified_here": set()}
    images: dict[str, dict[str, Any]] = {}
    chains: dict[str, list[dict[str, Any]]] = {}
    changed: set[str] = set()
    versions_added = 0
    # What one copy alone made, for the data versions of the other: (copy, image key, box id or None, time).
    one_sided: list[tuple[str, str, Any, str]] = []
    for key in mine_images.keys() | theirs_images.keys():
        m, t = mine_images.get(key), theirs_images.get(key)
        boxes, added, theirs_new, box_conflict = _combine_image(
            mine_chains.get(key, []), theirs_chains.get(key, []), key)
        versions_added += added
        conflict = box_conflict
        if m and t:
            if m["state"] == "unverified" and theirs_new:
                summary["unverified_here"].add(key)
            # Image states are compared like box versions, through their histories: the copy that changed
            # the state since the state both last shared gives it; both having changed it is a conflict.
            mine_states, theirs_states = _state_labels(m), _state_labels(t)
            mine_moved, theirs_moved = bool(mine_states - theirs_states), bool(theirs_states - mine_states)
            conflict = conflict or (mine_moved and theirs_moved)
            source = t if theirs_moved and not mine_moved else m
        else:
            source = m or t
        state_source = source
        state, background = source["state"], source["background"]
        if conflict:
            state = "review"
            summary["review"].add(key)
        if m is not None and any(box["changed"] for box in boxes):
            # Boxes were combined: a background mark goes once there are boxes, and verifying needs every box
            # verified or a background mark. An image the merge does not touch keeps its state as is.
            current = [box["versions"][box["current"]] for box in boxes if box["current"] is not None]
            if current:
                background = False
            if state == "verified" and not background and not (
                    current and all(version["verified"] for version in current)):
                state = "review"
        if m is None:
            summary["added"].add(key)
        if theirs_new:
            summary["from_theirs"].add(key)
        source = m or t
        # Image states: every earlier state of both copies is kept, ordered by time; when the two
        # copies' states differed, both are kept as earlier states and the image is Review from then on.
        state_history: list[dict[str, Any]] = []
        seen_states: set[tuple] = set()
        candidates = [*(m["state_history"] if m else []), *(t["state_history"] if t else [])]
        if conflict:
            candidates += [{"frame_state": side["state"], "background": side["background"],
                            "time": side["state_time"], "user": side["state_by"]} for side in (m, t) if side]
        for entry in sorted(candidates, key=lambda entry: entry["time"]):
            identity = (entry["time"], entry["frame_state"], entry["background"])
            if identity not in seen_states:
                seen_states.add(identity)
                state_history.append(entry)
        if conflict:
            state_time = max((side["state_time"] for side in (m, t) if side), default="")
            state_by = None
        else:
            state_time, state_by = state_source["state_time"], state_source["state_by"]
        favorite = any(side["record"].get("favorite") for side in (m, t) if side)  # a favorite in either copy
        images[key] = {"name": source["name"], "state": state, "background": background,
                       "record": source["record"], "state_time": state_time, "state_by": state_by,
                       "state_history": state_history, "favorite": favorite}
        chains[key] = boxes
        for box in boxes:
            one_sided += [(version["only"], key, box["box_id"], version["time"])
                          for version in box["versions"] if version["only"]]
        side_states = {name: {(entry["time"], entry["frame_state"], bool(entry["background"]))
                              for entry in [*side["state_history"], {"time": side["state_time"],
                                            "frame_state": side["state"], "background": side["background"]}]}
                       for name, side in (("mine", m), ("theirs", t)) if side}
        for name, other in (("mine", "theirs"), ("theirs", "mine")):
            one_sided += [(name, key, None, time) for time, *_state in
                          side_states.get(name, set()) - side_states.get(other, set())]
        if (m is None or m["state"] != state or m["background"] != background
                or bool(m["record"].get("favorite")) != favorite
                or len(state_history) != len(m["state_history"]) or state_time != m["state_time"]
                or any(box["changed"] for box in boxes)):
            changed.add(key)
    names = {key: image["name"] for key, image in images.items()}
    return MergeResult(
        images, chains, changed,
        {kind: sorted((names.get(key, key) for key in keys), key=str.casefold) for kind, keys in summary.items()},
        versions_added, one_sided,
        {name: _mapped(target, theirs_names.categories) or target for name, target in merged_away(theirs).items()},
    )


def _data_versions(project: dict[str, Any], one_sided: list[tuple[str, str, Any, str]],
                   new_version: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The data versions after a merge: this copy's own (the other copy's private versions are not taken),
    then ``new_version``, which the merged project starts. A version holds what was made until the next one
    starts, so each of this copy's versions still holds exactly its own work: a box version or image state
    only the other copy had, made before the version ended (by that copy's clock), is added to its
    ``exclude``; it belongs to the version the merge starts."""
    from .history import canonical_time  # history imports this module

    tags = [json_copy(tag) for tag in project.get("dataset_versions") or [] if isinstance(tag, dict)]
    if new_version is not None:
        tags.append(json_copy(new_version))
    starts = sorted(canonical_time(tag.get("time")) for tag in tags)
    for tag in tags:
        time = canonical_time(tag.get("time"))
        end = next((later for later in starts if later > time), None)  # when the next version starts
        if end is None:
            continue  # the newest holds everything: it is the current project
        exclude = tag.setdefault("exclude", {})
        known = {kind: {tuple(entry) for entry in exclude.get(kind, [])} for kind in ("boxes", "states")}
        for copy, key, box_id, made in one_sided:
            made = canonical_time(made)
            if copy != "theirs" or (made and made > end):
                continue  # this copy's own work, or made after the version ended
            kind, entry = ("boxes", (key, box_id, made)) if box_id is not None else ("states", (key, made))
            if entry not in known[kind]:
                exclude.setdefault(kind, []).append(list(entry))
                known[kind].add(entry)
        for kind in ("boxes", "states"):
            if not exclude.get(kind):
                exclude.pop(kind, None)
        if not exclude:
            tag.pop("exclude")
    return sorted(tags, key=lambda tag: canonical_time(tag.get("time")))


def json_copy(value: Any) -> Any:
    """A copy of plain JSON data (dicts, lists, values) that shares nothing with the original."""
    if isinstance(value, dict):
        return {key: json_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_copy(item) for item in value]
    return value


class _Ids:
    """Hands out ids above every id already used, without rescanning the used set each time."""

    def __init__(self, used: set[Any]) -> None:
        self.used = used
        self.next = max((value for value in used if isinstance(value, int)), default=0) + 1

    def take(self) -> int:
        while self.next in self.used:
            self.next += 1
        self.used.add(self.next)
        return self.next


def build_project(
    project: dict[str, Any], outcome: MergeResult, reserved_image_ids: set[Any],
    mine_names: NameMap | None = None, aliases: dict[str, dict[str, str | None]] | None = None,
    new_version: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], set[Any], set[Any]]:
    """Write a merge result into a copy of ``project`` (this copy, which ``merge`` was given as ``mine``).

    Only changed images are rewritten. Category and user ids are looked up by name ignoring case,
    and new ones never reuse an id seen anywhere in the project. This copy's categories and users
    merged into another (``mine_names``) leave the lists; a category stays listed as retired, with the
    category it went to. A category only earlier versions name is listed as retired, not brought back:
    only a current box brings a retired category back. ``aliases`` (the decisions made) are kept in the
    project.
    Returns the new project, the ids of the changed images and of the images added.
    """

    mine_names = mine_names or NameMap()
    result = dict(project)
    images = [dict(image) for image in project.get("images", [])]
    annotations = [dict(annotation) for annotation in project.get("annotations", [])]
    history = [dict(record) for record in project.get("annotation_history") or []]
    categories = [dict(category) for category in project.get("categories", [])]
    retired = [dict(category) for category in project.get("retired_categories") or []]
    users = [dict(user) for user in project.get("annotators", []) or []]

    # This copy's names merged into another leave the lists.
    gone = {name for name, target in mine_names.categories.items() if target is None or target.casefold() != name}
    retired += [category for category in categories if str(category["name"]).casefold() in gone]
    categories = [category for category in categories if str(category["name"]).casefold() not in gone]
    gone_users = {name for name, target in mine_names.users.items() if target is None or target.casefold() != name}
    users = [user for user in users if str(user["name"]).casefold() not in gone_users]

    category_by_name = {str(category["name"]).casefold(): category["id"] for category in categories}
    retired_by_name = {str(category["name"]).casefold(): category for category in retired}
    user_by_name = {str(user["name"]).casefold(): user["id"] for user in users}
    category_ids = _Ids({category["id"] for category in (*categories, *retired)} | {
        record.get("category_id") for record in (*annotations, *history)})
    user_ids = _Ids({user["id"] for user in project.get("annotators", []) or []} | {
        record.get("annotator_id") for record in (*annotations, *history)})

    def category_id(name: str, current: bool = True) -> Any:
        key = name.casefold()
        if key in category_by_name:
            return category_by_name[key]
        if not current:  # an earlier version: its category is kept as retired, not brought back
            if key not in retired_by_name:
                category = {"id": category_ids.take(), "name": name}
                target = outcome.merged_away.get(key)
                if target is not None and target.casefold() in category_by_name:
                    category["merged_into"] = category_by_name[target.casefold()]
                retired.append(category)
                retired_by_name[key] = category
            return retired_by_name[key]["id"]
        if key in retired_by_name:  # a retired category on a current box again comes back under its id
            category = retired_by_name.pop(key)
            retired.remove(category)
            category.pop("merged_into", None)
        else:
            category = {"id": category_ids.take(), "name": name}
        categories.append(category)
        category_by_name[key] = category["id"]
        return category["id"]

    # This copy's categories merged away in this merge record where they went.
    for category in list(retired):
        target = mine_names.categories.get(str(category["name"]).casefold())
        if target is not None and target.casefold() != str(category["name"]).casefold():
            category["merged_into"] = category_id(target)

    def user_id(name: str | None) -> Any:
        if not name:
            return None
        if name.casefold() not in user_by_name:
            new_id = user_ids.take()
            users.append({"id": new_id, "name": name})
            user_by_name[name.casefold()] = new_id
        return user_by_name[name.casefold()]

    image_by_key = {image_key(image["file_name"]): image for image in images}
    image_ids = _Ids(set(reserved_image_ids) | {image["id"] for image in images})
    changed_ids: set[Any] = set()
    added_ids: set[Any] = set()
    for key in sorted(outcome.changed):
        merged = outcome.images[key]
        image = image_by_key.get(key)
        if image is None:
            image = dict(merged["record"])  # keeps the other copy's COCO fields
            image["id"] = image_ids.take()
            image["file_name"] = merged["name"]
            images.append(image)
            image_by_key[key] = image
            added_ids.add(image["id"])
        image["frame_state"] = merged["state"]
        image["background"] = merged["background"]
        if merged.get("favorite"):
            image["favorite"] = True
        else:
            image.pop("favorite", None)
        if merged.get("state_time"):
            image["state_time"] = merged["state_time"]
        else:
            image.pop("state_time", None)
        image.pop("state_by", None)
        changed_ids.add(image["id"])

    kept_annotations = [annotation for annotation in annotations if annotation.get("image_id") not in changed_ids]
    kept_history = [record for record in history if record.get("image_id") not in changed_ids]
    annotation_ids = _Ids({annotation.get("id") for annotation in kept_annotations})
    for key in sorted(outcome.changed):
        image_id = image_by_key[key]["id"]
        for box in outcome.chains.get(key, []):
            box_id = box["box_id"]  # a different box with a taken id already has its own (see _other_id)
            for index, version in enumerate(box["versions"]):
                record: dict[str, Any] = {"box_id": box_id, "rev": version["rev"]}
                if version["time"]:
                    record["time"] = version["time"]
                annotator = user_id(version["user"])
                if annotator is not None:
                    record["annotator_id"] = annotator
                x, y, width, height = version["bbox"]
                if index == box["current"]:
                    annotation = dict(version["extra"])
                    annotation_id = version["id"]
                    if annotation_id is None or annotation_id in annotation_ids.used:
                        annotation_id = annotation_ids.take()
                    annotation_ids.used.add(annotation_id)
                    annotation.update({
                        "id": annotation_id, "image_id": image_id, "category_id": category_id(version["category"]),
                        "bbox": [x, y, width, height], "area": width * height, "verified": version["verified"],
                    })
                    if version["note"]:
                        annotation["note"] = version["note"]
                    annotation.setdefault("iscrowd", 0)
                    annotation.update(record)
                    kept_annotations.append(annotation)
                else:
                    record.update({
                        "image_id": image_id, "category_id": category_id(version["category"], current=False),
                        "bbox": [x, y, width, height], "verified": version["verified"],
                    })
                    if version["note"]:
                        record["note"] = version["note"]
                    if version["deleted"]:
                        record["deleted"] = True
                    kept_history.append(record)

    # Image states of the changed images: who set the current one, and the earlier ones.
    state_history = [dict(entry) for entry in project.get("state_history") or []
                     if isinstance(entry, dict) and entry.get("image_id") not in changed_ids]
    for key in sorted(outcome.changed):
        image = image_by_key[key]
        merged = outcome.images[key]
        by = user_id(merged.get("state_by"))
        if by is not None:
            image["state_by"] = by
        for entry in merged.get("state_history", []):
            record: dict[str, Any] = {"image_id": image["id"], "frame_state": entry["frame_state"],
                                      "background": entry["background"]}
            if entry["time"]:
                record["time"] = entry["time"]
            annotator = user_id(entry.get("user"))
            if annotator is not None:
                record["annotator_id"] = annotator
            state_history.append(record)

    result.update({
        "images": images, "annotations": kept_annotations, "annotation_history": kept_history,
        "categories": categories, "annotators": users,
    })
    if state_history:
        result["state_history"] = state_history
    versions = _data_versions(project, outcome.one_sided, new_version)  # the merge starts ``new_version``
    if versions:
        result["dataset_versions"] = versions
    if retired:
        result["retired_categories"] = retired
    if aliases:
        remembered = {kind: dict((project.get("name_aliases") or {}).get(kind, {})) for kind in ("categories", "users")}
        for kind in ("categories", "users"):
            remembered[kind].update(aliases.get(kind, {}))
        result["name_aliases"] = remembered
    return result, changed_ids, added_ids
