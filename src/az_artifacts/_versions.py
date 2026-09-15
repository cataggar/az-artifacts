"""Universal Package SemVer rules (not Python's PEP 440 version rules)."""

import re
from uuid import UUID

from . import _json
from ._http import Http, endpoint
from .errors import ProtocolError, VersionNotFoundError

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
    http: Http,
    feeds_url: str,
    project: str | None,
    feed: str,
    name: str,
    prefix: tuple[int, ...],
) -> str:
    segments = [project] if project is not None else []
    packages_url = endpoint(feeds_url, *segments, "_apis", "packaging", "Feeds", feed, "packages")
    skip = 0
    page_size = 100
    seen: set[tuple[str, ...]] = set()
    package_id = None
    while package_id is None:
        response = http.request(
            "GET",
            packages_url,
            params={
                "api-version": "7.1",
                "protocolType": "upack",
                "packageNameQuery": name,
                "includeDeleted": "false",
                "includeAllVersions": "false",
                "getTopPackageVersions": "false",
                "$top": str(page_size),
                "$skip": str(skip),
            },
        )
        obj = _json.as_object(response.json(), "packages")
        packages = [
            _json.as_object(value, "package")
            for value in _json.as_list(obj.get("value"), "packages")
        ]
        ids = tuple(_json.string(package.get("id"), "package ID") for package in packages)
        if ids and ids in seen:
            raise ProtocolError("Package listing pagination did not advance")
        seen.add(ids)
        for package in packages:
            package_name = _json.string(
                package.get("normalizedName", package.get("name")), "package name"
            )
            if package_name == name:
                try:
                    package_id = str(UUID(_json.string(package.get("id"), "package ID")))
                except ValueError:
                    raise ProtocolError("Package listing returned an invalid package ID") from None
                break
        if package_id is None:
            if len(packages) < page_size:
                raise VersionNotFoundError(f"Universal Package {name!r} was not found in the feed")
            skip += len(packages)

    # This endpoint returns all versions and has no documented pagination parameters.
    response = http.request(
        "GET",
        endpoint(packages_url, package_id, "versions"),
        params={"api-version": "7.1", "isDeleted": "false"},
    )
    obj = _json.as_object(response.json(), "package versions")
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for value in _json.as_list(obj.get("value"), "package versions"):
        entry = _json.as_object(value, "package version")
        if entry.get("isDeleted") is True:
            continue
        version = _json.string(entry.get("version"), "package version")
        if version_number(version) is None:
            raise ProtocolError("Package listing returned an invalid Universal Package version")
        number = version_number(version, stable_only=True)
        if number is not None and number[: len(prefix)] == prefix:
            candidates.append((number, version))
    if not candidates:
        pattern = ".".join((*map(str, prefix), "*"))
        raise VersionNotFoundError(f"No released version of {name!r} matches {pattern!r}")
    return max(candidates)[1]
