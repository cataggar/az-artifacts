"""Bounded, validated manifest reads shared by inspection and downloads."""

import json

from . import _json
from ._dedup import BlobReader
from ._paths import manifest_files
from .errors import ProtocolError
from .models import PackageFile, PackageMetadata


def load(
    reader: BlobReader, metadata: PackageMetadata, *, max_bytes: int
) -> tuple[PackageFile, ...]:
    data = reader.manifest(metadata.manifest_id, limit=max_bytes)
    try:
        value: object = json.loads(data)
    except (ValueError, UnicodeError):
        raise ProtocolError("Package manifest is not valid JSON") from None
    return manifest_files(_json.manifest(value))
