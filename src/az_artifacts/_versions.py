"""Universal Package SemVer rules (not Python's PEP 440 version rules)."""

import re

from .errors import VersionNotFoundError
from .models import PackageVersion

_NUMBER = r"(?:0|[1-9][0-9]*)"
_VERSION = re.compile(
    rf"({_NUMBER})\.({_NUMBER})\.({_NUMBER})(?:-([0-9a-z-]+(?:\.[0-9a-z-]+)*))?\Z"
)
_PATTERN = re.compile(rf"(?:{_NUMBER}\.){{0,2}}\*\Z")
_NAME = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*\Z")


def validate_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValueError(
            "Package names must be lowercase with nonconsecutive -, _, or . separators"
        )


def version_number(version: str, *, stable_only: bool = False) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(version)
    if match is None:
        return None
    prerelease = match[4]
    if prerelease is not None:
        if stable_only or any(
            part.isdigit() and len(part) > 1 and part.startswith("0")
            for part in prerelease.split(".")
        ):
            return None
    return int(match[1]), int(match[2]), int(match[3])


def version_pattern(version: str) -> tuple[int, ...] | None:
    if _PATTERN.fullmatch(version):
        return tuple(int(part) for part in version.split(".")[:-1])
    if version_number(version) is None:
        raise ValueError("Use an exact Universal Package SemVer or *, major.*, or major.minor.*")
    return None


def resolve_version(
    name: str,
    prefix: tuple[int, ...],
    versions: tuple[PackageVersion, ...] | None,
) -> str:
    """Select from validated catalog results; None denotes an established missing package."""
    if versions is None:
        raise VersionNotFoundError(f"Universal Package {name!r} was not found in the feed")
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for entry in versions:
        if entry.is_deleted is True:
            continue
        version = entry.normalized_version or entry.version
        number = version_number(version, stable_only=True)
        if number is not None and number[: len(prefix)] == prefix:
            candidates.append((number, version))
    if not candidates:
        pattern = ".".join((*map(str, prefix), "*"))
        raise VersionNotFoundError(f"No released version of {name!r} matches {pattern!r}")
    return max(candidates)[1]
