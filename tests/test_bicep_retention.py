"""Synthetic regression vectors for the acknowledgment shape observed with Bicep."""

import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from az_artifacts._dedup import content_hash, serialize_node
from az_artifacts._http import Response
from az_artifacts._prepare import Node
from az_artifacts._upload import Receipt, Uploader
from az_artifacts.errors import IncompleteUploadError, ProtocolError
from az_artifacts.models import BlobRef


@pytest.fixture
def captured_node(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 1, 1, tzinfo=UTC)

    monkeypatch.setattr("az_artifacts._upload.datetime", Clock)
    unique = tuple(
        BlobRef(content_hash(f"synthetic-child-{index}".encode()) + "01", 32)
        for index in range(510)
    )
    children = (*unique, unique[0], unique[1])
    data = serialize_node(children)
    identifier = content_hash(data) + "02"
    node = Node(BlobRef(identifier, sum(child.size for child in children)), children, data)
    body = {
        key: {
            "KeepUntil": {"KeepUntil": "2025-01-04T00:00:00Z"},
            "Signature": base64.b64encode(hashlib.sha256(key.encode()).digest()).decode(),
        }
        for key in (identifier, *(child.id for child in children))
    }
    uploader = Uploader(None, "unused", SimpleNamespace(), max_workers=1)
    return node, body, uploader


def response(body):
    return Response(json.dumps(body).encode(), {}, 200)


@pytest.mark.parametrize("include_children", [False, True])
def test_node_acknowledges_own_receipt_with_optional_immediate_children(
    captured_node, include_children
):
    node, body, uploader = captured_node
    assert len(node.children) == 512
    assert len({child.id for child in node.children}) == 510
    assert len(body) == 511 and node.ref.id in body
    selected = body if include_children else {node.ref.id: body[node.ref.id]}
    uploader._acknowledge(node, response(selected))
    assert uploader._ready(node.ref.id)
    assert len(uploader.known) == (511 if include_children else 1)


@pytest.mark.parametrize("failure", ["empty", "children-only", "expired-own", "unknown-id"])
def test_child_receipts_never_substitute_for_a_valid_own_receipt(captured_node, failure):
    node, body, uploader = captured_node
    if failure == "empty":
        body = {}
    elif failure == "children-only":
        body.pop(node.ref.id)
    elif failure == "expired-own":
        body[node.ref.id]["KeepUntil"]["KeepUntil"] = "2000-01-01T00:00:00Z"
    else:
        body = {
            node.ref.id: body[node.ref.id],
            "B" * 64 + "01": next(iter(body.values())),
        }
    error = ProtocolError if failure == "unknown-id" else IncompleteUploadError
    with pytest.raises(error):
        uploader._acknowledge(node, response(body))
    assert not uploader._ready(node.ref.id)


def test_shared_child_receipts_do_not_downgrade_stronger_existing_proofs(captured_node):
    node, body, uploader = captured_node
    identifier = node.children[0].id
    existing = Receipt(datetime(2030, 1, 1, tzinfo=UTC), b"synthetic-existing-proof")
    uploader.known[identifier] = existing
    body[identifier]["KeepUntil"]["KeepUntil"] = "2000-01-01T00:00:00Z"
    uploader._acknowledge(node, response(body))
    assert uploader._ready(node.ref.id)
    assert uploader.known[identifier] is existing
    assert uploader._ready(identifier)


def test_non_immediate_descendant_receipts_are_rejected(captured_node):
    inner, body, uploader = captured_node
    children = (inner.ref,)
    data = serialize_node(children)
    outer = Node(BlobRef(content_hash(data) + "02", inner.ref.size), children, data)
    with pytest.raises(ProtocolError, match="Unexpected or duplicate"):
        uploader._acknowledge(
            outer,
            response(
                {
                    outer.ref.id: body[inner.ref.id],
                    inner.children[0].id: body[inner.children[0].id],
                }
            ),
        )
    assert not uploader._ready(outer.ref.id)


def test_retention_diagnosis_requires_explicit_write_opt_in(monkeypatch):
    from interop import bicep_retention

    monkeypatch.setattr(bicep_retention, "load_local_proposal", lambda path: {"approved": True})
    monkeypatch.setattr(
        bicep_retention,
        "UniversalPackageClient",
        lambda *args, **kwargs: pytest.fail("No network before explicit opt-in"),
    )
    with pytest.raises(RuntimeError, match="opt-in"):
        bicep_retention.main(["--proposal", "local.json", "--capture", "diagnosis.jsonl"])
