# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bounded, read-only package preparation and BuildXL-compatible packed trees."""

import errno
import io
import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ._chunking import chunks
from ._dedup import content_hash, serialize_node
from ._paths import relative_path
from .errors import UnsafePathError
from .models import BlobRef, ManifestItem, PackageMetadata


def _changed() -> OSError:
    return OSError(errno.ESTALE, "Publishing source changed; keep the directory quiescent")


@dataclass(frozen=True)
class Stamp:
    device: int
    inode: int
    size: int
    modified: int
    changed: int

    @classmethod
    def read(cls, value: os.stat_result) -> "Stamp":
        return cls(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    def matches_handle(self, value: os.stat_result) -> bool:
        # Windows path stat and fstat can disagree about the meaning of st_ctime.
        return (self.device, self.inode, self.size, self.modified) == (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
        )


def _stat(path: Path) -> os.stat_result:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400:
        raise UnsafePathError("Publishing does not support symlinks or reparse points")
    return value


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative: str
    stamp: Stamp

    def check(self) -> None:
        if Stamp.read(_stat(self.path)) != self.stamp:
            raise _changed()


@dataclass(frozen=True)
class Chunk:
    ref: BlobRef
    offset: int
    source: SourceFile | None = None
    data: bytes | None = None

    def read(self) -> bytes:
        if self.data is not None:
            return self.data
        if self.source is None:
            raise AssertionError("Chunk has neither bytes nor a source")
        self.source.check()
        with self.source.path.open("rb") as stream:
            if not self.source.stamp.matches_handle(os.fstat(stream.fileno())):
                raise _changed()
            stream.seek(self.offset)
            data = stream.read(self.ref.size)
        self.source.check()
        if len(data) != self.ref.size or content_hash(data) + "01" != self.ref.id:
            raise _changed()
        return data


@dataclass(frozen=True)
class Node:
    ref: BlobRef
    children: tuple[BlobRef, ...]
    data: bytes


class PreparedPackage:
    def __init__(self, path: Path, version: str, *, max_bytes: int) -> None:
        if not stat.S_ISDIR(_stat(path).st_mode):
            raise NotADirectoryError(path)
        self.path = path.resolve()
        self._budget = max_bytes
        self._limit = max_bytes
        self.sources = self._inventory()
        if not self.sources:
            raise ValueError("Publishing requires at least one regular file")
        self.chunks: dict[str, Chunk] = {}
        self.nodes: dict[str, Node] = {}
        self._file_chunks: list[tuple[SourceFile, tuple[BlobRef, ...]]] = []
        items: list[ManifestItem] = []
        for source in self.sources:
            refs: list[BlobRef] = []
            offset = 0
            source.check()
            with source.path.open("rb") as stream:
                if not source.stamp.matches_handle(os.fstat(stream.fileno())):
                    raise _changed()
                for data in chunks(stream):
                    self._charge(512)
                    ref = BlobRef(content_hash(data) + "01", len(data))
                    self.chunks.setdefault(ref.id, Chunk(ref, offset, source))
                    refs.append(ref)
                    offset += len(data)
            source.check()
            if offset != source.stamp.size:
                raise _changed()
            if not refs:
                ref = BlobRef(content_hash(b"") + "01", 0)
                self.chunks.setdefault(ref.id, Chunk(ref, 0, data=b""))
                refs.append(ref)
            self._file_chunks.append((source, tuple(refs)))
            items.append(ManifestItem("/" + source.relative, self.tree(refs)))
        self.items = tuple(items)
        proofs: list[bytes] = []
        self.content_root = self.tree([item.blob for item in items], force_node=True, proofs=proofs)
        self.manifest = json.dumps(
            {
                "manifestFormat": "1.1.0",
                "items": [
                    {"path": item.path, "blob": {"id": item.blob.id, "size": item.blob.size}}
                    for item in items
                ],
                "manifestReferences": [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(self.manifest) > max_bytes:
            raise ValueError("Publishing manifest exceeds max_manifest_bytes")
        self._charge(len(self.manifest))
        manifest_refs = []
        for data in chunks(io.BytesIO(self.manifest)):
            self._charge(512 + len(data))
            ref = BlobRef(content_hash(data) + "01", len(data))
            self.chunks.setdefault(ref.id, Chunk(ref, 0, data=data))
            manifest_refs.append(ref)
        manifest_root = self.tree(manifest_refs)
        self.super_root = self.node((self.content_root, manifest_root), proofs=proofs)
        self.proofs = tuple(dict.fromkeys(proofs))
        self.metadata = PackageMetadata(
            version,
            manifest_root.id,
            self.super_root.id,
            sum(item.blob.size for item in items) + len(self.manifest),
        )

    def _charge(self, amount: int) -> None:
        self._budget -= amount
        if self._budget < 0:
            raise ValueError("Publishing preparation exceeds the max_manifest_bytes record budget")

    def _inventory(self) -> tuple[SourceFile, ...]:
        pending = [self.path]
        files = []
        seen: set[str] = set()
        budget = self._limit
        while pending:
            directory = pending.pop()
            if not stat.S_ISDIR(_stat(directory).st_mode):
                raise _changed()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    relative = path.relative_to(self.path).as_posix()
                    relative_path(relative, portable=True)
                    if len(path.relative_to(self.path).parts) > 256:
                        raise UnsafePathError("Publishing paths exceed the 256-component limit")
                    key = unicodedata.normalize("NFC", relative).casefold()
                    if key in seen:
                        raise UnsafePathError(
                            "Publishing paths collide on a case-insensitive filesystem"
                        )
                    seen.add(key)
                    budget -= 1024 + 4 * len(relative)
                    if budget < 0:
                        raise ValueError(
                            "Publishing inventory exceeds max_manifest_bytes record budget"
                        )
                    value = _stat(path)
                    if stat.S_ISDIR(value.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(value.st_mode):
                        files.append(SourceFile(path, relative, Stamp.read(value)))
                    else:
                        raise UnsafePathError(
                            "Publishing supports only regular files and directories"
                        )
        return tuple(sorted(files, key=lambda source: source.relative.encode("utf-16-be")))

    def node(self, children: tuple[BlobRef, ...], *, proofs: list[bytes] | None = None) -> BlobRef:
        data = serialize_node(children)
        ref = BlobRef(content_hash(data) + "02", sum(child.size for child in children))
        if ref.id not in self.nodes:
            self._charge(512 + len(data) * 3)
            self.nodes[ref.id] = Node(ref, children, data)
        if proofs is not None:
            proofs.append(data)
        return ref

    def tree(
        self,
        children: list[BlobRef],
        *,
        force_node: bool = False,
        proofs: list[bytes] | None = None,
    ) -> BlobRef:
        if len(children) == 1 and not force_node:
            return children[0]
        while len(children) > 512:
            complete = len(children) // 512 * 512
            parents = [
                self.node(tuple(children[offset : offset + 512]), proofs=proofs)
                for offset in range(0, complete, 512)
            ]
            children = parents + children[complete:]
        return self.node(tuple(children), proofs=proofs)

    def verify_sources(self) -> None:
        """Recheck the inventory and every original chunk before registration."""
        if self._inventory() != self.sources:
            raise _changed()
        for source, refs in self._file_chunks:
            source.check()
            with source.path.open("rb") as stream:
                if not source.stamp.matches_handle(os.fstat(stream.fileno())):
                    raise _changed()
                for ref in refs:
                    data = stream.read(ref.size)
                    if len(data) != ref.size or content_hash(data) + "01" != ref.id:
                        raise _changed()
                if stream.read(1):
                    raise _changed()
            source.check()
        if self._inventory() != self.sources:
            raise _changed()
