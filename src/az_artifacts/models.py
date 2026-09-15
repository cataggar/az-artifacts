"""Typed package metadata and download results."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Scope = Literal["organization", "project"]


@dataclass(frozen=True)
class PackageMetadata:
    version: str
    manifest_id: str
    super_root_id: str
    package_size: int


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
