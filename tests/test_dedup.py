import pytest
from conftest import identifier, node_bytes

from az_artifacts._dedup import (
    MAX_CHUNK_BYTES,
    MAX_NODE_BYTES,
    MAX_TREE_DEPTH,
    BlobReader,
    content_hash,
    decode_blob,
    parse_node,
)
from az_artifacts.errors import IntegrityError, ProtocolError
from az_artifacts.models import BlobRef


def test_chunk_hash_vector():
    assert content_hash(b"abc") == (
        "DDAF35A193617ABACC417349AE20413112E6FA4E89A97EA20A9EEEE64B55D39A"
    )


def test_mixed_typed_children():
    leaf = BlobRef("ab" * 32 + "01", 123)
    node = BlobRef("cd" * 32 + "02", (1 << 40) + 7)
    parsed = parse_node(node_bytes([leaf, node, leaf]))
    assert len(node_bytes([leaf, node, leaf])) == 4 + 36 + 40 + 36
    assert parsed == (
        BlobRef(leaf.id.upper(), leaf.size),
        BlobRef(node.id.upper(), node.size),
        BlobRef(leaf.id.upper(), leaf.size),
    )


def test_maximum_node_children():
    children = [BlobRef("ab" * 32 + "02", 1)] * 512
    data = node_bytes(children)
    assert len(data) == MAX_NODE_BYTES
    assert len(parse_node(data)) == 512


@pytest.mark.parametrize(
    "data",
    [
        b"",
        bytes(3),
        b"\x01\x00\x00\x00",
        b"\x00\x00\x00\x02",
        b"\x00\x00\x00\x00\x02",
        b"\x00\x00\x00\x00\x00",
        b"\x00\x00\x00\x00\x01" + bytes(35),
        b"\x00\x00\x00\x00\x00" + bytes(36),
    ],
)
def test_invalid_node(data):
    with pytest.raises(ProtocolError):
        parse_node(data)


def test_raw_and_compressed_are_identified_by_hash():
    ref = identifier(b"AAAA")
    assert decode_blob(b"AAAA", ref, size=4, limit=100) == b"AAAA"
    assert decode_blob(b"\x00\x00\x00\x40A\x00\x00", ref, size=4, limit=100) == b"AAAA"


def test_raw_size_mismatch_is_not_ignored():
    with pytest.raises(IntegrityError):
        decode_blob(b"AAAA", identifier(b"AAAA"), size=5, limit=100)


def test_compressed_hash_mismatch():
    with pytest.raises(IntegrityError, match="hash"):
        decode_blob(b"\x00\x00\x00\x40A", identifier(b"B"), size=1, limit=100)


def test_raw_hash_mismatch():
    with pytest.raises(IntegrityError):
        decode_blob(b"corrupted", identifier(b"expected"), size=8, limit=100)


def test_oversized_advertised_chunk():
    with pytest.raises(ProtocolError, match="limit"):
        decode_blob(b"A", identifier(b"A"), size=101, limit=100)


def test_leaf_traversal_is_lazy_ordered_and_preserves_repeated_refs(client, service):
    a, b = service.chunk(b"A"), service.chunk(b"B")
    first = service.node([b, a, b])
    second = service.node([a, a])
    root = service.node([first, second, first])
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    refs = reader.leaf_refs(root)
    assert not service.requests
    assert next(refs) == b
    assert set(service.resolve_counts) == {root.id, first.id}
    assert list(refs) == [a, b, a, a, b, a, b]
    assert set(service.resolve_counts) == {root.id, first.id, second.id}
    assert {a.id, b.id}.isdisjoint(service.resolve_counts)


@pytest.mark.parametrize(
    "ref",
    [
        BlobRef("00" * 32 + "03", 1),
        BlobRef("invalid", 1),
        BlobRef(identifier(b"a"), -1),
        BlobRef(identifier(b"a"), True),
        BlobRef(identifier(b"a"), MAX_CHUNK_BYTES + 1),
        BlobRef(identifier(b""), 1),
    ],
)
def test_leaf_root_validation_is_eager_without_requests(client, service, ref):
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    with pytest.raises(ProtocolError):
        reader.leaf_refs(ref)
    assert not service.requests


@pytest.mark.parametrize("empty", [False, True])
def test_direct_leaf_never_resolves_a_url(client, service, empty):
    leaf = service.chunk(b"" if empty else b"data")
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    assert list(reader.leaf_refs(leaf)) == [leaf]
    assert not service.requests


@pytest.mark.parametrize("operation", ["leaf_refs", "content"])
def test_node_aggregate_validated_before_yielding_any_child(client, service, operation):
    leaf = service.chunk(b"data")
    root = service.node([leaf, leaf])
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    refs = getattr(reader, operation)(BlobRef(root.id, root.size - 1))
    with pytest.raises(IntegrityError, match="logical size"):
        next(refs)
    assert leaf.id not in service.resolve_counts


@pytest.mark.parametrize("operation", ["leaf_refs", "content"])
def test_cycle_detection_without_constructing_a_hash_fixed_point(
    client, service, monkeypatch, operation
):
    root = BlobRef("AB" * 32 + "02", 1)
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    calls = []

    def cyclic_node(identifier, *, size):
        calls.append(identifier)
        return (root,)

    # Mock only the validated-node boundary: producing a real hash cycle would
    # require finding a cryptographic fixed point.
    monkeypatch.setattr(reader, "_node", cyclic_node)
    with pytest.raises(ProtocolError, match="cyclic"):
        list(getattr(reader, operation)(root))
    assert calls == [root.id]


@pytest.mark.parametrize("levels", [MAX_TREE_DEPTH - 1, MAX_TREE_DEPTH])
def test_leaf_depth_boundary(client, service, levels):
    leaf = service.chunk(b"a")
    root = leaf
    for _ in range(levels):
        root = service.node([root])
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    if levels == MAX_TREE_DEPTH:
        with pytest.raises(ProtocolError, match="depth"):
            list(reader.leaf_refs(root))
    else:
        assert list(reader.leaf_refs(root)) == [leaf]
    assert leaf.id not in service.resolve_counts


def test_download_content_still_prefetches_all_children(client, service):
    a, b = service.chunk(b"a"), service.chunk(b"b")
    root = service.node([a, b])
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    chunks = reader.content(root)
    assert next(chunks) == b"a"
    assert set(service.resolve_counts) == {root.id, a.id, b.id}
    assert list(chunks) == [b"b"]


@pytest.mark.parametrize("failure", ["unsupported-child", "empty-size", "trailing"])
def test_whole_node_is_validated_before_first_yield(client, service, failure):
    leaf = service.chunk(b"data")
    if failure == "empty-size":
        data = node_bytes([leaf, BlobRef(identifier(b""), 1)])
    else:
        data = node_bytes([leaf, leaf])
        if failure == "trailing":
            data += b"\0"
        else:
            data = data[:40] + b"\x02" + data[41:]
    root = BlobRef(identifier(data, "02"), 5 if failure == "empty-size" else 8)
    service.blobs[root.id] = data
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    with pytest.raises(ProtocolError):
        next(reader.leaf_refs(root))
    assert leaf.id not in service.resolve_counts
