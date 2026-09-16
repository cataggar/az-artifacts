"""Explicitly gated content/retention diagnosis; package registration is prohibited."""

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from az_artifacts import ArtifactsError, BearerToken, UniversalPackageClient
from az_artifacts._prepare import PreparedPackage
from az_artifacts._upload import Uploader

if __package__:
    from .native_trace import NativeTrace
    from .reference_tool import evidence_directory, load_local_proposal, verify_files
else:
    from native_trace import NativeTrace
    from reference_tool import evidence_directory, load_local_proposal, verify_files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--execute-approved", action="store_true")
    parser.add_argument("--node-id")
    args = parser.parse_args(argv)
    proposal = load_local_proposal(args.proposal)
    if (
        not args.execute_approved
        or proposal.get("approved") is not True
        or proposal.get("publisher") != "native-retention-only"
        or proposal.get("completed")
        or proposal.get("executed")
    ):
        raise RuntimeError(
            "Retention writes require a separately approved local proposal and opt-in"
        )
    output = evidence_directory(proposal)
    capture = output / args.capture
    if capture.parent != output or capture.exists():
        raise RuntimeError("Use a new capture filename inside the publishing evidence directory")
    source = Path(proposal["source_directory"]).resolve()
    verify_files(source, proposal["files"])
    prepared = PreparedPackage(source, proposal["version"], max_bytes=64 * 1024 * 1024)
    graph = {
        "metadata": asdict(prepared.metadata),
        "source_files": proposal["files"],
        "nodes": {
            key: {"size": node.ref.size, "children": [asdict(child) for child in node.children]}
            for key, node in prepared.nodes.items()
        },
    }
    capture.with_suffix(".graph.json").write_text(json.dumps(graph, indent=2) + "\n")
    with UniversalPackageClient(
        proposal["organization"],
        credential=BearerToken(os.environ["AZ_ARTIFACTS_REFERENCE_TOKEN"]),
    ) as client:
        original = client._http._request_once

        def dedup_only(method, url, **kwargs):
            path = urlsplit(url).path
            if method not in ("GET", "HEAD", "OPTIONS"):
                allowed = (
                    method == "PUT"
                    and (path.endswith("/_apis/dedup/chunks") or "/_apis/dedup/nodes/" in path)
                ) or (method == "POST" and path.endswith("/_apis/dedup/urls"))
                if not allowed:
                    raise RuntimeError("Registration and all non-dedup writes are disabled")
            return original(method, url, **kwargs)

        client._http._request_once = dedup_only
        with NativeTrace(client._http, capture, response_shapes=True) as trace:
            uploader = Uploader(
                client._http,
                client._upload_location(client.discover_services()["dedup"]),
                prepared,
                max_workers=4,
            )
            try:
                if args.node_id:
                    uploader._node(args.node_id)
                else:
                    uploader.upload()
                    prepared.verify_sources()
                result = {
                    "retention": "completed",
                    "super_root_ready": uploader._ready(prepared.super_root.id),
                    "metadata": asdict(prepared.metadata),
                }
                code = 0
            except ArtifactsError as error:
                result = {
                    "retention": "failed",
                    "error_type": type(error).__name__,
                    "message": str(error),
                }
                code = 1
            result["registration_disabled"] = True
            result["required_keep_until"] = uploader.keep_until.isoformat()
            result["transfer"] = trace.summary()
            capture.with_suffix(".result.json").write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(result), flush=True)
            return code


if __name__ == "__main__":
    try:
        code = main()
    except Exception as error:
        print(json.dumps({"diagnosis": "stopped", "error_type": type(error).__name__}))
        code = 1
    raise SystemExit(code)
