import pytest

from az_artifacts._decompress import decompress_chunk
from az_artifacts.errors import IntegrityError, ProtocolError


@pytest.mark.parametrize("data", [b"", bytes(4)])
def test_too_small(data):
    with pytest.raises(ProtocolError):
        decompress_chunk(data)


@pytest.mark.parametrize(
    ("indicator", "literal"),
    [(0x40000000, b"H"), (0x20000000, b"Hi"), (0x10000000, b"Hey"), (0x08000000, b"Test")],
)
def test_literal_lengths(indicator, literal):
    assert decompress_chunk(indicator.to_bytes(4, "little") + literal) == literal


def test_literal_then_overlapping_match():
    assert decompress_chunk(b"\x00\x00\x00\x40A\x00\x00") == b"AAAA"


def test_match_outside_history():
    with pytest.raises(ProtocolError, match="history"):
        decompress_chunk(b"\x00\x00\x00\x80\x08\x00")


@pytest.mark.parametrize(
    ("extension", "length"),
    [
        (b"\x00", 10),
        (b"\x0e", 24),
        (b"\x0f\x00", 25),
        (b"\x0f\xff\x16\x00", 25),
        (b"\x0f\xff\x00\x00\x16\x00\x00\x00", 25),
    ],
)
def test_extended_matches(extension, length):
    compressed = b"\x00\x00\x00\x40A\x07\x00" + extension
    assert decompress_chunk(compressed, expected_size=length + 1) == b"A" * (length + 1)


def test_shared_nibble():
    compressed = b"\x00\x00\x00\x60A\x07\x00\x10\x07\x00"
    assert decompress_chunk(compressed) == b"A" * 22


@pytest.mark.parametrize("extension", [b"", b"\x0f", b"\x0f\xff", b"\x0f\xff\x15\x00"])
def test_invalid_extended_match(extension):
    with pytest.raises(ProtocolError):
        decompress_chunk(b"\x00\x00\x00\x40A\x07\x00" + extension)


def test_truncated_literal():
    with pytest.raises(ProtocolError, match="Truncated"):
        decompress_chunk(b"\x00\x00\x00\x08A")


def test_size_mismatch():
    with pytest.raises(IntegrityError):
        decompress_chunk(b"\x00\x00\x00\x40A", expected_size=2)


def test_bounded_output():
    with pytest.raises(IntegrityError, match="limit"):
        decompress_chunk(b"\x00\x00\x00\x40A\x00\x00", max_output_size=3)
