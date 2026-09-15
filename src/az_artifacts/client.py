# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Universal Package discovery, download, and read-only metadata client."""

import math
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType
from urllib.parse import quote, unquote, urlsplit

import httpx

from . import _catalog, _json
from ._download import Downloader
from ._http import Http, endpoint, validate_url
from ._versions import resolve_version, validate_name, version_pattern
from .auth import BearerToken, Credential, _validate_token
from .errors import PackageNotFoundError, ProtocolError
from .models import (
    DownloadResult,
    Feed,
    LimitedPackageMetadataListResponse,
    Package,
    PackageMetadata,
    PackageVersion,
    Scope,
)

_UPACK_RESOURCE_AREA = "d397749b-f115-4027-b6dd-77a65dd10d21"
_FEED_RESOURCE_AREA = "7ab4e64e-c4d8-4f50-ae73-5ef2e21642a5"


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or value in (".", ".."):
        raise ValueError(f"{label} must be a nonempty name or ID")


def _validate_scope(scope: Scope, project: str | None) -> None:
    if scope not in ("organization", "project"):
        raise ValueError("scope must be 'organization' or 'project'")
    if scope == "project":
        if project is None:
            raise ValueError("project is required for project-scoped feeds")
        _identifier(project, "project")
    elif project is not None:
        raise ValueError("project requires scope='project'")


def _organization_url(organization: str) -> tuple[str, str]:
    _identifier(organization, "organization")
    if "://" not in organization:
        organization = "https://dev.azure.com/" + quote(organization, safe="")
    validate_url(organization, authenticated=True)
    parsed = urlsplit(organization)
    if parsed.query:
        raise ValueError("Organization URL must not contain a query string")
    parts = parsed.path.strip("/").split("/")
    if parsed.hostname == "dev.azure.com" and len(parts) == 1 and parts[0]:
        name = unquote(parts[0], errors="strict")
        _identifier(name, "organization")
        if any(char in name for char in "/\\?#"):
            raise ValueError("Organization URL must identify a single organization")
        return "https://dev.azure.com/" + quote(name, safe=""), name
    if parsed.hostname and parsed.hostname.endswith(".visualstudio.com"):
        name = parsed.hostname.removesuffix(".visualstudio.com")
        if name and "." not in name and parsed.path in ("", "/", "/DefaultCollection"):
            return organization.rstrip("/"), name
    raise ValueError("Use https://dev.azure.com/ORG or https://ORG.visualstudio.com")


class UniversalPackageClient:
    """Discover, read metadata, and download packages without Azure CLI or ArtifactTool.

    Pass a PAT string, a :class:`BearerToken`, or an Azure ``TokenCredential``.
    Use a context manager or call :meth:`close` to release HTTP connections.
    """

    def __init__(
        self,
        organization: str,
        *,
        credential: Credential,
        timeout: float = 60.0,
        retries: int = 3,
        max_workers: int = 4,
        max_manifest_bytes: int = 64 * 1024 * 1024,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.organization, self._organization_name = _organization_url(organization)
        if isinstance(credential, str):
            _validate_token(credential)
        elif not isinstance(credential, BearerToken) and not callable(
            getattr(credential, "get_token", None)
        ):
            raise TypeError("credential must be a PAT, BearerToken, or TokenCredential")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        for label, value, minimum in (
            ("retries", retries, 0),
            ("max_workers", max_workers, 1),
            ("max_manifest_bytes", max_manifest_bytes, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{label} must be an integer >= {minimum}")
        self._http = Http(credential, timeout=timeout, retries=retries, transport=transport)
        self._max_workers = max_workers
        self._max_manifest_bytes = max_manifest_bytes
        self._services: dict[str, str] | None = None
        self._service_ids: dict[str, str] = {}
        self._closed = False

    def __enter__(self) -> "UniversalPackageClient":
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the client's connections; does not close a supplied credential."""
        if not self._closed:
            self._http.close()
            self._closed = True

    def discover_services(self) -> dict[str, str]:
        """Discover and cache this organization's Azure DevOps service locations."""
        self._ensure_open()
        if self._services is None:
            response = self._http.request(
                "GET", endpoint(self.organization, "_apis", "ResourceAreas")
            )
            obj = _json.as_object(response.json(), "resource areas")
            services = {}
            service_ids = {}
            for value in _json.as_list(obj.get("value"), "resource areas"):
                area = _json.as_object(value, "resource area")
                name = _json.string(area.get("name"), "resource area name").lower()
                location = _json.string(area.get("locationUrl"), "resource area location")
                validate_url(location)
                if urlsplit(location).query:
                    raise ProtocolError("Resource area location must not contain a query string")
                services[name] = location.rstrip("/")
                area_id = area.get("id")
                if area_id is not None:
                    service_ids[_json.string(area_id, "resource area ID").lower()] = (
                        location.rstrip("/")
                    )
            self._services = services
            self._service_ids = service_ids
        return dict(self._services)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Client is closed")

    def _validate_package(self, feed: str, name: str) -> None:
        self._ensure_open()
        _identifier(feed, "feed")
        validate_name(name)

    def _packaging_url(self) -> str:
        services = self.discover_services()
        return (
            self._service_ids.get(_UPACK_RESOURCE_AREA)
            or services.get("upackpackaging")
            or services.get("packaging")
            or endpoint("https://pkgs.dev.azure.com", self._organization_name)
        )

    def _feeds_url(self) -> str:
        services = self.discover_services()
        return (
            self._service_ids.get(_FEED_RESOURCE_AREA)
            or services.get("feed")
            or endpoint("https://feeds.dev.azure.com", self._organization_name)
        )

    def list_feeds(self, *, project: str | None = None) -> tuple[Feed, ...]:
        """List all accessible organization feeds, optionally filtered by project.

        ``project`` is an optional project name or ID, not the feed's inferred
        scope: each returned Feed.project preserves the service association.
        Returns a tuple in service order. Requires an open client, not Dedup;
        no blobs or files are accessed. Service/protocol failures propagate.
        """
        self._ensure_open()
        if project is not None:
            _identifier(project, "project")
        return _catalog.list_feeds(self._http, self._feeds_url(), project)

    def list_packages(
        self,
        *,
        feed: str,
        name_query: str | None = None,
        page_size: int = 100,
        scope: Scope = "organization",
        project: str | None = None,
    ) -> Iterator[Package]:
        """Lazily list visible Universal Packages using bounded $top/$skip pages.

        ``feed`` is a feed name or ID; ``name_query`` is an optional nonempty
        substring filter, NOT an exact identity. ``page_size`` is a positive
        int32 (not bool). Project scope requires a ``project`` name or ID;
        otherwise omit it. Arguments are validated immediately; network work
        begins on iteration. Advancing an unexhausted iterator requires an open
        client, even for buffered entries. No Dedup, blobs, or filesystem access.

        Memory is bounded per page. Repeated/cyclic pages and unsupported partial
        responses raise ProtocolError; service errors propagate. Concurrent
        catalog changes can cause omissions/duplicates: this is not a snapshot.
        """
        self._ensure_open()
        _identifier(feed, "feed")
        _validate_scope(scope, project)
        if name_query is not None and (not isinstance(name_query, str) or not name_query):
            raise ValueError("name_query must be a nonempty string or None")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 2_147_483_647
        ):
            raise ValueError("page_size must be a positive int32")

        def iterate() -> Iterator[Package]:
            self._ensure_open()
            yield from _catalog.list_packages(
                self._http,
                self._feeds_url(),
                project,
                feed,
                name_query=name_query,
                page_size=page_size,
                include_deleted=False,
                ensure_open=self._ensure_open,
            )

        return iterate()

    def _catalog_versions(
        self, feed: str, name: str, project: str | None, *, include_deleted: bool = False
    ) -> tuple[PackageVersion, ...] | None:
        base = self._feeds_url()
        package = _catalog.find_package(
            self._http,
            base,
            project,
            feed,
            name,
            include_deleted=include_deleted,
            ensure_open=self._ensure_open,
        )
        if package is None:
            return None
        self._ensure_open()
        return _catalog.list_versions(
            self._http, base, project, feed, package.id, include_deleted=include_deleted
        )

    def list_package_versions(
        self,
        *,
        feed: str,
        name: str,
        include_deleted: bool = False,
        scope: Scope = "organization",
        project: str | None = None,
    ) -> tuple[PackageVersion, ...]:
        """Enumerate exact-name catalog versions, including prereleases, in service order.

        ``feed`` is a feed name or ID and ``name`` an exact Universal Package
        name. ``include_deleted`` must be bool; true includes both live/deleted
        records by omitting isDeleted. Project scope requires ``project``;
        otherwise omit it. A successful lookup establishing absence raises
        PackageNotFoundError. HTTP 404 and other service/protocol errors propagate.
        Requires an open client, not Dedup; no blobs or files are accessed.
        """
        self._validate_package(feed, name)
        _validate_scope(scope, project)
        if not isinstance(include_deleted, bool):
            raise ValueError("include_deleted must be a boolean")
        versions = self._catalog_versions(feed, name, project, include_deleted=include_deleted)
        if versions is None:
            raise PackageNotFoundError(f"Universal Package {name!r} was not found in the feed")
        return versions

    def package_version_exists(
        self,
        *,
        feed: str,
        name: str,
        version: str,
        scope: Scope = "organization",
        project: str | None = None,
    ) -> bool:
        """Check for a visible live exact version without payload or filesystem access.

        ``feed`` is a feed name or ID; ``name`` is an exact package name and
        ``version`` an exact Universal Package SemVer (prereleases allowed).
        Project scope requires ``project``; otherwise omit it. Requires an open
        client, not Dedup. False means successful catalog reads established
        absence under current permissions, never an HTTP/transport/protocol error.
        Results are not cached. Absence does not guarantee publishability:
        deleted versions remain reserved, and concurrent publishers can race.
        """
        self._validate_package(feed, name)
        _validate_scope(scope, project)
        if version_pattern(version) is not None:
            raise ValueError("Existence checks require an exact Universal Package version")
        versions = self._catalog_versions(feed, name, project)
        return versions is not None and any(
            (entry.normalized_version or entry.version) == version for entry in versions
        )

    def _upack_url(
        self, feed: str, name: str, project: str | None, version: str | None = None
    ) -> str:
        segments = [project] if project is not None else []
        # The SDK shares one location and omits only packageVersion for the list GET.
        segments.extend(["_packaging", feed, "upack", "packages", name, "versions"])
        if version is not None:
            segments.append(version)
        return endpoint(self._packaging_url(), *segments)

    def _get_package_metadata(
        self, feed: str, name: str, version: str, project: str | None, intent: str | None
    ) -> PackageMetadata:
        response = self._http.request(
            "GET",
            self._upack_url(feed, name, project, version),
            params={"intent": intent} if intent is not None else None,
        )
        metadata = _json.package_metadata(response.json())
        if metadata.version != version:
            raise ProtocolError("Returned package version does not match the requested version")
        return metadata

    def get_package_metadata(
        self,
        *,
        feed: str,
        name: str,
        version: str,
        intent: str | None = None,
        scope: Scope = "organization",
        project: str | None = None,
    ) -> PackageMetadata:
        """Read exact-version metadata without retrieving blobs or writing files.

        ``feed`` is a feed name or ID; ``name`` is the Universal Package name.
        ``version`` must be an exact version (prereleases allowed, no wildcards).
        ``intent`` is omitted when None, otherwise sent as a nonempty string.
        ``scope`` defaults to organization; project scope requires a ``project``
        name or ID, which must otherwise be omitted. Requires an open client,
        but not a Dedup service. Service and malformed-response errors propagate.
        """
        self._validate_package(feed, name)
        if version_pattern(version) is not None:
            raise ValueError("Metadata requires an exact Universal Package version")
        _validate_scope(scope, project)
        if intent is not None and (not isinstance(intent, str) or not intent):
            raise ValueError("intent must be a nonempty string or None")
        return self._get_package_metadata(feed, name, version, project, intent)

    def get_package_versions_metadata(
        self,
        *,
        feed: str,
        name: str,
        scope: Scope = "organization",
        project: str | None = None,
    ) -> LimitedPackageMetadataListResponse:
        """Read limited version/description entries and the unmodified server count.

        ``feed`` is a feed name or ID; ``name`` is the Universal Package name.
        ``scope`` defaults to organization; project scope requires a ``project``
        name or ID, which must otherwise be omitted. Entries retain service order,
        including prereleases. Requires an open client, but not a Dedup service;
        no blobs or files are read or written. Unsupported continuation/partial
        responses raise ProtocolError. The versionless route is experimental and
        has not been verified against a live service.
        """
        self._validate_package(feed, name)
        _validate_scope(scope, project)
        response = self._http.request("GET", self._upack_url(feed, name, project))
        if response.status == 206 or response.headers.get("x-ms-continuationtoken"):
            raise ProtocolError("Limited package metadata returned a partial or continued response")
        return _json.limited_package_metadata_list_response(response.json())

    def download(
        self,
        *,
        feed: str,
        name: str,
        version: str,
        path: str | Path,
        scope: Scope = "organization",
        project: str | None = None,
        file_filter: str | Sequence[str] | None = None,
        overwrite: bool = True,
    ) -> DownloadResult:
        """Download selected package files, replacing completed files atomically.

        ``version`` accepts exact SemVer or ``*``, ``1.*``, ``1.2.*`` patterns.
        Filters use package-relative POSIX paths; sequences are processed in order.
        Files already completed are retained if another file fails.
        """
        self._validate_package(feed, name)
        prefix = version_pattern(version)
        _validate_scope(scope, project)
        if not isinstance(overwrite, bool):
            raise ValueError("overwrite must be a boolean")
        services = self.discover_services()
        blob_url = services.get("dedup")
        if blob_url is None:
            raise ProtocolError("ResourceAreas did not advertise a dedup service")
        if prefix is not None:
            version = resolve_version(
                name,
                prefix,
                self._catalog_versions(feed, name, project),
            )
        metadata = self._get_package_metadata(feed, name, version, project, intent="Download")
        downloader = Downloader(
            self._http,
            blob_url,
            max_workers=self._max_workers,
            max_manifest_bytes=self._max_manifest_bytes,
        )
        return downloader.download(
            metadata, Path(path), file_filter=file_filter, overwrite=overwrite
        )
