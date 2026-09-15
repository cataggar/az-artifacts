# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Universal Package download client, based on azure-devops-rust-api."""

import math
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from urllib.parse import quote, unquote, urlsplit

import httpx

from . import _json
from ._download import Downloader
from ._http import Http, endpoint, validate_url
from ._versions import resolve_version, validate_name, version_pattern
from .auth import BearerToken, Credential, _validate_token
from .errors import ProtocolError
from .models import DownloadResult, Scope


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or value in (".", ".."):
        raise ValueError(f"{label} must be a nonempty name or ID")


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
    """Download Universal Packages without Azure CLI or ArtifactTool.

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
        self._closed = False

    def __enter__(self) -> "UniversalPackageClient":
        if self._closed:
            raise RuntimeError("Client is closed")
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
        if self._closed:
            raise RuntimeError("Client is closed")
        if self._services is None:
            response = self._http.request(
                "GET", endpoint(self.organization, "_apis", "ResourceAreas")
            )
            obj = _json.as_object(response.json(), "resource areas")
            services = {}
            for value in _json.as_list(obj.get("value"), "resource areas"):
                area = _json.as_object(value, "resource area")
                name = _json.string(area.get("name"), "resource area name").lower()
                location = _json.string(area.get("locationUrl"), "resource area location")
                validate_url(location)
                if urlsplit(location).query:
                    raise ProtocolError("Resource area location must not contain a query string")
                services[name] = location.rstrip("/")
            self._services = services
        return dict(self._services)

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
        if self._closed:
            raise RuntimeError("Client is closed")
        _identifier(feed, "feed")
        validate_name(name)
        prefix = version_pattern(version)
        if scope not in ("organization", "project"):
            raise ValueError("scope must be 'organization' or 'project'")
        if scope == "project":
            if project is None:
                raise ValueError("project is required for project-scoped feeds")
            _identifier(project, "project")
        elif project is not None:
            raise ValueError("project requires scope='project'")
        if not isinstance(overwrite, bool):
            raise ValueError("overwrite must be a boolean")
        services = self.discover_services()
        packages_url = services.get(
            "packaging", endpoint("https://pkgs.dev.azure.com", self._organization_name)
        )
        blob_url = services.get("dedup")
        if blob_url is None:
            raise ProtocolError("ResourceAreas did not advertise a dedup service")
        if prefix is not None:
            version = resolve_version(
                self._http,
                endpoint("https://feeds.dev.azure.com", self._organization_name),
                project,
                feed,
                name,
                prefix,
            )
        segments = [project] if project is not None else []
        segments.extend(["_packaging", feed, "upack", "packages", name, "versions", version])
        response = self._http.request(
            "GET", endpoint(packages_url, *segments), params={"intent": "Download"}
        )
        metadata = _json.package_metadata(response.json())
        if metadata.version != version:
            raise ProtocolError("Returned package version does not match the requested version")
        downloader = Downloader(
            self._http,
            blob_url,
            max_workers=self._max_workers,
            max_manifest_bytes=self._max_manifest_bytes,
        )
        return downloader.download(
            metadata, Path(path), file_filter=file_filter, overwrite=overwrite
        )
