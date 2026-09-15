"""Strict decoding of the small subset of Azure DevOps models we consume."""

import re
from datetime import UTC, datetime
from uuid import UUID

from ._versions import validate_name, version_number
from .errors import ProtocolError
from .models import (
    BlobRef,
    Feed,
    LimitedPackageMetadata,
    LimitedPackageMetadataListResponse,
    ManifestItem,
    Package,
    PackageMetadata,
    PackagePushMetadata,
    PackageVersion,
    PackageVersionDeletionState,
    ProjectReference,
)

_BLOB_ID = re.compile(r"[0-9A-Fa-f]{64}(?:01|02)\Z")


def as_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProtocolError(f"Expected a JSON object for {label}")
    return value


def as_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ProtocolError(f"Expected a JSON array for {label}")
    return value


def string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        kind = "string" if allow_empty else "nonempty string"
        raise ProtocolError(f"Expected a {kind} for {label}")
    return value


def optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return string(value, label, allow_empty=True)


def optional_date(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    text = string(value, label)
    try:
        result = datetime.fromisoformat(text)
        if result.tzinfo is not None and result.utcoffset() is not None:
            return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ProtocolError(f"Expected a timezone-aware ISO 8601 datetime for {label}") from None
    raise ProtocolError(f"Expected a timezone-aware ISO 8601 datetime for {label}")


def size(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(f"Expected a nonnegative integer for {label}")
    return value


def guid(value: object, label: str) -> str:
    text = string(value, label)
    try:
        return str(UUID(text))
    except ValueError:
        raise ProtocolError(f"Expected a GUID for {label}") from None


def optional_boolean(value: object, label: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise ProtocolError(f"Expected a boolean for {label}")
    return value


def _optional_identity(value: object, label: str) -> str | None:
    if value is None:
        return None
    return string(value, label)


def _package_name(value: str) -> None:
    try:
        validate_name(value)
    except ValueError:
        raise ProtocolError("Package listing returned an invalid Universal Package name") from None


def _package_version(value: object) -> str:
    text = string(value, "package version")
    if version_number(text) is None:
        raise ProtocolError("Package listing returned an invalid Universal Package version")
    return text


def project_reference(value: object) -> ProjectReference:
    obj = as_object(value, "project reference")
    return ProjectReference(
        id=guid(obj.get("id"), "project ID"),
        name=_optional_identity(obj.get("name"), "project name"),
        visibility=optional_string(obj.get("visibility"), "project visibility"),
    )


def feed(value: object) -> Feed:
    obj = as_object(value, "feed")
    project = obj.get("project")
    return Feed(
        id=guid(obj.get("id"), "feed ID"),
        name=string(obj.get("name"), "feed name"),
        project=project_reference(project) if project is not None else None,
        description=optional_string(obj.get("description"), "feed description"),
        deleted_date=optional_date(obj.get("deletedDate"), "feed deletion date"),
    )


def package_version(value: object) -> PackageVersion:
    obj = as_object(value, "package version")
    version_id = obj.get("id")
    normalized = obj.get("normalizedVersion")
    return PackageVersion(
        version=_package_version(obj.get("version")),
        id=guid(version_id, "package version ID") if version_id is not None else None,
        normalized_version=_package_version(normalized) if normalized is not None else None,
        is_deleted=optional_boolean(obj.get("isDeleted"), "version deletion state"),
        is_latest=optional_boolean(obj.get("isLatest"), "latest version state"),
        publish_date=optional_date(obj.get("publishDate"), "version publish date"),
        deleted_date=optional_date(obj.get("deletedDate"), "version deletion date"),
        description=optional_string(obj.get("description"), "version description"),
        package_description=optional_string(obj.get("packageDescription"), "package description"),
    )


def package(value: object) -> Package:
    obj = as_object(value, "package")
    name = string(obj.get("name"), "package name")
    normalized = _optional_identity(obj.get("normalizedName"), "normalized package name")
    _package_name(normalized if normalized is not None else name)
    protocol = _optional_identity(obj.get("protocolType"), "package protocol")
    if protocol is not None and protocol.lower() != "upack":
        raise ProtocolError("Package listing returned a non-Universal Package protocol")
    versions = obj.get("versions")
    return Package(
        id=guid(obj.get("id"), "package ID"),
        name=name,
        normalized_name=normalized,
        protocol_type=protocol,
        versions=(
            tuple(package_version(entry) for entry in as_list(versions, "package versions"))
            if versions is not None
            else None
        ),
    )


def blob_id(value: object) -> str:
    result = string(value, "blob ID")
    if not _BLOB_ID.fullmatch(result):
        raise ProtocolError("Unsupported or invalid dedup blob ID")
    return result.upper()


def package_metadata(value: object) -> PackageMetadata:
    obj = as_object(value, "package metadata")
    return PackageMetadata(
        version=string(obj.get("version"), "package version"),
        manifest_id=blob_id(obj.get("manifestId")),
        super_root_id=blob_id(obj.get("superRootId")),
        package_size=size(obj.get("packageSize"), "package size"),
        description=optional_string(obj.get("description"), "package description"),
    )


def limited_package_metadata(value: object) -> LimitedPackageMetadata:
    obj = as_object(value, "limited package metadata")
    return LimitedPackageMetadata(
        version=string(obj.get("version"), "package version"),
        description=optional_string(obj.get("description"), "package description"),
    )


def limited_package_metadata_list_response(value: object) -> LimitedPackageMetadataListResponse:
    obj = as_object(value, "limited package metadata list")
    for field in ("continuationToken", "nextLink", "@odata.nextLink"):
        if optional_string(obj.get(field), field):
            raise ProtocolError("Limited package metadata returned unsupported continuation")
    return LimitedPackageMetadataListResponse(
        count=size(obj.get("count"), "limited package metadata count"),
        value=tuple(
            limited_package_metadata(entry)
            for entry in as_list(obj.get("value"), "limited package metadata entries")
        ),
    )


def package_push_metadata(value: object) -> PackagePushMetadata:
    obj = as_object(value, "package push metadata")
    return PackagePushMetadata(
        manifest_id=blob_id(obj.get("manifestId")),
        super_root_id=blob_id(obj.get("superRootId")),
        proof_nodes=tuple(
            string(entry, "proof node", allow_empty=True)
            for entry in as_list(obj.get("proofNodes"), "proof nodes")
        ),
        description=optional_string(obj.get("description"), "package description"),
    )


def package_version_deletion_state(value: object) -> PackageVersionDeletionState:
    obj = as_object(value, "package version deletion state")
    return PackageVersionDeletionState(
        name=string(obj.get("name"), "package name"),
        version=string(obj.get("version"), "package version"),
        deleted_date=optional_date(obj.get("deletedDate"), "package deletion date"),
    )


def manifest(value: object) -> tuple[ManifestItem, ...]:
    obj = as_object(value, "manifest")
    result = []
    for entry in as_list(obj.get("items"), "manifest items"):
        item = as_object(entry, "manifest item")
        blob = as_object(item.get("blob"), "manifest blob")
        result.append(
            ManifestItem(
                path=string(item.get("path"), "manifest path"),
                blob=BlobRef(blob_id(blob.get("id")), size(blob.get("size"), "blob size")),
            )
        )
    return tuple(result)
