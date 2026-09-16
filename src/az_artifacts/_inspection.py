"""Read-only local streaming against validated remote dedup boundaries."""

import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from ._dedup import BlobReader
from .errors import LocalFileChangedError
from .models import BlobRef, PackageFile

_READ_BYTES = 64 * 1024


def local_path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise TypeError("local_path must be a string or Path")
    if not str(value) or "\0" in str(value):
        raise ValueError("local_path must be nonempty and contain no NUL characters")
    return Path(value)


def _fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _unchanged(before: os.stat_result, after: os.stat_result) -> None:
    if _fingerprint(before) != _fingerprint(after):
        raise LocalFileChangedError("Local source changed or was replaced during comparison")


def _nonblocking_open(path: str, flags: int) -> int:
    # The pre-open stat rejects special files; O_NONBLOCK also prevents a raced
    # replacement with a POSIX FIFO from blocking before the descriptor check.
    if os.name != "nt":
        flags |= os.O_NONBLOCK
    return os.open(path, flags)


@contextmanager
def open_local(path: Path) -> Iterator[tuple[BinaryIO, int]]:
    """Follow symlinks, require a regular file, and guard every returned status.

    This is not a snapshot. Read/stat errors propagate; on remote failure we
    preserve that failure instead of masking it with a subsequent local check.
    """
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("local_path must refer to a regular file")
    with open(path, "rb", buffering=0, opener=_nonblocking_open) as stream:
        opened = os.fstat(stream.fileno())
        # Windows stat/fstat can expose different ctime meanings. Compare that
        # field only within each API, retaining both independent baselines.
        if _fingerprint(before)[:-1] != _fingerprint(opened)[:-1]:
            raise LocalFileChangedError("Local source changed or was replaced while opening")
        yield stream, before.st_size
        _unchanged(opened, os.fstat(stream.fileno()))
        try:
            after = path.stat()
        except FileNotFoundError as error:
            raise LocalFileChangedError("Local source disappeared during comparison") from error
        _unchanged(before, after)


def matches(stream: BinaryIO, size: int, file: PackageFile, reader: BlobReader) -> bool:
    refs = reader.leaf_refs(BlobRef(file.content_id, file.size))
    if size != file.size:
        return False
    for ref in refs:
        remaining = ref.size
        digest = hashlib.sha512()
        while remaining:
            data = stream.read(min(remaining, _READ_BYTES))
            if not data:
                raise LocalFileChangedError("Local source ended before its observed size")
            remaining -= len(data)
            digest.update(data)
        if digest.digest()[:32].hex().upper() != ref.id[:64]:
            return False
    if stream.read(1):
        raise LocalFileChangedError("Local source exceeds its observed size")
    return True
