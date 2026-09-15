"""Portable logical paths/matching, separate from host destination validation."""

import os
import re
import sys
import unicodedata
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from wcmatch import glob

from .errors import NoMatchingFilesError, UnsafePathError
from .models import ManifestItem, PackageFile

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
    """Normalize a manifest path, allowing its optional single leading slash."""
    normalized = path.removeprefix("/")
    parts = normalized.split("/")
    if (
        any(part in ("", ".", "..") for part in parts)
        or "\\" in normalized
        or re.match(r"^[A-Za-z]:", normalized)
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in normalized)
    ):
        raise UnsafePathError(f"Invalid manifest path: {path!r}")
    return PurePosixPath(*parts)


def package_path(path: str | PurePosixPath) -> PurePosixPath:
    """Validate an explicit caller path; never interpret a host filesystem path."""
    if isinstance(path, Path) or not isinstance(path, (str, PurePosixPath)):
        raise ValueError("relative_path must be a package-relative string or PurePosixPath")
    text = path.as_posix() if isinstance(path, PurePosixPath) else path
    if text.startswith("/"):
        raise UnsafePathError("relative_path must be package-relative, without a leading slash")
    return relative_path(text)


def _validate_keys(keys: Sequence[str]) -> None:
    paths: set[str] = set()
    for key in keys:
        if key in paths:
            raise UnsafePathError(f"Duplicate manifest path or destination: {key!r}")
        paths.add(key)
    for key in paths:
        if any(parent.as_posix() in paths for parent in PurePosixPath(key).parents):
            raise UnsafePathError("Manifest contains overlapping file and directory paths")


def manifest_files(items: tuple[ManifestItem, ...]) -> tuple[PackageFile, ...]:
    files = tuple(
        PackageFile(relative_path(item.path), item.blob.size, item.blob.id) for item in items
    )
    _validate_keys(tuple(file.path.as_posix() for file in files))
    return files


def _validate_destination_path(path: PurePosixPath) -> None:
    # Preserve download's colon restriction on every host, including POSIX.
    if ":" in path.as_posix():
        raise UnsafePathError(f"Invalid manifest destination: {path!s}")
    if os.name == "nt" and any(
        part.endswith((".", " "))
        or part.split(".")[0].upper() in _WINDOWS_RESERVED
        or any(char in part for char in '<>"|?*')
        for part in path.parts
    ):
        raise UnsafePathError(f"Manifest path is not supported on Windows: {path!s}")


def _path_key(path: PurePosixPath) -> str:
    value = os.path.normcase(path.as_posix()).replace("\\", "/")
    if sys.platform == "darwin":
        value = unicodedata.normalize("NFD", value).casefold()
    return value


def validate_file_filter(file_filter: str | Sequence[str] | None) -> tuple[str, ...] | None:
    if file_filter is None:
        return None
    if not isinstance(file_filter, (str, Sequence)) or isinstance(file_filter, (bytes, bytearray)):
        raise ValueError("file_filter must be a string, sequence of strings, or None")
    patterns = (file_filter,) if isinstance(file_filter, str) else tuple(file_filter)
    if not patterns or any(not isinstance(pattern, str) or not pattern for pattern in patterns):
        raise ValueError("file_filter must contain nonempty patterns")
    for pattern in patterns:
        match_pattern = _match_pattern(pattern)
        if not match_pattern:
            raise ValueError("An exclusion filter must specify a pattern")
        glob.compile(match_pattern, flags=_GLOB_FLAGS)
    return patterns


def _match_pattern(pattern: str) -> str:
    return pattern[1:] if pattern.startswith("!") and not pattern.startswith("!(") else pattern


def filter_files(
    files: tuple[PackageFile, ...], patterns: tuple[str, ...] | None
) -> tuple[PackageFile, ...]:
    selected = []
    for file in files:
        matches = patterns is None
        for pattern in patterns or ():
            match_pattern = _match_pattern(pattern)
            if glob.globmatch(file.path.as_posix(), match_pattern, flags=_GLOB_FLAGS):
                matches = match_pattern == pattern
        if matches:
            selected.append(file)
    return tuple(selected)


def select_files(
    files: tuple[PackageFile, ...], file_filter: str | Sequence[str] | None
) -> tuple[tuple[PackageFile, Path], ...]:
    patterns = validate_file_filter(file_filter)
    for file in files:
        _validate_destination_path(file.path)
    _validate_keys(tuple(_path_key(file.path) for file in files))
    selected = filter_files(files, patterns)
    if patterns is not None and not selected:
        raise NoMatchingFilesError("No package files match file_filter")
    return tuple((file, Path(*file.path.parts)) for file in selected)


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
