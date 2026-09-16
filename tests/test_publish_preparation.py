"""Pure-Python preparation checked against Microsoft-generated, offline vectors."""

import base64
import hashlib
import io
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from az_artifacts import UnsafePathError
from az_artifacts._chunking import chunks
from az_artifacts._dedup import content_hash, parse_node, serialize_node
from az_artifacts._prepare import PreparedPackage
from az_artifacts.models import BlobRef

FIXTURES = Path(__file__).parent / "fixtures" / "publishing"
VECTORS = json.loads((FIXTURES / "sdk-chunk-vectors.json").read_text())


def seeded(seed, size):
    prefix = seed.encode() + b"\0"
    return b"".join(
        hashlib.sha256(prefix + i.to_bytes(8, "little")).digest() for i in range((size + 31) // 32)
    )[:size]


class ShortReads(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 997))


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda case: case["name"])
def test_chunking_and_roots_match_local_microsoft_sdk(vector, tmp_path):
    data = b"".join(
        bytes(part["length"])
        if part["kind"] == "zeros"
        else seeded(VECTORS["seed"], part["length"])
        for part in vector["recipe"]
    )
    assert hashlib.sha256(data).hexdigest() == vector["sha256"]
    expected = [(chunk["id"], chunk["size"]) for chunk in vector["chunks"]]
    for stream in (io.BytesIO(data), ShortReads(data)):
        result = [(content_hash(chunk) + "01", len(chunk)) for chunk in chunks(stream)]
        assert (result or [(content_hash(b"") + "01", 0)]) == expected
    (tmp_path / "sample.bin").write_bytes(data)
    prepared = PreparedPackage(tmp_path, "1.0.0", max_bytes=64 * 1024 * 1024)
    assert prepared.items[0].blob.id == vector["root"]
    prepared.verify_sources()


def captured_registration(name):
    for line in (FIXTURES / name).read_text().splitlines():
        row = json.loads(line)
        obj = (row.get("body") or {}).get("json", {})
        if isinstance(obj, dict) and "proofNodes" in obj:
            return obj
    raise AssertionError("Fixture has no registration")


def assert_capture_match(prepared, name, prefix):
    registration = captured_registration(name)
    assert prepared.manifest == (FIXTURES / (prefix + "reference-manifest.json")).read_bytes()
    assert prepared.metadata.manifest_id == registration["manifestId"]
    assert prepared.metadata.super_root_id == registration["superRootId"]
    assert prepared.proofs == tuple(
        base64.b64decode(proof["value"]) for proof in registration["proofNodes"]
    )


def test_small_preparation_matches_approved_reference():
    prepared = PreparedPackage(FIXTURES / "smoke", "1.0.0", max_bytes=1048576)
    assert_capture_match(prepared, "approved-publish.jsonl", "")
    assert prepared.metadata.package_size == 219


def test_100_mib_deep_tree_matches_actual_reference(tmp_path):
    seed = b"az-artifacts reference multi-chunk v1\0"
    with (tmp_path / "payload.bin").open("wb") as stream:
        for start in range(0, 3276800, 32768):
            stream.write(
                b"".join(
                    hashlib.sha256(seed + i.to_bytes(8, "little")).digest()
                    for i in range(start, start + 32768)
                )
            )
    (tmp_path / "empty.txt").touch()
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "repeated.bin").write_bytes(bytes(131072))
    prepared = PreparedPackage(tmp_path, "1.0.0", max_bytes=64 * 1024 * 1024)
    assert_capture_match(prepared, "next-approved-publish.jsonl", "next-")
    for line in (FIXTURES / "next-approved-publish.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row.get("phase") == "request_body" and "/dedup/nodes/" in row.get("route", ""):
            identifier = row["route"].rsplit("/", 1)[1]
            assert prepared.nodes[identifier].data == base64.b64decode(row["body"]["base64"])
    file_root = prepared.nodes[prepared.items[2].blob.id]
    assert len(file_root.children) == 342
    assert sum(len(node.children) == 512 for node in prepared.nodes.values()) == 2
    assert len(prepared._file_chunks[2][1]) == 1364
    assert prepared.metadata.package_size == 104989099
    prepared.verify_sources()


def test_manifest_order_duplicates_and_empty_directories(tmp_path):
    (tmp_path / "z").write_bytes(b"same")
    (tmp_path / "a").write_bytes(b"same")
    (tmp_path / "unused").mkdir()
    first = PreparedPackage(tmp_path, "1.0.0", max_bytes=1048576)
    second = PreparedPackage(tmp_path, "2.0.0", max_bytes=1048576)
    assert first.manifest == second.manifest
    assert [item.path for item in first.items] == ["/a", "/z"]
    assert first.items[0].blob == first.items[1].blob
    assert len(first.chunks) == 2


def test_empty_source_is_explicitly_rejected(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        PreparedPackage(tmp_path, "1.0.0", max_bytes=1048576)


@pytest.mark.parametrize("mutation", ["content", "added", "deleted", "size"])
def test_mutation_is_detected_before_registration(tmp_path, mutation):
    path = tmp_path / "file"
    path.write_bytes(b"original")
    prepared = PreparedPackage(tmp_path, "1.0.0", max_bytes=1048576)
    stamp = path.stat()
    if mutation == "content":
        path.write_bytes(b"modified")
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    elif mutation == "added":
        (tmp_path / "another").touch()
    elif mutation == "deleted":
        path.unlink()
    else:
        path.write_bytes(b"different size")
    with pytest.raises(OSError):
        prepared.verify_sources()


def test_chunk_reread_detects_content_change(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"original")
    prepared = PreparedPackage(tmp_path, "1.0.0", max_bytes=1048576)
    path.write_bytes(b"modified")
    with pytest.raises(OSError):
        prepared.chunks[prepared.items[0].blob.id].read()


def test_preparation_record_budget_is_bounded(tmp_path):
    (tmp_path / "file").write_bytes(bytes(131072))
    with pytest.raises(ValueError, match="budget"):
        PreparedPackage(tmp_path, "1.0.0", max_bytes=200)


def test_reparse_points_are_not_followed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda *args: SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400),
    )
    with pytest.raises(UnsafePathError, match="reparse"):
        PreparedPackage(tmp_path, "1.0.0", max_bytes=1048576)


def test_link_is_not_followed(tmp_path):
    (tmp_path / "original").write_bytes(b"file")
    try:
        (tmp_path / "link").symlink_to(tmp_path / "original")
    except OSError:
        pytest.skip("Creating links is not permitted on this machine")
    with pytest.raises(UnsafePathError, match="symlinks"):
        PreparedPackage(tmp_path, "1.0.0", max_bytes=1048576)


def test_node_serialization_roundtrip_and_limits():
    chunk = BlobRef(content_hash(b"abc") + "01", 3)
    node = BlobRef(content_hash(b"node") + "02", (1 << 32) + 5)
    assert parse_node(serialize_node((chunk, node))) == (chunk, node)
    for children in ([], [chunk] * 513, [BlobRef(chunk.id, 1 << 24)]):
        with pytest.raises(ValueError):
            serialize_node(children)
