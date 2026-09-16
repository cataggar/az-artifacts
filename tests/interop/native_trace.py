"""Sanitized native HTTP evidence: no credentials, receipts, or signed URLs."""

import hashlib
import json
import re
from collections import Counter
from threading import Lock
from urllib.parse import urlsplit

_ID = re.compile(r"[0-9A-Fa-f]{64}0[12]\Z")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_FIELDS = {
    "signature",
    "keepuntil",
    "receipts",
    "missing",
    "insufficientkeepuntil",
    "needaction",
    "result",
    "value",
    "type",
    "status",
    "children",
    "node",
    "keepuntilreceipt",
    "alreadyexists",
    "references",
    "item1",
    "item2",
}


def safe_shape(value, depth=0):
    if depth > 8:
        return {"kind": "depth-limit"}
    if isinstance(value, dict):
        result = {}
        for index, (key, child) in enumerate(value.items()):
            safe_key = key if _ID.fullmatch(key) or key.lower() in _FIELDS else f"<field-{index}>"
            result[safe_key] = (
                "<redacted>" if "signature" in key.lower() else safe_shape(child, depth + 1)
            )
        return result
    if isinstance(value, list):
        return [safe_shape(child, depth + 1) for child in value[:1024]]
    if isinstance(value, str):
        return value if _ID.fullmatch(value) or _TIME.fullmatch(value) else "<redacted>"
    if value is None or isinstance(value, bool):
        return value
    return {"kind": type(value).__name__}


class NativeTrace:
    def __init__(self, http, path, *, response_shapes=False):
        self.http = http
        self.original = http._request_once
        self.stream = path.open("x", encoding="utf-8")
        self.lock = Lock()
        self.sequence = 0
        self.statuses = Counter()
        self.registration_attempts = 0
        self.chunk_requests = 0
        self.chunk_bytes = 0
        self.chunk_count = 0
        self.node_bytes = 0
        self.missing = set()
        self.uploaded = set()
        self.response_shapes = response_shapes
        http._request_once = self.request

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.http._request_once = self.original
        self.stream.close()

    def write(self, value):
        with self.lock:
            self.stream.write(json.dumps(value, separators=(",", ":")) + "\n")
            self.stream.flush()

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path if kwargs["authenticated"] else "<signed-blob-download>"
        operation = "api-read"
        if path == "<signed-blob-download>":
            operation = "blob-download"
        elif method == "PUT" and path.endswith("/dedup/chunks"):
            operation = "chunks-put"
        elif method == "PUT" and "/dedup/nodes/" in path:
            operation = "node-put"
        elif method == "PUT" and "/upack/packages/" in path:
            operation = "registration"
        elif "/upack/packages/" in path:
            operation = "metadata"
        elif path.endswith("/dedup/urls"):
            operation = "blob-url-query"
        elif not path.endswith(("/ResourceAreas", "/connectionData")):
            path = "<other-api>"
        with self.lock:
            self.sequence += 1
            sequence = self.sequence
            if operation == "registration":
                self.registration_attempts += 1
        record = {
            "phase": "request",
            "sequence": sequence,
            "operation": operation,
            "method": method,
            "route": path,
        }
        chunk_ids = []
        data = kwargs.get("content")
        if operation in ("chunks-put", "node-put"):
            record["body_bytes"] = len(data)
            record["body_sha256"] = hashlib.sha256(data).hexdigest()
            for key, value in kwargs["headers"].items():
                if key.lower().startswith("x-ms-chunk-"):
                    identifier = key[len("x-ms-chunk-") :]
                    if not _ID.fullmatch(identifier) or not re.fullmatch(r"\d+/false", value):
                        raise RuntimeError("Unexpected native chunk-header structure")
                    chunk_ids.append(identifier.upper())
            record["chunk_ids"] = chunk_ids
            record["retention_summary_present"] = any(
                key.lower() == "x-ms-signature" for key in kwargs["headers"]
            )
        elif operation == "registration":
            body = kwargs["json_body"]
            for key in ("manifestId", "superRootId"):
                if not _ID.fullmatch(body[key]):
                    raise RuntimeError("Unexpected native registration identifier")
                record[key] = body[key]
            record["proof_count"] = len(body["proofNodes"])
        self.write(record)
        try:
            response = self.original(method, url, **kwargs)
        except Exception as error:
            self.write(
                {
                    "phase": "failure",
                    "sequence": sequence,
                    "operation": operation,
                    "error_type": type(error).__name__,
                }
            )
            raise
        result = {
            "phase": "response",
            "sequence": sequence,
            "operation": operation,
            "status": response.status,
        }
        if operation in ("node-put", "chunks-put") and response.status in (200, 409):
            body = response.json()
            if not isinstance(body, dict):
                raise RuntimeError("Unexpected native dedup response shape")
            if response.status == 409:
                missing = body.get("Missing", [])
                if not isinstance(missing, list) or not all(
                    isinstance(value, str) and _ID.fullmatch(value) for value in missing
                ):
                    raise RuntimeError("Unexpected native missing-content identifiers")
                result["missing_ids"] = missing
                result["insufficient_count"] = len(body.get("InsufficientKeepUntil", []))
                result["receipt_count"] = len(body.get("Receipts", {}))
                with self.lock:
                    self.missing.update(value.upper() for value in missing)
            else:
                result["receipt_count"] = len(body)
            if self.response_shapes:
                result["safe_response_shape"] = safe_shape(body)
        with self.lock:
            self.statuses[f"{operation}:{response.status}"] += 1
            if operation == "chunks-put" and response.status == 200:
                self.chunk_requests += 1
                self.chunk_bytes += len(data)
                self.chunk_count += len(chunk_ids)
                self.uploaded.update(chunk_ids)
            elif operation == "node-put":
                self.node_bytes += len(data)
        self.write(result)
        if operation in ("chunks-put", "registration"):
            progress = {"operation": operation, "status": response.status}
            if "body_bytes" in record:
                progress["body_bytes"] = record["body_bytes"]
            print(json.dumps(progress), flush=True)
        return response

    def summary(self):
        return {
            "registration_attempts": self.registration_attempts,
            "successful_chunk_requests": self.chunk_requests,
            "successful_chunk_body_bytes": self.chunk_bytes,
            "uploaded_chunk_count": self.chunk_count,
            "unique_uploaded_chunks": len(self.uploaded),
            "uploaded_chunks_requested_missing": len(self.uploaded & self.missing),
            "node_request_body_bytes": self.node_bytes,
            "statuses": dict(self.statuses),
        }
