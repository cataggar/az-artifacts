"""Preview, or explicitly execute, the gated cold-content native interop proposal."""

import argparse
import json
import os
from dataclasses import asdict

from az_artifacts import ArtifactsError, BearerToken, PublishRequest, UniversalPackageClient

if __package__:
    from . import reference_tool
    from .native_trace import NativeTrace
else:
    import reference_tool
    from native_trace import NativeTrace


def load_proposal(path=None):
    if path is not None:
        return reference_tool.load_local_proposal(path)
    return json.loads((reference_tool.EVIDENCE / "protocol_evidence.json").read_text())[
        "native_interop_proposal"
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal")
    parser.add_argument("--execute-approved", action="store_true")
    args = parser.parse_args(argv)
    proposal = load_proposal(args.proposal)
    if args.execute_approved:
        if args.proposal is None or proposal.get("fixture_only"):
            raise RuntimeError(
                "Provide a separate local proposal; public fixtures cannot authorize writes"
            )
        if proposal.get("approved") is not True:
            raise RuntimeError("Native remote publishing has not been approved")
        if (
            proposal.get("executed")
            or proposal.get("completed")
            or proposal.get("publisher") != "native-python"
        ):
            raise RuntimeError("Native proposal is completed or has the wrong publisher")
    source = reference_tool.ROOT / proposal["source_directory"]
    if not args.execute_approved:
        print(json.dumps(proposal, indent=2))
        return 0
    reference_tool.verify_files(source, proposal["files"])
    reference_tool.reference_tool_path(proposal)
    if not reference_tool.HOOK.is_file():
        raise RuntimeError("ArtifactTool download reference and capture hook must be ready")
    token = os.environ.get("AZ_ARTIFACTS_REFERENCE_TOKEN")
    if not token:
        raise RuntimeError("A process-local native interoperability credential is required")
    feed = proposal.get("feed_id", proposal["feed"])
    project = proposal.get("project_id", proposal.get("project"))
    output = reference_tool.evidence_directory(proposal)
    attempt = output / "native-publish-attempt.json"
    with attempt.open("x", encoding="utf-8") as stream:
        json.dump(
            {"version": proposal["version"], "state": "attempt started; reconcile before retry"},
            stream,
        )
    try:
        with (
            UniversalPackageClient(
                proposal["organization"], credential=BearerToken(token), retries=3
            ) as client,
            NativeTrace(client._http, output / "native-python-http-1.jsonl") as trace,
        ):
            result = client.publish(
                PublishRequest(
                    feed=feed,
                    name=proposal["name"],
                    version=proposal["version"],
                    path=source,
                    scope=proposal["scope"],
                    project=project,
                    description=proposal["description"],
                )
            )
            publication = {
                "native_publish": "confirmed",
                "metadata": asdict(result.metadata),
                "bytes_uploaded": result.bytes_uploaded,
                "transfer": trace.summary(),
            }
            (output / "native-registration-result.json").write_text(
                json.dumps(publication, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(publication), flush=True)
            downloaded = client.download(
                feed=feed,
                name=proposal["name"],
                version=proposal["version"],
                scope=proposal["scope"],
                project=project,
                path=output / "native-download",
                overwrite=False,
            )
            reference_tool.verify_files(downloaded.path, proposal["files"])
        artifacttool_exit = reference_tool.main(["download", "--proposal", args.proposal])
        if artifacttool_exit:
            raise RuntimeError("ArtifactTool native-package verification failed")
    except ArtifactsError as error:
        print(
            json.dumps(
                {
                    "native_interop": "not confirmed",
                    "error_type": type(error).__name__,
                    "status_code": getattr(error, "status_code", None),
                    "note": "Reconcile the immutable version before any retry.",
                }
            )
        )
        return 1
    verification = {
        "native_publish": "confirmed",
        "metadata": asdict(result.metadata),
        "bytes_uploaded": result.bytes_uploaded,
        "native_download": "all approved sizes and SHA256 hashes verified",
        "artifacttool_download": "all approved sizes and SHA256 hashes verified",
        "files": proposal["files"],
        "transfer": publication["transfer"],
    }
    (output / "native-publish-result.json").write_text(
        json.dumps(verification, indent=2) + "\n", encoding="utf-8"
    )
    attempt.write_text(
        json.dumps(
            {
                "version": proposal["version"],
                "state": "completed; both downloads verified; do not republish",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(verification))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except (ArtifactsError, RuntimeError, OSError):
        print("Native interoperability aborted; inspect the proposal and attempt state.")
        code = 1
    raise SystemExit(code)
