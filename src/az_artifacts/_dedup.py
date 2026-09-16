# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Dedup blob reading based on azure-devops-rust-api and BuildXL's node format."""

import hashlib
from collections import OrderedDict, deque
from collections.abc import Iterator, Sequence
from concurrent.futures import Executor, Future
from threading import Lock

from . import _json
from ._decompress import decompress_chunk
from ._http import Http, endpoint, validate_url
from .errors import IntegrityError, PermissionDeniedError, ProtocolError
from .models import BlobRef

MAX_CHUNK_BYTES = (1 << 24) - 1
MAX_NODE_BYTES = 4 + 512 * 40
MAX_TREE_DEPTH = 64
_URL_BATCH_SIZE = 100
_URL_CACHE_SIZE = 2048
_EMPTY_CHUNK_ID = hashlib.sha512(b"").digest()[:32].hex().upper() + "01"


def content_hash(data: bytes) -> str:
    """Dedup uses ordinary SHA-512 truncated to 32 bytes, not SHA-512/256."""
    return hashlib.sha512(data).digest()[:32].hex().upper()


def serialize_node(children: Sequence[BlobRef]) -> bytes:
    if not 1 <= len(children) <= 512:
        raise ValueError("Dedup nodes require 1 through 512 children")
    data = bytearray(b"\0\0" + (len(children) - 1).to_bytes(2, "little"))
    for child in children:
        identifier = _json.blob_id(child.id)
        node = identifier.endswith("02")
        width = 7 if node else 3
        if not 0 <= child.size < 1 << (width * 8):
            raise ValueError("Dedup child size cannot be represented")
        data.append(int(node))
        data.extend(child.size.to_bytes(width, "little"))
        data.extend(bytes.fromhex(identifier[:64]))
    return bytes(data)


def parse_node(data: bytes) -> tuple[BlobRef, ...]:
    if len(data) < 4:
        raise ProtocolError("Truncated dedup node header")
    if int.from_bytes(data[:2], "little") != 0:
        raise ProtocolError("Unsupported dedup node format version")
    count = int.from_bytes(data[2:4], "little") + 1
    if count > 512:
        raise ProtocolError("Dedup node contains more than 512 children")
    children = []
    offset = 4
    for _ in range(count):
        if offset >= len(data):
            raise ProtocolError("Truncated dedup node entry")
        kind = data[offset]
        if kind not in (0, 1):
            raise ProtocolError("Unsupported dedup child type")
        size_bytes = 3 if kind == 0 else 7
        end = offset + 1 + size_bytes + 32
        if end > len(data):
            raise ProtocolError("Truncated dedup node entry")
        logical_size = int.from_bytes(data[offset + 1 : offset + 1 + size_bytes], "little")
        identifier = data[offset + 1 + size_bytes : end].hex().upper()
        children.append(BlobRef(identifier + ("01" if kind == 0 else "02"), logical_size))
        offset = end
    if offset != len(data):
        raise ProtocolError("Dedup node contains unexpected trailing bytes")
    return tuple(children)


def decode_blob(data: bytes, identifier: str, *, size: int | None, limit: int) -> bytes:
    if size is not None and size > limit:
        raise ProtocolError("Advertised blob size exceeds the supported limit")
    # No public transport flag reliably distinguishes raw and LZ77 data. The
    # content ID lets us identify raw data without guessing from its length.
    if content_hash(data) == identifier[:64]:
        if len(data) > limit or (size is not None and len(data) != size):
            raise IntegrityError("Blob does not match its advertised size")
        return data
    try:
        decoded = decompress_chunk(data, max_output_size=limit, expected_size=size)
    except ProtocolError as error:
        raise IntegrityError("Blob is neither valid raw data nor supported LZ77 content") from error
    if content_hash(decoded) != identifier[:64]:
        raise IntegrityError("Blob content hash does not match its identifier")
    return decoded


def _validate_ref(ref: BlobRef) -> BlobRef:
    identifier = _json.blob_id(ref.id)
    size = _json.size(ref.size, "blob size")
    if identifier.endswith("01") and size > MAX_CHUNK_BYTES:
        raise ProtocolError("Advertised blob size exceeds the supported limit")
    if identifier == _EMPTY_CHUNK_ID and size != 0:
        raise IntegrityError("Empty chunk does not match its advertised size")
    return BlobRef(identifier, size)


def _check_tree(ref: BlobRef, ancestors: frozenset[str]) -> None:
    if len(ancestors) >= MAX_TREE_DEPTH or ref.id in ancestors:
        raise ProtocolError("Dedup tree is cyclic or exceeds the supported depth")


class BlobReader:
    def __init__(self, http: Http, blob_url: str) -> None:
        self._http = http
        self._blob_url = blob_url
        self._urls: OrderedDict[str, str] = OrderedDict()
        self._lock = Lock()

    def resolve(self, identifiers: Sequence[str], *, refresh: bool = False) -> dict[str, str]:
        identifiers = tuple(dict.fromkeys(identifiers))
        with self._lock:
            found = {}
            missing = []
            for identifier in identifiers:
                if not refresh and identifier in self._urls:
                    found[identifier] = self._urls[identifier]
                    self._urls.move_to_end(identifier)
                else:
                    missing.append(identifier)
            for offset in range(0, len(missing), _URL_BATCH_SIZE):
                batch = missing[offset : offset + _URL_BATCH_SIZE]
                response = self._http.request(
                    "POST",
                    endpoint(self._blob_url, "_apis", "dedup", "urls"),
                    params={"allowEdge": "true"},
                    json_body=batch,
                    retry_safe=True,
                    headers={
                        "Content-Type": "application/json; charset=utf-8; api-version=1.0-preview",
                        "Accept": "application/json; api-version=1.0",
                    },
                )
                obj = _json.as_object(response.json(), "blob URLs")
                urls = {
                    _json.blob_id(key): _json.string(value, "blob URL")
                    for key, value in obj.items()
                }
                for identifier in batch:
                    if identifier not in urls:
                        raise ProtocolError("Dedup service did not return a requested blob URL")
                    url = urls[identifier]
                    validate_url(url)
                    found[identifier] = url
                    self._urls[identifier] = url
                    self._urls.move_to_end(identifier)
                while len(self._urls) > _URL_CACHE_SIZE:
                    self._urls.popitem(last=False)
            return found

    def blob(self, identifier: str, *, size: int | None, limit: int) -> bytes:
        if identifier == _EMPTY_CHUNK_ID:
            return decode_blob(b"", identifier, size=size, limit=limit)
        if size is not None and size > limit:
            raise ProtocolError("Advertised blob size exceeds the supported limit")
        for attempt in range(2):
            url = self.resolve([identifier], refresh=attempt == 1)[identifier]
            try:
                response = self._http.request(
                    "GET", url, authenticated=False, max_bytes=2 * limit + 65536
                )
            except PermissionDeniedError:
                if attempt == 1:
                    raise
                # A queued signed URL can expire. Resolve it once more rather
                # than sending an Azure credential to the blob endpoint.
                continue
            return decode_blob(response.body, identifier, size=size, limit=limit)
        raise AssertionError("Blob refresh loop exhausted without a result")

    def _node(self, identifier: str, *, size: int | None) -> tuple[BlobRef, ...]:
        identifier = _json.blob_id(identifier)
        if not identifier.endswith("02"):
            raise ProtocolError("Expected a dedup node identifier")
        node = self.blob(identifier, size=None, limit=MAX_NODE_BYTES)
        children = tuple(_validate_ref(child) for child in parse_node(node))
        if size is not None and sum(child.size for child in children) != size:
            raise IntegrityError("Dedup node children do not match its advertised logical size")
        return children

    def leaf_refs(self, ref: BlobRef) -> Iterator[BlobRef]:
        """Yield ordered, validated leaf boundaries, fetching ONLY node blobs.

        Root ID/size validation is eager; node reads and traversal are lazy.
        Repeated references are yielded repeatedly, not deduplicated. Memory is
        bounded by node wire size and depth; no payload URLs are prefetched.
        Partial consumption validates only the structures actually traversed.
        """
        return self._leaf_refs(_validate_ref(ref), frozenset())

    def _leaf_refs(
        self, ref: BlobRef, ancestors: frozenset[str], *, resolve_urls: bool = False
    ) -> Iterator[BlobRef]:
        _check_tree(ref, ancestors)
        if ref.id.endswith("01"):
            yield ref
            return
        children = self._node(ref.id, size=ref.size)
        if resolve_urls:
            self.resolve([child.id for child in children if child.id != _EMPTY_CHUNK_ID])
        for child in children:
            yield from self._leaf_refs(
                _validate_ref(child), ancestors | {ref.id}, resolve_urls=resolve_urls
            )

    def content(
        self,
        ref: BlobRef,
        *,
        ancestors: frozenset[str] = frozenset(),
        executor: Executor | None = None,
        prefetch: int = 1,
    ) -> Iterator[bytes]:
        children = self._leaf_refs(_validate_ref(ref), ancestors, resolve_urls=True)
        if executor is None:
            for child in children:
                yield self.blob(child.id, size=child.size, limit=MAX_CHUNK_BYTES)
            return
        pending: deque[Future[bytes]] = deque()
        try:
            for child in children:
                pending.append(
                    executor.submit(self.blob, child.id, size=child.size, limit=MAX_CHUNK_BYTES)
                )
                if len(pending) >= prefetch:
                    yield pending.popleft().result()
            while pending:
                yield pending.popleft().result()
        finally:
            for future in pending:
                future.cancel()

    def manifest(self, identifier: str, *, limit: int) -> bytes:
        identifier = _json.blob_id(identifier)
        if identifier.endswith("01"):
            return self.blob(identifier, size=None, limit=limit)
        children = self._node(identifier, size=None)
        if sum(child.size for child in children) > limit:
            raise ProtocolError("Manifest exceeds the configured size limit")
        result = bytearray()
        for child in children:
            for data in self.content(child, ancestors=frozenset({identifier})):
                if len(result) + len(data) > limit:
                    raise ProtocolError("Manifest exceeds the configured size limit")
                result.extend(data)
        return bytes(result)
