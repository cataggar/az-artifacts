"""Typed package metadata and download results."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

Scope = Literal["organization", "project"]


@dataclass(frozen=True)
class PackageMetadata:
    """Exact-version metadata.

    ``version`` is the package version; ``manifest_id`` and ``super_root_id``
    identify its dedup manifest and super-root. ``package_size`` is the advertised
    whole-package size in bytes. ``description`` is optional and may be empty.
    """

    version: str
    manifest_id: str
    super_root_id: str
    package_size: int
    description: str | None = None


@dataclass(frozen=True)
class LimitedPackageMetadata:
    """A package ``version`` and its optional, possibly empty ``description``."""

    version: str
    description: str | None = None


@dataclass(frozen=True)
class LimitedPackageMetadataListResponse:
    """Limited metadata returned by the service.

    ``count`` preserves the server's count, not a computed length or pagination
    total. ``value`` is an immutable tuple of entries in service order.
    """

    count: int
    value: tuple[LimitedPackageMetadata, ...]


@dataclass(frozen=True)
class PackagePushMetadata:
    """Registration data only; constructing this model does not publish anything.

    ``manifest_id`` and ``super_root_id`` identify pre-uploaded dedup content.
    ``proof_nodes`` is an immutable tuple of opaque proof strings, not blob IDs.
    ``description`` is optional and may be empty.
    """

    manifest_id: str
    super_root_id: str
    proof_nodes: tuple[str, ...]
    description: str | None = None


@dataclass(frozen=True)
class PackageVersionDeletionState:
    """Deletion data only; no deletion operation is provided.

    ``name`` and ``version`` identify the package version. ``deleted_date`` is
    its optional deletion timestamp, represented as a timezone-aware UTC datetime.
    """

    name: str
    version: str
    deleted_date: datetime | None = None


@dataclass(frozen=True)
class DownloadResult:
    metadata: PackageMetadata
    path: Path
    files: tuple[Path, ...]
    bytes_downloaded: int


@dataclass(frozen=True)
class BlobRef:
    id: str
    size: int


@dataclass(frozen=True)
class ManifestItem:
    path: str
    blob: BlobRef
