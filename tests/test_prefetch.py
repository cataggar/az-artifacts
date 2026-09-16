"""Bounded and ordered chunk fetching for large-file interoperability checks."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from az_artifacts._dedup import BlobReader
from az_artifacts.errors import IntegrityError
from az_artifacts.models import BlobRef


@pytest.mark.parametrize("fail", [False, True])
def test_chunk_prefetch_is_bounded_ordered_and_propagates_failure(monkeypatch, fail):
    reader = object.__new__(BlobReader)
    refs = [BlobRef(f"{index:064X}01", 1) for index in range(20)]
    active = maximum = fetched = 0
    lock = threading.Lock()

    def leaves(*args, **kwargs):
        yield from refs

    def blob(identifier, **kwargs):
        nonlocal active, maximum, fetched
        index = int(identifier[:64], 16)
        with lock:
            active += 1
            fetched += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.001 * (3 - index % 3))
            if fail and index == 1:
                raise IntegrityError("synthetic corruption")
            return bytes([index])
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(reader, "_leaf_refs", leaves)
    monkeypatch.setattr(reader, "blob", blob)
    with ThreadPoolExecutor(max_workers=4) as executor:
        stream = reader.content(refs[0], executor=executor, prefetch=3)
        if fail:
            with pytest.raises(IntegrityError):
                list(stream)
        else:
            assert b"".join(stream) == bytes(range(20))
    assert maximum <= 3
    assert fetched <= 4 if fail else fetched == 20
