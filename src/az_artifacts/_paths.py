"""Manifest path validation and ordered file matching."""

import os
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from wcmatch import glob

from .errors import NoMatchingFilesError, UnsafePathError
from .models import ManifestItem

_GLOB_FLAGS = glob.GLOBSTAR | glob.EXTGLOB | glob.BRACE | glob.DOTGLOB | glob.FORCEUNIX | glob.CASE
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def relative_path(path: str) -> PurePosixPath:
    normalized = path.removeprefix("/")
    parts = normalized.split("/")
    if (
        any(part in ("", ".", "..") for part in parts)
        or "\\" in normalized
        or ":" in normalized
        or any(ord(char) < 32 for char in normalized)
    ):
        raise UnsafePathError(f"Invalid manifest path: {path!r}")
    if os.name == "nt" and any(
        part.endswith((".", " "))
        or part.split(".")[0].upper() in _WINDOWS_RESERVED
        or any(char in part for char in '<>"|?*')
        for part in parts
    ):
        raise UnsafePathError(f"Manifest path is not supported on Windows: {path!r}")
    return PurePosixPath(*parts)


def _path_key(path: PurePosixPath) -> str:
    value = os.path.normcase(path.as_posix()).replace("\\", "/")
    if sys.platform == "darwin":
        value = unicodedata.normalize("NFD", value).casefold()
    return value


def select_files(
    items: tuple[ManifestItem, ...], file_filter: str | Sequence[str] | None
) -> tuple[tuple[ManifestItem, Path], ...]:
    patterns = [file_filter] if isinstance(file_filter, str) else file_filter
    if patterns is not None and (
        not patterns or any(not isinstance(pattern, str) or not pattern for pattern in patterns)
    ):
        raise ValueError("file_filter must contain nonempty patterns")
    paths: set[str] = set()
    all_files = []
    for item in items:
        path = relative_path(item.path)
        key = _path_key(path)
        if key in paths:
            raise UnsafePathError(f"Duplicate manifest destination: {item.path!r}")
        paths.add(key)
        all_files.append((item, path))
    for key in paths:
        if any(parent.as_posix() in paths for parent in PurePosixPath(key).parents):
            raise UnsafePathError("Manifest contains overlapping file and directory paths")
    selected = []
    for item, path in all_files:
        matches = patterns is None
        for pattern in patterns or ():
            exclude = pattern.startswith("!") and not pattern.startswith("!(")
            match_pattern = pattern[1:] if exclude else pattern
            if not match_pattern:
                raise ValueError("An exclusion filter must specify a pattern")
            if glob.globmatch(path.as_posix(), match_pattern, flags=_GLOB_FLAGS):
                matches = not exclude
        if matches:
            selected.append((item, Path(*path.parts)))
    if patterns is not None and not selected:
        raise NoMatchingFilesError("No package files match file_filter")
    return tuple(selected)


def prepare_destination(root: Path, relative: Path, *, overwrite: bool) -> Path:
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        if current.is_symlink() or not current.resolve().is_relative_to(root):
            raise UnsafePathError(f"Destination parent is linked or outside the output: {relative}")
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise UnsafePathError(f"Destination parent is not a directory: {relative}")
    destination = root / relative
    if destination.is_symlink() or not destination.resolve().is_relative_to(root):
        raise UnsafePathError(f"Destination is linked or outside the output: {relative}")
    if destination.exists():
        if not destination.is_file():
            raise UnsafePathError(f"Destination is not a regular file: {relative}")
        if not overwrite:
            raise FileExistsError(f"Destination already exists: {relative}")
    return destination
