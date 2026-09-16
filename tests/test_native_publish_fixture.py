"""Normalized captured native cold-content publish; identities and dates are synthetic."""

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "publishing"


def test_live_native_publish_uploaded_missing_content_and_registered_once():
    result = json.loads((FIXTURES / "native-publish-result.json").read_text())
    assert result["native_publish"] == "confirmed"
    assert result["metadata"]["version"] == "0.0.3-native.20000101"
    assert result["metadata"]["package_size"] == 104989099
    assert result["bytes_uploaded"] == 104957027
    transfer = result["transfer"]
    assert transfer["registration_attempts"] == 1
    assert transfer["successful_chunk_requests"] == 23
    assert transfer["successful_chunk_body_bytes"] == 104857600 + 427
    assert transfer["node_request_body_bytes"] == 99000
    assert (
        transfer["unique_uploaded_chunks"] == transfer["uploaded_chunks_requested_missing"] == 1368
    )
    rows = [
        json.loads(line)
        for line in (FIXTURES / "native-python-http-1.jsonl").read_text().splitlines()
    ]
    registrations = [
        row for row in rows if row["phase"] == "response" and row["operation"] == "registration"
    ]
    assert len(registrations) == 1 and registrations[0]["status"] == 204
    uploaded = {
        identifier
        for row in rows
        if row["phase"] == "request" and row["operation"] == "chunks-put"
        for identifier in row["chunk_ids"]
    }
    missing = {
        identifier
        for row in rows
        if row["phase"] == "response"
        for identifier in row.get("missing_ids", [])
    }
    assert len(uploaded) == 1368 and uploaded <= missing
    assert (
        sum(
            row["body_bytes"]
            for row in rows
            if row["phase"] == "request" and row["operation"] in ("chunks-put", "node-put")
        )
        == result["bytes_uploaded"]
    )


def test_live_native_roundtrip_matches_exact_cold_approval_and_is_complete():
    result = json.loads((FIXTURES / "native-publish-result.json").read_text())
    proposal = json.loads((FIXTURES / "protocol_evidence.json").read_text())[
        "native_interop_proposal"
    ]
    assert proposal["approved"] is False and proposal["fixture_only"] is True
    assert proposal["completed"] is True
    assert result["files"] == proposal["files"]
    assert result["native_download"] == result["artifacttool_download"]
    assert result["native_download"] == "all approved sizes and SHA256 hashes verified"
    assert sum(item["size"] for item in result["files"]) == 104988672
    hashes = json.loads((FIXTURES / "native-roundtrip-hashes.json").read_text())
    assert hashes["source"] == hashes["native"] == hashes["artifacttool"] == proposal["files"]
    artifacttool_requests = [
        json.loads(line) for line in (FIXTURES / "native-download.jsonl").read_text().splitlines()
    ]
    assert not any(row.get("method") in ("PUT", "PATCH", "DELETE") for row in artifacttool_requests)


def test_native_capture_contains_no_auth_or_signed_blob_paths():
    text = (FIXTURES / "native-python-http-1.jsonl").read_text()
    assert '"authorization"' not in text.lower()
    assert "bearer " not in text.lower()
    assert "?sig=" not in text.lower()
    assert '"Signature"' not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert all(
        row["route"] == "<signed-blob-download>"
        for row in rows
        if row["phase"] == "request" and row["operation"] == "blob-download"
    )
