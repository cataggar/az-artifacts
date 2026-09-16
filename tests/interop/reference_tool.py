"""Explicitly gated reference experiments; never imported by the Python library.

Requires the existing ArtifactTool and the locally built diagnostic hook.
Credentials and tool output stay in memory. Capture output is sanitized in-process.
"""

import argparse
import hashlib
import json
import os
import subprocess
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from az_artifacts import BearerToken, NotFoundError, UniversalPackageClient

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "tests" / "fixtures" / "publishing"
HOOK = ROOT / "tests" / "interop" / "capture" / "bin" / "Debug" / "net10.0" / "CaptureHook.dll"


def load_local_proposal(path: str) -> dict:
    proposal_path = Path(path).resolve()
    if proposal_path.is_relative_to(EVIDENCE.resolve()):
        raise RuntimeError("Public fixtures are not live proposals or write authority")
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    if proposal.get("fixture_only"):
        raise RuntimeError("Public fixtures are not live proposals or write authority")
    for key in ("organization", "scope", "feed", "name", "version", "source_directory"):
        if not isinstance(proposal.get(key), str) or not proposal[key]:
            raise RuntimeError(f"Provide an explicit local proposal field: {key}")
    if proposal["scope"] not in ("organization", "project"):
        raise RuntimeError("Proposal scope must be organization or project")
    if proposal["scope"] == "project" and not proposal.get("project"):
        raise RuntimeError("A project-scoped proposal requires an explicit project")
    return proposal


def evidence_directory(proposal: dict) -> Path:
    path = Path(proposal["evidence_directory"]).resolve()
    if not path.is_relative_to((ROOT / ".interop-local").resolve()):
        raise RuntimeError("Live evidence must stay in the ignored .interop-local directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def reference_tool_path(proposal: dict) -> Path:
    path = Path(proposal["artifacttool_path"]).resolve()
    if not path.is_file():
        raise RuntimeError("Provide the path to an existing ArtifactTool")
    return path


def verify_files(source: Path, expected: list[dict[str, object]]) -> None:
    entries = tuple(source.rglob("*"))
    if source.is_symlink() or any(path.is_symlink() for path in entries):
        raise RuntimeError("Reference source must not contain symlinks")
    actual = {path.relative_to(source).as_posix() for path in entries if path.is_file()}
    if actual != {item["path"] for item in expected}:
        raise RuntimeError("Reference file list differs from the reviewed proposal")
    for item in expected:
        path = source / str(item["path"])
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != item["size"] or digest != item["sha256"]:
            raise RuntimeError("Reference file bytes differ from the reviewed proposal")


def check_publish_allowed(proposal: dict[str, object]) -> None:
    if proposal.get("fixture_only"):
        raise RuntimeError("Public fixtures are not live proposals or write authority")
    if proposal.get("publisher", "artifacttool") != "artifacttool":
        raise RuntimeError("This harness cannot publish a native-only proposal")
    if proposal.get("completed") or proposal.get("executed"):
        raise RuntimeError("The immutable reference publish is already completed")
    if proposal.get("approved") is not True:
        raise RuntimeError("Reference publish has not been approved")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight-download", "approved-publish", "download"])
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--execute-approved", action="store_true")
    args = parser.parse_args(argv)
    proposal = load_local_proposal(args.proposal)
    publish = args.action == "approved-publish"
    if publish:
        if not args.execute_approved:
            raise RuntimeError("Reference publishing requires --execute-approved")
        check_publish_allowed(proposal)
    source = ROOT / proposal["source_directory"]
    verify_files(source, proposal["files"])
    tool = reference_tool_path(proposal)
    if not HOOK.is_file():
        raise RuntimeError("Existing ArtifactTool and built capture hook are required")
    token = os.environ.get("AZ_ARTIFACTS_REFERENCE_TOKEN")
    if not token:
        raise RuntimeError("A process-local reference credential is required")
    output = evidence_directory(proposal)
    capture = output / f"{args.action}.jsonl"
    if capture.exists():
        raise RuntimeError(
            "Capture already exists; inspect and reconcile before another invocation"
        )
    env = os.environ.copy()
    env["DOTNET_STARTUP_HOOKS"] = str(HOOK)
    env["AZ_ARTIFACTS_CAPTURE_PATH"] = str(capture)
    env["AZ_ARTIFACTS_CAPTURE_PACKAGE_NAME"] = proposal["name"]
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    if publish:
        with UniversalPackageClient(
            proposal["organization"], credential=BearerToken(token), retries=0
        ) as client:
            try:
                client.get_package_metadata(
                    feed=proposal["feed"],
                    name=proposal["name"],
                    version=proposal["version"],
                    scope=proposal["scope"],
                    project=proposal.get("project"),
                    intent="FetchMetadataOnly",
                )
            except NotFoundError:
                pass
            else:
                raise RuntimeError("Immutable version already exists; do not publish it again")
        with (output / "reference-publish-attempt.json").open("x", encoding="utf-8") as stream:
            json.dump({"state": "attempt started; reconcile before retry"}, stream)
    destination = output / "reference-download"
    command = [
        str(tool),
        "universal",
        "publish" if publish else "download",
        "--service",
        proposal["organization"],
        "--patvar",
        "AZ_ARTIFACTS_REFERENCE_TOKEN",
        "--feed",
        proposal["feed"],
        "--package-name",
        proposal["name"],
        "--package-version",
        proposal["version"],
        "--path",
        str(source if publish else destination),
    ]
    if proposal["scope"] == "project":
        command.extend(["--project", proposal["project"]])
    if publish:
        command.extend(["--description", proposal["description"]])
    # Never persist stdout/stderr: even ordinary SDK errors can include signed URLs.
    result = subprocess.run(command, env=env, capture_output=True, check=False)
    summary: dict[str, object] = {"action": args.action, "exit_code": result.returncode}
    if capture.exists():
        records = [json.loads(line) for line in capture.read_text().splitlines()]
        summary["captured_requests"] = sum(r.get("phase") == "request" for r in records)
        summary["http_status_counts"] = dict(
            Counter(str(r["status"]) for r in records if r.get("phase") == "response")
        )
        capture_text = capture.read_text()
        if token in capture_text:
            capture.unlink()
            raise RuntimeError("Capture rejected by credential leak check")
    if not publish and result.returncode == 0:
        verify_files(destination, proposal["files"])
        summary["download_matches"] = True
    print(json.dumps(summary))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
