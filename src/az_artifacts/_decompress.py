# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""The blob store LZ77 decoder, ported from azure-devops-rust-api."""

from .errors import IntegrityError, ProtocolError


def _shift(value: int, *, sentinel: bool = False) -> int:
    value = ((value << 1) + int(sentinel)) & 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


class _Decoder:
    def __init__(self, data: bytes, limit: int) -> None:
        self.data = data
        self.pos = 0
        self.output = bytearray()
        self.limit = limit
        self.nibble_pos: int | None = None

    def number(self, count: int) -> int:
        if self.pos + count > len(self.data):
            raise ProtocolError("Truncated compressed chunk")
        value = int.from_bytes(self.data[self.pos : self.pos + count], "little")
        self.pos += count
        return value

    def check_size(self, count: int) -> None:
        if len(self.output) + count > self.limit:
            raise IntegrityError("Decompressed chunk exceeds its size limit")

    def literal(self, count: int) -> None:
        self.check_size(count)
        if self.pos + count > len(self.data):
            raise ProtocolError("Truncated literal in compressed chunk")
        self.output.extend(self.data[self.pos : self.pos + count])
        self.pos += count

    def match(self) -> None:
        value = self.number(2)
        length = value & 7
        offset = (value >> 3) + 1
        if length == 7:
            if self.nibble_pos is None:
                self.nibble_pos = self.pos
                length = self.number(1) & 0x0F
            else:
                length = self.data[self.nibble_pos] >> 4
                self.nibble_pos = None
            if length == 15:
                length = self.number(1)
                if length == 255:
                    length = self.number(2)
                    if length == 0:
                        length = self.number(4)
                    if length < 22:
                        raise ProtocolError("Invalid extended match length")
                    length -= 22
                length += 15
            length += 7
        length += 3
        if offset > len(self.output):
            raise ProtocolError("Compressed match refers outside the output history")
        self.check_size(length)
        pattern = self.output[-offset:]
        repeats, remainder = divmod(length, offset)
        self.output.extend(pattern * repeats)
        self.output.extend(pattern[:remainder])

    def decode(self) -> bytes:
        raw = self.number(4)
        indicator = _shift(raw, sentinel=True)
        fresh_literal = raw < 0x80000000
        if not fresh_literal:
            if self.pos + 1 >= len(self.data):
                return bytes(self.output)
            self.match()
        while self.pos < len(self.data):
            # Fresh literal indicators already consumed their decision bit.
            if fresh_literal:
                fresh_literal = False
            elif indicator >= 0:
                indicator = _shift(indicator)
            else:
                indicator = _shift(indicator)
                if indicator == 0:
                    if self.pos + 3 >= len(self.data):
                        break
                    raw = self.number(4)
                    indicator = _shift(raw, sentinel=True)
                    if raw < 0x80000000:
                        fresh_literal = True
                        continue
                if self.pos + 1 >= len(self.data):
                    break
                self.match()
                continue

            while True:
                if indicator < 0:
                    self.literal(1)
                    break
                indicator = _shift(indicator)
                if indicator < 0:
                    self.literal(2)
                    break
                indicator = _shift(indicator)
                if indicator < 0:
                    self.literal(3)
                    break
                indicator = _shift(indicator)
                self.literal(4)
                if indicator < 0:
                    break
                indicator = _shift(indicator)

            indicator = _shift(indicator)
            if indicator == 0:
                if self.pos + 3 >= len(self.data):
                    break
                raw = self.number(4)
                indicator = _shift(raw, sentinel=True)
                if raw < 0x80000000:
                    fresh_literal = True
                    continue
            if self.pos + 1 >= len(self.data):
                break
            self.match()
        return bytes(self.output)


def decompress_chunk(
    compressed: bytes,
    *,
    max_output_size: int = 4 * 1024 * 1024,
    expected_size: int | None = None,
) -> bytes:
    """Decode a blob-store LZ77 chunk, enforcing a bounded output size."""
    if max_output_size < 0 or (expected_size is not None and expected_size < 0):
        raise ValueError("Decompression sizes cannot be negative")
    if len(compressed) < 5:
        raise ProtocolError("Compressed data must contain at least five bytes")
    limit = min(max_output_size, expected_size) if expected_size is not None else max_output_size
    output = _Decoder(compressed, limit).decode()
    if expected_size is not None and len(output) != expected_size:
        raise IntegrityError("Decompressed chunk does not match its advertised size")
    return output
