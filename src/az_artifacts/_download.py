"""Bounded file assembly with per-file atomic replacement."""

import os
import tempfile
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import _manifest
from ._dedup import BlobReader
from ._http import Http
from ._paths import prepare_destination, select_files
from .errors import IntegrityError
from .models import BlobRef, DownloadResult, PackageFile, PackageMetadata


class Downloader:
    def __init__(
        self, http: Http, blob_url: str, *, max_workers: int, max_manifest_bytes: int
    ) -> None:
        self._reader = BlobReader(http, blob_url)
        self._max_workers = max_workers
        self._max_manifest_bytes = max_manifest_bytes

    def download(
        self,
        metadata: PackageMetadata,
        path: Path,
        *,
        file_filter: str | Sequence[str] | None,
        overwrite: bool,
    ) -> DownloadResult:
        files = _manifest.load(self._reader, metadata, max_bytes=self._max_manifest_bytes)
        selected = select_files(files, file_filter)
        root = path.resolve()
        root.mkdir(parents=True, exist_ok=True)
        for _, relative in selected:
            prepare_destination(root, relative, overwrite=overwrite)

        total = 0
        remaining = iter(selected)
        with (
            ThreadPoolExecutor(max_workers=self._max_workers) as chunks,
            ThreadPoolExecutor(max_workers=self._max_workers) as executor,
        ):
            pending = set()
            for item, relative in remaining:
                pending.add(
                    executor.submit(self._write_file, root, item, relative, overwrite, chunks)
                )
                if len(pending) == self._max_workers:
                    break
            try:
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        total += future.result()
                    for _ in done:
                        entry = next(remaining, None)
                        if entry is not None:
                            item, relative = entry
                            pending.add(
                                executor.submit(
                                    self._write_file, root, item, relative, overwrite, chunks
                                )
                            )
            finally:
                for future in pending:
                    future.cancel()
        return DownloadResult(metadata, root, tuple(relative for _, relative in selected), total)

    def _write_file(
        self,
        root: Path,
        item: PackageFile,
        relative: Path,
        overwrite: bool,
        chunks: ThreadPoolExecutor,
    ) -> int:
        destination = prepare_destination(root, relative, overwrite=overwrite)
        descriptor, name = tempfile.mkstemp(prefix=".az-artifacts-", dir=destination.parent)
        temporary = Path(name)
        count = 0
        try:
            with os.fdopen(descriptor, "wb") as stream:
                for chunk in self._reader.content(
                    BlobRef(item.content_id, item.size),
                    executor=chunks,
                    prefetch=min(self._max_workers, 4),
                ):
                    if count + len(chunk) > item.size:
                        raise IntegrityError("File content exceeds its advertised size")
                    stream.write(chunk)
                    count += len(chunk)
            if count != item.size:
                raise IntegrityError("File content does not match its advertised size")
            prepare_destination(root, relative, overwrite=overwrite)
            if overwrite:
                os.replace(temporary, destination)
            else:
                # An exclusive hard link keeps no-overwrite atomic as well.
                os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return count
