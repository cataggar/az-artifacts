# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Dedup64K chunking, adapted from BuildXL's MIT-licensed managed chunker.

The 16-byte Rabin window, regression cuts, zero runs, and 1 MiB lookahead
follow RegressionChunking.cs and DeterministicChunker.cs at 16e96dc02e86.
"""

from collections.abc import Iterator
from typing import BinaryIO

MIN_CHUNK = 32768
MAX_CHUNK = 131072
BUFFER_SIZE = 1048576
_MASK64 = (1 << 64) - 1
_POLYNOMIAL = 0xF2B5D42C384A2167


def _tables() -> tuple[tuple[int, ...], tuple[int, ...]]:
    down = []
    for byte in range(256):
        value = byte << 56
        for _ in range(8):
            value = ((value << 1) ^ (_POLYNOMIAL if value >> 63 else 0)) & _MASK64
        down.append(value)
    up = []
    for byte in range(256):
        value = byte
        for _ in range(15):
            value = ((value << 8) & _MASK64) ^ down[value >> 56]
        up.append(value)
    return tuple(down), tuple(up)


_DOWN, _UP = _tables()


def boundaries(data: bytes) -> list[int]:
    """Return end offsets for one independent managed-chunker buffer."""
    length = len(data)
    result: list[int] = []
    start = 0
    zero_run = True
    down, up, mask64 = _DOWN, _UP, _MASK64
    while start < length:
        if length - start < MIN_CHUNK:
            result.append(length)
            break
        pos = start + MIN_CHUNK
        end = min(length, start + MAX_CHUNK)
        if zero_run and not data[start:pos].strip(b"\0"):
            while pos < end and data[pos] == 0:
                pos += 1
            result.append(pos)
            start = pos
            zero_run = True
            continue
        zero_run = False
        value = 0
        for byte in data[pos - 16 : pos]:
            value = ((value << 8) & mask64) ^ byte ^ down[value >> 56]
        regress = [-1, -1, -1, -1]
        while True:
            while pos < end and value & 0xFFF != 0x555:
                value ^= up[data[pos - 16]]
                value = ((value << 8) & mask64) ^ data[pos] ^ down[value >> 56]
                pos += 1
            if value & 0xFFF == 0x555:
                index = 3
                while index >= 0:
                    regress[index] = pos
                    mask = 0xFFFF >> index
                    if value & mask != 0x5555 & mask:
                        break
                    index -= 1
                if index == -1:
                    result.append(pos)
                    start = pos
                    zero_run = True
                    break
                if pos < end:
                    value ^= up[data[pos - 16]]
                    value = ((value << 8) & mask64) ^ data[pos] ^ down[value >> 56]
                    pos += 1
                    continue
            if pos - start == MAX_CHUNK:
                cut = next((point for point in regress if point >= 0), pos)
                result.append(cut)
                start = cut
                regress = [point if point >= start + MIN_CHUNK else -1 for point in regress]
                if pos - start >= MIN_CHUNK:
                    end = min(length, start + MAX_CHUNK)
                    continue
                zero_run = pos - start < 16 and not data[start:pos].strip(b"\0")
                break
            result.append(length)
            return result
    return result


def chunks(stream: BinaryIO) -> Iterator[bytes]:
    """Yield compatible chunks with at most one 1 MiB lookahead buffer."""
    pending = bytearray()
    eof = False
    while not eof:
        while len(pending) < BUFFER_SIZE:
            block = stream.read(BUFFER_SIZE - len(pending))
            if not block:
                eof = True
                break
            pending.extend(block)
        if not pending:
            return
        data = bytes(pending)
        cuts = boundaries(data)
        if not eof:
            cuts = cuts[:-1]
        start = 0
        for end in cuts:
            yield data[start:end]
            start = end
        if eof:
            return
        pending = bytearray(data[start:])
