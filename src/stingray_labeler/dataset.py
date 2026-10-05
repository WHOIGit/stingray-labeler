"""Project data and filesystem helpers used by the annotator."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def is_verified(value: Any) -> bool:
    """Treat absent legacy verification metadata as verified."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}


def dataset_image_path(root: Path, file_name: str) -> Path:
    """Resolve an image name directly inside the selected image folder."""
    parts = [part for part in str(file_name).replace("\\", "/").split("/") if part]
    if not parts or ".." in parts:
        raise ValueError(f"Invalid image filename: {file_name}")
    return root / parts[-1]


def directory_names(folder: Path) -> set[str]:
    """List immediate entry names without statting or opening the entries."""
    try:
        with os.scandir(folder) as entries:
            return {entry.name.casefold() for entry in entries}
    except FileNotFoundError:
        return set()


def path_key(path: Path) -> str:
    """Normalize a path string without checking whether its target exists."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))

