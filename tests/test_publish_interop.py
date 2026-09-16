"""Explicitly gated live native-publish interoperability, disabled by default."""

import json
import os
from pathlib import Path

import pytest
from interop import native_publish


@pytest.mark.parametrize("approval", [False, "false", "true", 1])
def test_native_write_gate_precedes_credentials_and_network(monkeypatch, approval):
    monkeypatch.setattr(native_publish, "load_proposal", lambda path: {"approved": approval})
    monkeypatch.delenv("AZ_ARTIFACTS_REFERENCE_TOKEN", raising=False)
    monkeypatch.setattr(
        native_publish,
        "UniversalPackageClient",
        lambda *args, **kwargs: pytest.fail("Unapproved publisher must not be constructed"),
    )
    with pytest.raises(RuntimeError, match="has not been approved"):
        native_publish.main(["--execute-approved", "--proposal", "local.json"])


def test_native_proposal_uses_cold_payload_and_unchanged_control_files():
    path = Path(__file__).parent / "fixtures" / "publishing" / "protocol_evidence.json"
    evidence = json.loads(path.read_text())
    native = evidence["native_interop_proposal"]
    reference = evidence["next_reference_experiment"]
    assert native["files"][0] == {
        "path": "payload.bin",
        "size": 104857600,
        "sha256": "514dd8bcb18fa0afb5b93eaffa7a636cf943e54638ae7b56c2a10b03481b14f5",
    }
    assert native["files"][0]["sha256"] != reference["files"][0]["sha256"]
    assert native["files"][1:] == reference["files"][1:]
    assert native["source_directory"] == ".reference-proposal/0.0.3-native.20000101"
    assert native["source_directory"] != reference["source_directory"]
    assert "az-artifacts native interoperability v1" in native["payload_algorithm"]
    assert native["total_bytes"] == reference["total_bytes"]
    assert native["version"] not in (
        reference["version"],
        evidence["reference_publish_proposal"]["version"],
    )


def test_native_preview_selects_cold_source_without_network(monkeypatch, capsys):
    checked = []
    proposal = native_publish.load_proposal()
    monkeypatch.setattr(
        native_publish.reference_tool,
        "verify_files",
        lambda source, files: checked.append((source, files)),
    )
    monkeypatch.setattr(
        native_publish,
        "UniversalPackageClient",
        lambda *args, **kwargs: pytest.fail("Preview must not construct a network client"),
    )
    assert native_publish.main([]) == 0
    assert checked == []
    preview = json.loads(capsys.readouterr().out)
    assert preview["source_directory"] == proposal["source_directory"]
    assert preview["files"][0]["sha256"] == proposal["files"][0]["sha256"]


@pytest.mark.skipif(
    os.environ.get("AZ_ARTIFACTS_RUN_APPROVED_NATIVE_INTEROP") != "1",
    reason="Requires separate exact proposal approval and explicit live-write opt-in",
)
def test_approved_native_publish_then_both_downloaders():
    proposal = os.environ["AZ_ARTIFACTS_NATIVE_PROPOSAL"]
    assert native_publish.main(["--execute-approved", "--proposal", proposal]) == 0


def test_public_native_fixture_cannot_authorize_writes(monkeypatch):
    monkeypatch.setattr(
        native_publish,
        "UniversalPackageClient",
        lambda *args, **kwargs: pytest.fail("Public fixture must not construct a client"),
    )
    with pytest.raises(RuntimeError, match="separate local proposal"):
        native_publish.main(["--execute-approved"])


def test_copied_public_fixture_is_not_a_local_approval(tmp_path):
    proposal = {**native_publish.load_proposal(), "approved": True, "completed": False}
    path = tmp_path / "copied-fixture.json"
    path.write_text(json.dumps(proposal), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Public fixtures"):
        native_publish.load_proposal(str(path))


@pytest.mark.parametrize("scope,project", [("organization", None), ("project", "fixture-project")])
def test_separate_local_proposal_requires_explicit_destination(tmp_path, scope, project):
    proposal = {
        "organization": "https://dev.azure.com/fixtureorg",
        "scope": scope,
        "project": project,
        "feed": "fixture-feed",
        "name": "fixture-package",
        "version": "1.0.0",
        "source_directory": str(tmp_path / "source"),
    }
    path = tmp_path / "local-proposal.json"
    path.write_text(json.dumps(proposal), encoding="utf-8")
    assert native_publish.load_proposal(str(path)) == proposal
    proposal.pop("organization")
    path.write_text(json.dumps(proposal), encoding="utf-8")
    with pytest.raises(RuntimeError, match="explicit local proposal field: organization"):
        native_publish.load_proposal(str(path))


def test_live_evidence_cannot_overwrite_public_fixtures():
    with pytest.raises(RuntimeError, match="ignored .interop-local"):
        native_publish.reference_tool.evidence_directory(
            {"evidence_directory": str(native_publish.reference_tool.EVIDENCE)}
        )
