"""Normalized Microsoft-tool captures; identities and dates are synthetic."""

import base64
import hashlib
import json
from pathlib import Path

import pytest

from az_artifacts import _json
from az_artifacts._dedup import content_hash, parse_node
from az_artifacts.models import BlobRef

FIXTURES = Path(__file__).parent / "fixtures" / "publishing"


def records(name="approved-publish.jsonl"):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


def test_exact_reference_manifest_matches_captured_upload():
    manifest = (FIXTURES / "reference-manifest.json").read_bytes()
    verification = json.loads((FIXTURES / "reference-verification.json").read_text())
    assert len(manifest) == 175
    assert content_hash(manifest) + "01" == verification["metadata"]["manifest_id"]
    parsed = json.loads(manifest)
    assert parsed["manifestFormat"] == "1.1.0"
    assert parsed["manifestReferences"] == []
    items = _json.manifest(parsed)
    assert len(items) == 1
    assert items[0].path == "/hello.txt"
    assert items[0].blob.size == 44
    upload = next(
        r
        for r in records()
        if r.get("phase") == "request" and (r.get("body") or {}).get("length") == 175
    )
    assert hashlib.sha256(manifest).hexdigest() == upload["body"]["sha256"]
    assert json.dumps(parsed, separators=(",", ":")).encode() == manifest


def test_reference_proofs_are_serialized_nodes_not_content_id_strings():
    registration = next(
        r
        for r in records()
        if r.get("method") == "PUT" and "/upack/packages/" in r.get("route", "")
    )
    metadata = registration["body"]["json"]
    proofs = [base64.b64decode(p["value"], validate=True) for p in metadata["proofNodes"]]
    assert len(proofs) == 2
    file_tree, super_root = proofs
    hello = b"az-artifacts native publishing feasibility\r\n"
    assert parse_node(file_tree) == (BlobRef(content_hash(hello) + "01", 44),)
    assert super_root == (FIXTURES / "reference-super-root.bin").read_bytes()
    assert content_hash(super_root) + "02" == metadata["superRootId"]
    assert parse_node(super_root) == (
        BlobRef(content_hash(file_tree) + "02", 44),
        BlobRef(metadata["manifestId"], 175),
    )


def test_reference_missing_content_is_409_not_package_conflict():
    exchanges = records()
    missing = [r for r in exchanges if r.get("phase") == "response" and r.get("status") == 409]
    assert len(missing) == 2
    for response in missing:
        assert "/dedup/nodes/" in response["route"]
        assert response["body"]["json"]["InsufficientKeepUntil"] == []
        assert len(response["body"]["json"]["Missing"]) == 1
        assert response["body"]["json"]["Receipts"] == {}
        repeated = [
            r
            for r in exchanges
            if r.get("phase") == "response"
            and r.get("status") == 200
            and r.get("route") == response["route"]
        ]
        assert len(repeated) == 1
        assert repeated[0]["sequence"] > response["sequence"]
    registrations = [
        r
        for r in exchanges
        if r.get("method") == "PUT" and "/upack/packages/" in r.get("route", "")
    ]
    assert len(registrations) == 1
    assert (
        next(
            r["status"]
            for r in exchanges
            if r.get("phase") == "response" and r.get("sequence") == registrations[0]["sequence"]
        )
        == 204
    )


def test_reference_retention_and_framing_observations():
    uploads = [r for r in records() if r.get("method") == "PUT" and "/dedup/" in r.get("route", "")]
    keep_until = set()
    for upload in uploads:
        keep_until.update(p["value"] for p in upload["query"] if p["name"] == "keepUntil")
        headers = {h["name"].lower(): h["values"] for h in upload["content_headers"]}
        assert headers["content-type"] == ["application/octet-stream; api-version=1.0-preview"]
        assert headers["content-range"] == [f"bytes */{upload['body']['length']}"]
    assert keep_until == {"2000-01-03T19:10:51Z"}
    assert any(h["name"].lower().startswith("x-ms-chunk-") for r in uploads for h in r["headers"])
    # Values were deliberately not retained in the first capture; do not invent them.
    assert all(
        h["values"] == ["<redacted>"]
        for r in uploads
        for h in r["headers"]
        if h["name"].lower().startswith("x-ms-chunk-") or h["name"] == "X-MS-KeepUntils"
    )


def test_verified_package_size_includes_manifest_bytes():
    verification = json.loads((FIXTURES / "reference-verification.json").read_text())
    assert verification["native_download"] == "verified"
    assert verification["downloaded_bytes"] == 44
    assert verification["metadata"]["package_size"] == 219
    assert verification["metadata"]["package_size"] == sum(
        child.size for child in parse_node((FIXTURES / "reference-super-root.bin").read_bytes())
    )
    assert (
        verification["file_sha256"]
        == hashlib.sha256(b"az-artifacts native publishing feasibility\r\n").hexdigest()
    )


def test_capture_does_not_contain_auth_headers_or_signed_storage_urls():
    for name in (
        "preflight-download.jsonl",
        "approved-publish.jsonl",
        "download.jsonl",
        "next-approved-publish.jsonl",
        "next-download.jsonl",
    ):
        text = (FIXTURES / name).read_text().lower()
        assert '"authorization"' not in text
        assert "bearer " not in text
        assert "?sig=" not in text
        for record in records(name):
            if record.get("route") == "<storage-or-other>" and record.get("phase") == "request":
                assert record["query"] == []


def test_completed_reference_payloads_are_distinct():
    evidence = json.loads((FIXTURES / "protocol_evidence.json").read_text())
    completed = evidence["reference_publish_proposal"]
    proposed = evidence["next_reference_experiment"]
    assert not completed["approved"] and completed["fixture_only"] and completed["completed"]
    assert not proposed["approved"] and proposed["fixture_only"] and proposed["completed"]
    assert completed["version"] != proposed["version"]
    seed = b"az-artifacts reference multi-chunk v1\0"
    payload_size = 104857600
    digest = hashlib.sha256()
    for start in range(0, payload_size // 32, 32768):
        digest.update(
            b"".join(
                hashlib.sha256(seed + i.to_bytes(8, "little")).digest()
                for i in range(start, start + 32768)
            )
        )
    files = {
        "payload.bin": (payload_size, digest.hexdigest()),
        "empty.txt": (0, hashlib.sha256(b"").hexdigest()),
        "nested/repeated.bin": (131072, hashlib.sha256(bytes(131072)).hexdigest()),
    }
    assert sum(size for size, _ in files.values()) == proposed["total_bytes"]
    for item in proposed["files"]:
        size, sha256 = files[item["path"]]
        assert size == item["size"]
        assert sha256 == item["sha256"]


def test_reference_runner_rejects_public_fixture_writes(monkeypatch):
    from interop import reference_tool

    monkeypatch.setattr(
        reference_tool.subprocess, "run", lambda *a, **kw: pytest.fail("must not invoke any tool")
    )
    with pytest.raises(RuntimeError, match="Public fixtures"):
        reference_tool.main(
            [
                "approved-publish",
                "--execute-approved",
                "--proposal",
                str(FIXTURES / "protocol_evidence.json"),
            ]
        )


def test_reference_gate_rejects_unapproved_and_native_only_proposals():
    from interop.reference_tool import check_publish_allowed

    with pytest.raises(RuntimeError, match="not been approved"):
        check_publish_allowed({"approved": False})
    with pytest.raises(RuntimeError, match="native-only"):
        check_publish_allowed({"approved": True, "publisher": "native-python"})
    with pytest.raises(RuntimeError, match="completed"):
        check_publish_allowed({"approved": True, "completed": True})
    with pytest.raises(RuntimeError, match="Public fixtures"):
        check_publish_allowed({"approved": True, "fixture_only": True})


def test_sdk_wire_vectors_establish_batch_framing_and_ordered_signature_digest():
    fixture = json.loads((FIXTURES / "sdk-wire-vectors.json").read_text())
    batch = next(row for row in fixture["records"] if row.get("route") == "/_apis/dedup/chunks")
    headers = {row["name"]: row["values"] for row in batch["headers"]}
    assert headers["X-ms-chunk-" + content_hash(b"abc") + "01"] == ["3/false"]
    assert headers["X-ms-chunk-" + content_hash(b"defgh") + "01"] == ["5/false"]
    assert bytes.fromhex(batch["body"]) == b"abcdefgh"
    summary = next(row for row in fixture["records"] if row["source"] == "synthetic receipts only")
    assert summary["summarySignatureIsOrderedSha256"]
    assert summary["singleSummaryIsSha256"]
    assert not summary["summarySignatureIsXor"]
    assert summary["syntheticAggregateLength"] == 32


def test_upload_account_discovery_matches_every_real_dedup_write():
    discovery = json.loads((FIXTURES / "upload-account-discovery.json").read_text())
    uploads = [
        row
        for row in records("next-approved-publish.jsonl")
        if row.get("phase") == "request"
        and row.get("method") == "PUT"
        and "/dedup/" in row.get("route", "")
    ]
    assert len(uploads) == discovery["captured_write_count"] == 39
    assert all(
        row["route"].startswith("/A" + discovery["connection_data_instance_id"] + "/")
        for row in uploads
    )


def test_large_reference_native_download_verified_exact_approved_bytes():
    result = json.loads((FIXTURES / "next-reference-verification.json").read_text())
    proposal = json.loads((FIXTURES / "protocol_evidence.json").read_text())[
        "next_reference_experiment"
    ]
    assert result["native_download"] == "verified"
    assert result["files"] == proposal["files"]
    assert result["downloaded_bytes"] == proposal["total_bytes"]
    assert result["metadata"]["package_size"] == proposal["total_bytes"] + 427
