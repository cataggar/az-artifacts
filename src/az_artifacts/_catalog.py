"""Shared read-only Feed API I/O; no transfer metadata, blobs, or filesystem access."""

from collections.abc import Callable, Iterator

from . import _json
from ._http import Http, endpoint
from .errors import ProtocolError
from .models import Feed, Package, PackageVersion


def _values(
    http: Http,
    url: str,
    params: dict[str, str],
    label: str,
    *,
    max_bytes: int = 16 * 1024 * 1024,
) -> list[object]:
    response = http.request(
        "GET", url, params=params, headers={"Accept": "application/json; api-version=7.1"},
        max_bytes=max_bytes,
    )
    if (
        response.status != 200
        or response.headers.get("x-ms-continuationtoken")
        or response.headers.get("content-range")
        or response.headers.get("link")
    ):
        raise ProtocolError(f"{label} returned an unsupported partial or continued response")
    obj = _json.as_object(response.json(), label)
    for field in ("continuationToken", "nextLink", "@odata.nextLink"):
        if _json.optional_string(obj.get(field), field):
            raise ProtocolError(f"{label} returned unsupported continuation")
    values = _json.as_list(obj.get("value"), label)
    if "count" in obj and _json.size(obj["count"], f"{label} count") != len(values):
        raise ProtocolError(f"{label} count does not match the returned page")
    return values


def _feeds_url(base: str, project: str | None) -> str:
    segments = [project] if project is not None else []
    return endpoint(base, *segments, "_apis", "packaging", "Feeds")


def list_feeds(
    http: Http, base: str, project: str | None, *, max_bytes: int
) -> tuple[Feed, ...]:
    # This endpoint has no documented paging parameters.
    # URL resolution and deleted upstreams are not part of the public Feed model.
    params = {"api-version": "7.1", "includeUrls": "false", "includeDeletedUpstreams": "false"}
    return tuple(
        _json.feed(entry)
        for entry in _values(
            http, _feeds_url(base, project), params, "Feeds", max_bytes=max_bytes
        )
    )


def list_packages(
    http: Http,
    base: str,
    project: str | None,
    feed: str,
    *,
    name_query: str | None,
    page_size: int,
    include_deleted: bool,
    ensure_open: Callable[[], None],
) -> Iterator[Package]:
    url = endpoint(_feeds_url(base, project), feed, "packages")
    params = {
        "api-version": "7.1",
        "protocolType": "upack",
        "includeDeleted": str(include_deleted).lower(),
        "includeAllVersions": "false",
        "getTopPackageVersions": "false",
        "$top": str(page_size),
    }
    if name_query is not None:
        params["packageNameQuery"] = name_query
    skip = 0
    previous: tuple[str, ...] = ()
    checkpoint: tuple[str, ...] = ()
    distance, span = 0, 1
    while True:
        ensure_open()
        # FeedClient's $skip is an int32; never wrap or silently truncate a catalog.
        if skip > 2_147_483_647:
            raise ProtocolError("Package listing exceeded the supported pagination offset")
        params["$skip"] = str(skip)
        packages = tuple(_json.package(entry) for entry in _values(http, url, params, "Packages"))
        if len(packages) > page_size:
            raise ProtocolError("Package listing exceeded the requested page size")
        ids = tuple(package.id for package in packages)
        if len(set(ids)) != len(ids):
            raise ProtocolError("Package listing returned duplicate package IDs within a page")
        if ids:
            if ids == previous or ids == checkpoint:
                raise ProtocolError("Package listing pagination did not advance")
            # Brent-style cycle detection keeps two page signatures, not the whole catalog.
            distance += 1
            if distance == span:
                checkpoint, distance, span = ids, 0, span * 2
            previous = ids
        for package in packages:
            ensure_open()
            yield package
        ensure_open()
        if len(packages) < page_size:
            return
        skip += len(packages)


def find_package(
    http: Http,
    base: str,
    project: str | None,
    feed: str,
    name: str,
    *,
    include_deleted: bool,
    ensure_open: Callable[[], None],
) -> Package | None:
    match = None
    for package in list_packages(
        http,
        base,
        project,
        feed,
        name_query=name,
        page_size=100,
        include_deleted=include_deleted,
        ensure_open=ensure_open,
    ):
        if (package.normalized_name or package.name) == name:
            if match is not None and match.id != package.id:
                raise ProtocolError("Package listing returned ambiguous exact package names")
            match = package
    return match


def list_versions(
    http: Http,
    base: str,
    project: str | None,
    feed: str,
    package_id: str,
    *,
    include_deleted: bool,
) -> tuple[PackageVersion, ...]:
    # No documented pagination; isDeleted=true would request ONLY deleted versions.
    params = {"api-version": "7.1"}
    if not include_deleted:
        params["isDeleted"] = "false"
    url = endpoint(_feeds_url(base, project), feed, "packages", package_id, "versions")
    versions = tuple(
        _json.package_version(entry) for entry in _values(http, url, params, "Package versions")
    )
    return tuple(
        version for version in versions if include_deleted or version.is_deleted is not True
    )
