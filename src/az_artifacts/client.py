# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Universal Package discovery, metadata, inspection, download, and publishing client."""

import math
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from types import TracebackType
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

import httpx

from . import _catalog, _inspection, _json, _manifest
from ._dedup import BlobReader
from ._download import Downloader
from ._http import Http, endpoint, validate_url
from ._paths import filter_files, package_path, validate_file_filter
from ._prepare import PreparedPackage
from ._publish import publish
from ._versions import resolve_version, validate_name, version_number, version_pattern
from .auth import BearerToken, Credential, _validate_token
from .errors import (
    PackageNotFoundError,
    ProtocolError,
    RegistrationOutcomeUnknownError,
    ServiceError,
    TransportError,
)
from .models import (
    DownloadResult,
    Feed,
    FileComparison,
    FileVersion,
    LimitedPackageMetadataListResponse,
    Package,
    PackageFile,
    PackageMetadata,
    PackagePushMetadata,
    PackageVersion,
    PublishRequest,
    PublishResult,
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
    """Discover, inspect, download, register, and publish Universal Packages.

    Pass a PAT string, a :class:`BearerToken`, or an Azure ``TokenCredential``.
    Use a context manager or call :meth:`close` to release HTTP connections.
    No Azure CLI or ArtifactTool dependency, including for content uploading.
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
        self._upload_url: str | None = None
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
                area_id = area.get("id")
                if area_id is not None:
                    area_id = _json.string(area_id, "resource area ID").lower()
                # Unrelated resource areas can advertise ports this client never uses.
                if name in (
                    "dedup",
                    "packaging",
                    "packagingapi",
                    "upackpackaging",
                    "feed",
                ) or area_id in (_UPACK_RESOURCE_AREA, _FEED_RESOURCE_AREA):
                    validate_url(location, authenticated=True)
                    if urlsplit(location).query:
                        raise ProtocolError(
                            "Resource area location must not contain a query string"
                        )
                services[name] = location.rstrip("/")
                if area_id is not None:
                    service_ids[area_id] = location.rstrip("/")
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
            or services.get("packagingapi")
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

    def _blob_url(self) -> str:
        blob_url = self.discover_services().get("dedup")
        if blob_url is None:
            raise ProtocolError("ResourceAreas did not advertise a dedup service")
        return blob_url

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
        responses raise ProtocolError. The versionless route has been verified
        against independent live REST baselines in both feed scopes, using names
        and IDs. Unsupported or incomplete service responses still fail explicitly.
        """
        self._validate_package(feed, name)
        _validate_scope(scope, project)
        response = self._http.request("GET", self._upack_url(feed, name, project))
        if response.status == 206 or response.headers.get("x-ms-continuationtoken"):
            raise ProtocolError("Limited package metadata returned a partial or continued response")
        return _json.limited_package_metadata_list_response(response.json())

    def add_package(
        self,
        *,
        feed: str,
        name: str,
        version: str,
        metadata: PackagePushMetadata,
        scope: Scope = "organization",
        project: str | None = None,
    ) -> None:
        """Register pre-uploaded content; this is NOT a file-upload/publish workflow.

        ``feed`` is a feed name or ID; ``name`` an exact Universal Package name.
        ``version`` must be exact SemVer (prereleases allowed, no wildcards).
        ``scope`` defaults to organization; project scope requires a ``project``
        name or ID, which must otherwise be omitted. Requires an open client and
        Packaging write/publish permission, not merely read access.

        ``metadata`` must be PackagePushMetadata referencing existing uploaded
        content and suitable proofs. Manifest/super-root IDs must be supported
        64-hex-digit IDs with 01/02 suffixes; serialization canonicalizes them to
        uppercase. Proofs must be a tuple of opaque strings: order, duplicates,
        empty strings and an empty tuple are preserved, not proof-validated.
        Description None is omitted; "" is sent unchanged. Invalid caller data
        raises ValueError/TypeError before any request; inputs are never mutated.
        No Dedup discovery, blob access, local I/O, proof generation, or overwrite.

        Returns None only for HTTP 200/201/204 with no asynchronous/partial
        response headers (Azure-AsyncOperation, Operation-Location, Content-Range,
        x-ms-continuationtoken). The bounded body is ignored, as in the SDK.
        The PUT is never automatically retried, regardless of client retries.
        HTTP 409 raises ConflictError; other 4xx except 408 retain ServiceError
        types, including 401/403/404/429. HTTP 408, 5xx, other unacknowledged
        responses, and transport/response-protocol failures after starting the
        PUT raise RegistrationOutcomeUnknownError: the version may have committed.
        Discovery failures propagate normally before registration is attempted.

        Versions are immutable/reserved even after deletion. No automatic
        reconciliation occurs: explicitly verify intended metadata after an
        unknown outcome, and never interpret a later 409 as success. Callers and
        custom transports must not replay the PUT automatically either.
        Acknowledgment and conflict behavior have been exercised live for a
        project-scoped feed; organization-scoped registration remains unverified.
        """
        self._validate_package(feed, name)
        _validate_scope(scope, project)
        if version_pattern(version) is not None:
            raise ValueError("Registration requires an exact Universal Package version")
        body = _json.serialize_package_push_metadata(metadata)
        url = self._upack_url(feed, name, project, version)
        validate_url(url, authenticated=True)
        try:
            response = self._http.request("PUT", url, json_body=body, retry=False)
        except ServiceError as error:
            if 400 <= error.status_code < 500 and error.status_code != 408:
                raise
            raise RegistrationOutcomeUnknownError(
                "Package registration was not acknowledged; its outcome is unknown",
                status_code=error.status_code,
                request_id=error.request_id,
            ) from error
        except (TransportError, ProtocolError) as error:
            raise RegistrationOutcomeUnknownError(
                "Package registration could not be confirmed; its outcome is unknown"
            ) from error
        if response.status not in (200, 201, 204) or any(
            response.headers.get(header)
            for header in (
                "azure-asyncoperation",
                "operation-location",
                "content-range",
                "x-ms-continuationtoken",
            )
        ):
            raise RegistrationOutcomeUnknownError(
                "Package registration did not acknowledge synchronous completion",
                status_code=response.status,
                request_id=response.request_id,
            )

    def _package_files(
        self, feed: str, name: str, version: str, project: str | None
    ) -> tuple[PackageFile, ...]:
        return self._exact_manifest(feed, name, version, project)[1]

    def _exact_manifest(
        self, feed: str, name: str, version: str, project: str | None
    ) -> tuple[PackageMetadata, tuple[PackageFile, ...], BlobReader]:
        reader = BlobReader(self._http, self._blob_url())
        metadata = self._get_package_metadata(feed, name, version, project, intent=None)
        files = _manifest.load(reader, metadata, max_bytes=self._max_manifest_bytes)
        return metadata, files, reader

    def _find_file(
        self,
        feed: str,
        name: str,
        version: str,
        project: str | None,
        path: PurePosixPath,
    ) -> PackageFile | None:
        files = self._package_files(feed, name, version, project)
        return next((file for file in files if file.path == path), None)

    def list_files(
        self,
        *,
        feed: str,
        name: str,
        version: str,
        file_filter: str | Sequence[str] | None = None,
        scope: Scope = "organization",
        project: str | None = None,
    ) -> tuple[PackageFile, ...]:
        """Read exact-version manifest files in order, without payloads or local I/O.

        ``feed``, ``name``, ``scope`` and ``project`` follow download(); ``version``
        must be exact, including prereleases. ``file_filter`` uses download's
        ordered POSIX globs, but no matches returns (). All inputs are validated
        before requests. Requires an open client and an advertised Dedup service.
        Paths are case-sensitive even on Windows; host-only destination
        restrictions do not apply. Metadata/manifest/blob failures propagate.
        """
        self._validate_package(feed, name)
        _validate_scope(scope, project)
        if version_pattern(version) is not None:
            raise ValueError("Inspection requires an exact Universal Package version")
        patterns = validate_file_filter(file_filter)
        return filter_files(self._package_files(feed, name, version, project), patterns)

    def file_exists(
        self,
        *,
        feed: str,
        name: str,
        version: str,
        relative_path: str | PurePosixPath,
        scope: Scope = "organization",
        project: str | None = None,
    ) -> bool:
        """Check an exact, case-sensitive logical path without payloads or local I/O.

        Common arguments follow list_files(). ``relative_path`` is a nonempty
        package-relative POSIX string or PurePosixPath, never a local Path.
        No leading slash, drive prefix, backslash, control, empty, dot or parent
        component is accepted; PurePosixPath's already-normalized value is used.
        False means catalog-established package/version absence or a missing
        manifest path. No negative caching; absence does not permit publishing.
        HTTP 404 (including metadata after catalog success), authentication,
        transport and malformed-response errors always propagate.
        """
        self._validate_package(feed, name)
        _validate_scope(scope, project)
        if version_pattern(version) is not None:
            raise ValueError("Inspection requires an exact Universal Package version")
        path = package_path(relative_path)
        if not self.package_version_exists(
            feed=feed, name=name, version=version, scope=scope, project=project
        ):
            return False
        return self._find_file(feed, name, version, project, path) is not None

    def compare_file(
        self,
        *,
        feed: str,
        name: str,
        version: str,
        relative_path: str | PurePosixPath,
        local_path: str | Path,
        scope: Scope = "organization",
        project: str | None = None,
    ) -> FileComparison:
        """Compare a local regular file with an exact version's represented content.

        Common arguments and the literal, portable ``relative_path`` follow
        file_exists(). ``version`` must be exact, including prereleases.
        ``local_path`` is a nonempty local string or Path; symlinks are followed.
        All arguments are validated, then the local file is opened read-only
        BEFORE catalog access, even when the version/path is absent.

        Returns frozen FileComparison with status version_missing, path_missing,
        match or different and available metadata/file. No negative caching.
        Fetches catalog, exact metadata, manifest and traversed dedup nodes, never
        file payloads or their URLs. Local bytes are hashed at remote boundaries.
        A size/hash mismatch can stop early: different is not a remote health
        audit, and match does not prove payload availability/upload provenance.

        Do not mutate the source concurrently. Identity, size and timestamp
        checks detect observable changes (LocalFileChangedError), not an atomic
        snapshot. Nonregular files raise ValueError; other local I/O and all
        remote/protocol failures propagate. Requires an open client.
        """
        self._validate_package(feed, name)
        _validate_scope(scope, project)
        if version_pattern(version) is not None:
            raise ValueError("Comparison requires an exact Universal Package version")
        path = package_path(relative_path)
        source = _inspection.local_path(local_path)
        with _inspection.open_local(source) as (stream, size):
            if not self.package_version_exists(
                feed=feed, name=name, version=version, scope=scope, project=project
            ):
                return FileComparison("version_missing", None, None)
            metadata, files, reader = self._exact_manifest(feed, name, version, project)
            file = next((entry for entry in files if entry.path == path), None)
            if file is None:
                return FileComparison("path_missing", metadata, None)
            return FileComparison(
                "match" if _inspection.matches(stream, size, file, reader) else "different",
                metadata,
                file,
            )

    def list_file_versions(
        self,
        *,
        feed: str,
        name: str,
        relative_path: str | PurePosixPath,
        versions: Sequence[str] | None = None,
        scope: Scope = "organization",
        project: str | None = None,
    ) -> Iterator[FileVersion]:
        """Lazily scan one package's versions for a path; never fetch file payloads.

        Common arguments and path rules follow file_exists(). None enumerates
        visible nondeleted catalog versions, including prereleases, in service
        order; an absent package raises PackageNotFoundError. An explicit
        sequence is copied/validated eagerly and scanned in caller order; empty
        means no requests. Wildcards and a bare version string are not accepted.
        Explicit missing versions and versions disappearing mid-scan raise their
        metadata errors, never silently skip an HTTP 404.

        Network work starts on iteration; advancing requires an open client.
        Only path-present records are yielded, without change detection or
        sorting. Holds the bounded version catalog/explicit sequence and one
        bounded manifest at a time, not all manifests/history results. No local
        I/O, global feed scans, concurrency, or snapshot guarantees.
        """
        self._validate_package(feed, name)
        _validate_scope(scope, project)
        path = package_path(relative_path)
        selected: tuple[str, ...] | None = None
        if versions is not None:
            if isinstance(versions, (str, bytes, bytearray)) or not isinstance(versions, Sequence):
                raise ValueError("versions must be a sequence of exact version strings or None")
            selected = tuple(versions)
            for version in selected:
                if not isinstance(version, str) or version_pattern(version) is not None:
                    raise ValueError("History requires exact Universal Package versions")

        def iterate() -> Iterator[FileVersion]:
            self._ensure_open()
            names: Iterator[str]
            if selected is None:
                catalog = self.list_package_versions(
                    feed=feed, name=name, scope=scope, project=project
                )
                names = (entry.normalized_version or entry.version for entry in catalog)
            else:
                names = iter(selected)
            for version in names:
                self._ensure_open()
                file = self._find_file(feed, name, version, project, path)
                if file is not None:
                    self._ensure_open()
                    yield FileVersion(version, file)
                    self._ensure_open()

        return iterate()

    def _upload_location(self, blob_url: str) -> str:
        if self._upload_url is None:
            response = self._http.request(
                "GET",
                endpoint(self.organization, "_apis", "connectionData"),
                params={"connectOptions": "0"},
            )
            obj = _json.as_object(response.json(), "organization connection data")
            try:
                identifier = str(
                    UUID(_json.string(obj.get("instanceId"), "organization instance ID"))
                )
            except ValueError:
                raise ProtocolError("Invalid organization instance ID") from None
            account = "A" + identifier
            self._upload_url = (
                blob_url
                if blob_url.lower().endswith("/" + account.lower())
                else endpoint(blob_url, account)
            )
        return self._upload_url

    def publish(self, request: PublishRequest) -> PublishResult:
        """Publish an immutable exact version from a quiescent regular-file tree.

        No registration is attempted until all content and retention are complete.
        A registration request is never automatically repeated.
        """
        self._ensure_open()
        if not isinstance(request, PublishRequest):
            raise TypeError("publish requires a PublishRequest")
        if not isinstance(request.name, str) or not isinstance(request.version, str):
            raise ValueError("Package name and version must be strings")
        self._validate_package(request.feed, request.name)
        number = version_number(request.version)
        if number is None or any(component > 2147483647 for component in number):
            raise ValueError("Publishing requires exact lowercase SemVer with 32-bit components")
        _validate_scope(request.scope, request.project)
        if request.description is not None and not isinstance(request.description, str):
            raise ValueError("description must be a string or None")
        prepared = PreparedPackage(
            Path(request.path), request.version, max_bytes=self._max_manifest_bytes
        )
        package_url = self._upack_url(request.feed, request.name, request.project, request.version)
        blob_url = self._upload_location(self._blob_url())
        return publish(
            self._http,
            package_url,
            blob_url,
            request,
            prepared,
            max_workers=self._max_workers,
        )

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
        file_filter = validate_file_filter(file_filter)
        blob_url = self._blob_url()
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
