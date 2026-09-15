"""Strict decoding of the small subset of Azure DevOps models we consume."""

import re

from .errors import ProtocolError
from .models import BlobRef, ManifestItem, PackageMetadata

_BLOB_ID = re.compile(r"[0-9A-Fa-f]{64}(?:01|02)\Z")


def as_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProtocolError(f"Expected a JSON object for {label}")
    return value


def as_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ProtocolError(f"Expected a JSON array for {label}")
    return value


def string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"Expected a nonempty string for {label}")
    return value


def size(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(f"Expected a nonnegative integer for {label}")
    return value


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
