import pytest
from conftest import identifier, node_bytes

from az_artifacts._dedup import MAX_NODE_BYTES, content_hash, decode_blob, parse_node
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
