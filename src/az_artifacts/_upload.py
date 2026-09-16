"""Dedup upload and retention negotiation; receipts exist only in process memory."""

import base64
import binascii
import hashlib
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import _json
from ._http import Http, Response, endpoint
from ._prepare import Node, PreparedPackage
from .errors import IncompleteUploadError, ProtocolError
from .models import BlobRef

_BATCH_SIZE = 64
_RESPONSE_LIMIT = 2 * 1024 * 1024


@dataclass(frozen=True)
class Receipt:
    keep_until: datetime
    signature: bytes = field(repr=False)


def receipts(value: object, allowed: set[str]) -> dict[str, Receipt]:
    obj = _json.as_object(value, "retention receipts")
    if len(obj) > len(allowed):
        raise ProtocolError("Unexpected retention receipt count")
    result = {}
    for key, value in obj.items():
        identifier = _json.blob_id(key)
        if identifier not in allowed or identifier in result:
            raise ProtocolError("Unexpected or duplicate retention receipt")
        item = _json.as_object(value, "retention receipt")
        keep = _json.as_object(item.get("KeepUntil"), "retention expiry")
        stamp = _json.string(keep.get("KeepUntil"), "retention expiry")
        signature = _json.string(item.get("Signature"), "retention signature")
        if len(stamp) != 20:
            raise ProtocolError("Invalid retention expiry format")
        if len(signature) > 2048:
            raise ProtocolError("Retention signature exceeds supported size")
        try:
            expiration = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            raw = base64.b64decode(signature, validate=True)
        except (ValueError, binascii.Error):
            raise ProtocolError("Malformed retention receipt") from None
        if not raw:
            raise ProtocolError("Empty retention signature")
        result[identifier] = Receipt(expiration, raw)
    return result


def summary_headers(children: tuple[BlobRef, ...], known: dict[str, Receipt]) -> dict[str, str]:
    # The public Microsoft SDK formatter hashes the ordered child signatures.
    signatures = hashlib.sha256()
    dates = []
    for child in children:
        receipt = known[child.id]
        signatures.update(receipt.signature)
        dates.append(receipt.keep_until.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return {
        "X-MS-KeepUntils": ",".join(dates),
        "X-MS-Signature": base64.b64encode(signatures.digest()).decode("ascii"),
    }


class Uploader:
    def __init__(self, http: Http, url: str, package: PreparedPackage, *, max_workers: int) -> None:
        self.http = http
        self.url = url
        self.package = package
        self.workers = min(max_workers, 16)
        self.keep_until = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)
        self.known: dict[str, Receipt] = {}
        self.bytes_uploaded = 0

    def _ready(self, identifier: str) -> bool:
        receipt = self.known.get(identifier)
        return (
            receipt is not None
            and receipt.keep_until >= self.keep_until
            and receipt.keep_until > datetime.now(UTC)
        )

    def _remember(self, found: dict[str, Receipt]) -> None:
        for identifier, receipt in found.items():
            current = self.known.get(identifier)
            if current is None or receipt.keep_until >= current.keep_until:
                self.known[identifier] = receipt

    def _put(self, url: str, data: bytes, headers: dict[str, str]) -> Response:
        return self.http.request(
            "PUT",
            url,
            params={"keepUntil": self.keep_until.strftime("%Y-%m-%dT%H:%M:%SZ")},
            content=data,
            headers={
                "Accept": "application/json; api-version=1.0",
                "Content-Type": "application/octet-stream; api-version=1.0-preview",
                "Content-Range": f"bytes */{len(data)}",
                **headers,
            },
            accepted_statuses=frozenset({409}),
            retry_safe=False,
            max_bytes=_RESPONSE_LIMIT,
        )

    def _batch(self, identifiers: tuple[str, ...]) -> tuple[dict[str, Receipt], int]:
        data = bytearray()
        headers = {}
        for identifier in identifiers:
            chunk = self.package.chunks[identifier].read()
            headers["X-ms-chunk-" + identifier] = f"{len(chunk)}/false"
            data.extend(chunk)
        response = self._put(endpoint(self.url, "_apis", "dedup", "chunks"), bytes(data), headers)
        if response.status != 200:
            raise IncompleteUploadError(
                "Chunk upload was not acknowledged; registration not attempted"
            )
        found = receipts(response.json(), set(identifiers))
        if set(found) != set(identifiers) or any(
            receipt.keep_until < self.keep_until for receipt in found.values()
        ):
            raise IncompleteUploadError(
                "Chunk retention was not completed; registration not attempted"
            )
        return found, len(data)

    def _chunks(self, identifiers: list[str]) -> None:
        identifiers = [identifier for identifier in identifiers if not self._ready(identifier)]
        if not identifiers:
            return
        batches = iter(
            tuple(identifiers[offset : offset + _BATCH_SIZE])
            for offset in range(0, len(identifiers), _BATCH_SIZE)
        )
        pending: set[Future[tuple[dict[str, Receipt], int]]] = set()
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            for batch in batches:
                pending.add(executor.submit(self._batch, batch))
                if len(pending) == self.workers:
                    complete, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in complete:
                        found, size = future.result()
                        self._remember(found)
                        self.bytes_uploaded += size
            for future in pending:
                found, size = future.result()
                self._remember(found)
                self.bytes_uploaded += size

    def _node_request(self, node: Node) -> Response:
        headers = (
            summary_headers(node.children, self.known)
            if all(self._ready(child.id) for child in node.children)
            else {}
        )
        response = self._put(
            endpoint(self.url, "_apis", "dedup", "nodes", node.ref.id), node.data, headers
        )
        self.bytes_uploaded += len(node.data)
        return response

    def _acknowledge(self, node: Node, response: Response) -> None:
        if response.status != 200:
            raise IncompleteUploadError(
                "Node retention was not completed; registration not attempted"
            )
        allowed = {node.ref.id, *(child.id for child in node.children)}
        found = receipts(response.json(), allowed)
        own = found.get(node.ref.id)
        if own is None or own.keep_until < self.keep_until or own.keep_until <= datetime.now(UTC):
            raise IncompleteUploadError(
                "Node receipt is missing or expired; registration not attempted"
            )
        # Existing nodes can return their own receipt plus immediate child receipts.
        self._remember(found)

    def _node(self, identifier: str) -> None:
        if self._ready(identifier):
            return
        node = self.package.nodes[identifier]
        had_proof = all(self._ready(child.id) for child in node.children)
        response = self._node_request(node)
        if response.status == 200:
            self._acknowledge(node, response)
            return
        if response.status != 409:
            raise IncompleteUploadError(
                "Node upload was not acknowledged; registration not attempted"
            )
        if had_proof:
            raise IncompleteUploadError(
                "Completed retention proof was rejected; registration not attempted"
            )
        obj = _json.as_object(response.json(), "node negotiation")
        children = {child.id for child in node.children}
        required: set[str] = set()
        for field_name in ("Missing", "InsufficientKeepUntil"):
            for value in _json.as_list(obj.get(field_name), "node retention requirements"):
                child_id = _json.blob_id(value)
                if child_id not in children:
                    raise ProtocolError("Node negotiation requested an unknown child")
                required.add(child_id)
        self._remember(receipts(obj.get("Receipts"), children))
        gained_proof = not had_proof and all(self._ready(child) for child in children)
        if (not required and not gained_proof) or any(
            child not in required and not self._ready(child) for child in children
        ):
            raise IncompleteUploadError(
                "Node negotiation did not account for child retention; registration not attempted"
            )
        chunk_ids = []
        for child_id in sorted(required):
            if child_id.endswith("02"):
                self._node(child_id)
            else:
                chunk_ids.append(child_id)
        self._chunks(chunk_ids)
        if not all(self._ready(child) for child in children):
            raise IncompleteUploadError("Child retention is incomplete; registration not attempted")
        self._acknowledge(node, self._node_request(node))

    def upload(self) -> int:
        self._node(self.package.content_root.id)
        self._node(self.package.super_root.id)
        if not self._ready(self.package.super_root.id):
            raise IncompleteUploadError(
                "Super-root retention is incomplete; registration not attempted"
            )
        return self.bytes_uploaded
