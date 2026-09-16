"""Typed catalog summaries, registration metadata, inspection, and download results."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

Scope = Literal["organization", "project"]


@dataclass(frozen=True)
class ProjectReference:
    """A feed's associated project.

    ``id`` is its GUID; ``name`` and ``visibility`` are optional service values.
    """

    id: str
    name: str | None = None
    visibility: str | None = None


@dataclass(frozen=True)
class Feed:
    """A feed accessible to the caller, not necessarily organization-scoped.

    ``id`` is its GUID and ``name`` its display name. ``project`` preserves the
    returned association (None when absent), independently of the query scope.
    ``description`` may be empty; ``deleted_date`` is an optional UTC timestamp.
    """

    id: str
    name: str
    project: ProjectReference | None = None
    description: str | None = None
    deleted_date: datetime | None = None


@dataclass(frozen=True)
class PackageVersion:
    """A Universal Package version summary, including prereleases.

    ``version`` is the display version; optional ``normalized_version`` is its
    package-type identity. ``id`` is the optional version GUID. ``is_deleted``
    and ``is_latest`` preserve the service flags, or None when unknown.
    ``publish_date`` and ``deleted_date`` are optional timezone-aware UTC dates.
    ``description`` and ``package_description`` preserve the distinct SDK fields
    (version description and package description); either may be empty.
    """

    version: str
    id: str | None = None
    normalized_version: str | None = None
    is_deleted: bool | None = None
    is_latest: bool | None = None
    publish_date: datetime | None = None
    deleted_date: datetime | None = None
    description: str | None = None
    package_description: str | None = None


@dataclass(frozen=True)
class Package:
    """A Universal Package container within the requested feed.

    ``id`` is its GUID and ``name`` its display name. ``normalized_name`` is the
    optional package-type identity; ``protocol_type`` preserves the optional
    service protocol string. ``versions`` contains only summaries supplied with
    this listing, in service order: None means omitted, not an empty catalog.
    Use list_package_versions() for version enumeration.
    """

    id: str
    name: str
    normalized_name: str | None = None
    protocol_type: str | None = None
    versions: tuple[PackageVersion, ...] | None = None


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
    add_package() validates their 64-hex-digit plus 01/02 shapes and serializes
    uppercase IDs. ``proof_nodes`` must be a tuple of opaque strings, not blob IDs;
    order, duplicates, empty strings and an empty tuple are preserved on the wire.
    ``description`` None is omitted; an empty string is sent unchanged.
    Validation occurs on registration, not construction. Content/proof generation
    and upload are caller preconditions, not operations provided by this model.
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
class PackageFile:
    """A logical file described by a package manifest.

    ``path`` is a case-sensitive, package-relative POSIX path, not a local path.
    ``size`` is the logical byte length. ``content_id`` is the uppercase dedup
    chunk/node ID, including its type suffix, NOT necessarily a flat file hash.
    """

    path: PurePosixPath
    size: int
    content_id: str


@dataclass(frozen=True)
class FileVersion:
    """An exact package ``version`` containing the path described by ``file``.

    Files have no independent version: this associates a path with its package
    version, without asserting that its content changed from another version.
    """

    version: str
    file: PackageFile


@dataclass(frozen=True)
class FileComparison:
    """Comparison of a local file with one exact package version's manifest.

    ``status`` is ``version_missing`` for catalog-established package/version
    absence, ``path_missing`` for an absent manifest path, or ``match`` /
    ``different`` for content agreement / disagreement with that manifest.
    ``metadata`` is None only for version_missing, otherwise the exact metadata.
    ``file`` is the manifest entry for match/different, otherwise None.
    A match verifies represented content, not remote payload availability.
    """

    status: Literal["version_missing", "path_missing", "match", "different"]
    metadata: PackageMetadata | None
    file: PackageFile | None


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
