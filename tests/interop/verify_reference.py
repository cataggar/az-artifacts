"""Read-only native verification of the explicitly approved reference package."""

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from az_artifacts import ArtifactsError, BearerToken, UniversalPackageClient
from az_artifacts._dedup import MAX_NODE_BYTES, BlobReader

if __package__:
    from .reference_tool import evidence_directory, load_local_proposal
else:
    from reference_tool import evidence_directory, load_local_proposal

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "publishing"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args()
    proposal = load_local_proposal(args.proposal)
    output = evidence_directory(proposal)
    with UniversalPackageClient(
        proposal["organization"],
        credential=BearerToken(os.environ["AZ_ARTIFACTS_REFERENCE_TOKEN"]),
        retries=3,
    ) as client:
        if args.progress:
            request = client._http.request
            completed = 0

            def counted(*args, **kwargs):
                nonlocal completed
                response = request(*args, **kwargs)
                completed += 1
                if completed % 32 == 0:
                    print(json.dumps({"completed_http_requests": completed}), flush=True)
                return response

            client._http.request = counted
        try:
            result = client.download(
                feed=proposal["feed"],
                name=proposal["name"],
                version=proposal["version"],
                scope=proposal["scope"],
                project=proposal.get("project"),
                path=output / "native-reference-download",
                overwrite=False,
            )
        except ArtifactsError as error:
            print(
                json.dumps(
                    {
                        "native_download": "failed",
                        "error_type": type(error).__name__,
                        "status_code": getattr(error, "status_code", None),
                        "message": str(error),
                    }
                )
            )
            raise SystemExit(1) from None
        verified = []
        for item in proposal["files"]:
            path = result.path / item["path"]
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != item["sha256"] or path.stat().st_size != item["size"]:
                raise RuntimeError("Reference download hash mismatch")
            verified.append({"path": item["path"], "size": path.stat().st_size, "sha256": digest})
        reader = BlobReader(client._http, client.discover_services()["dedup"])
        manifest = reader.manifest(result.metadata.manifest_id, limit=1024 * 1024)
        decoded = json.loads(manifest)
        if sorted(item["path"] for item in decoded["items"]) != sorted(
            "/" + item["path"] for item in proposal["files"]
        ):
            raise RuntimeError("Unexpected content in synthetic package manifest")
        super_root = reader.blob(result.metadata.super_root_id, size=None, limit=MAX_NODE_BYTES)
        (output / "reference-manifest.json").write_bytes(manifest)
        (output / "reference-super-root.bin").write_bytes(super_root)
        verification = {
            "native_download": "verified",
            "metadata": asdict(result.metadata),
            "downloaded_bytes": result.bytes_downloaded,
            "files": verified,
        }
        (output / "reference-verification.json").write_text(
            json.dumps(verification, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(verification))


if __name__ == "__main__":
    main()
