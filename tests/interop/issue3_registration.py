"""Separately approved add_package acceptance: registration only, never upload."""

import base64
import hashlib
import json
import os
from collections import Counter
from uuid import UUID

import httpx

from az_artifacts import ConflictError, PackagePushMetadata, UniversalPackageClient
from az_artifacts._versions import validate_name, version_pattern
from az_artifacts.client import _organization_url

if __package__:
    from . import acceptance_support as support
else:
    import acceptance_support as support


def validate_proposal(proposal):
    support.require(proposal.get("fixture_only") is False)
    support.require(proposal.get("approved") is True)
    support.require(proposal.get("publisher") == "registration-only")
    support.require(proposal.get("disposable_version") is True)
    support.require(proposal.get("preuploaded_references_verified") is True)
    support.require(not proposal.get("completed") and not proposal.get("executed"))
    support.require(type(proposal.get("conflict_probe")) is bool)
    support.require(proposal["scope"] in ("organization", "project"))
    support.require(bool(proposal.get("project")) == (proposal["scope"] == "project"))
    support.require(isinstance(proposal.get("feed"), str) and proposal["feed"])
    UUID(proposal["feed_id"])
    if proposal["scope"] == "project":
        UUID(proposal["project_id"])
    validate_name(proposal["name"])
    support.require(version_pattern(proposal["version"]) is None)
    expected = proposal["expected_metadata"]
    support.require(set(expected) == {
        "version", "manifest_id", "super_root_id", "description", "package_size",
    })
    support.require(expected["version"] == proposal["version"])
    support.require(type(expected["package_size"]) is int and expected["package_size"] >= 0)
    support.require(expected["description"] is None or isinstance(expected["description"], str))
    for field in ("manifest_id", "super_root_id"):
        support.require(support.ID.fullmatch(expected[field]) is not None)
        support.require(expected[field] == expected[field].upper())
    files = proposal["files"]
    support.require(isinstance(files, list) and 0 < len(files) <= proposal["limits"]["items"])
    paths = set()
    for file in files:
        support.require(isinstance(file["path"], str) and file["path"] not in paths)
        support.require(bool(file["path"]))
        paths.add(file["path"])
        support.require(type(file["size"]) is int and file["size"] >= 0)
        support.require(support.ID.fullmatch(file["content_id"]) is not None)
        support.require(file["content_id"] == file["content_id"].upper())
    support.require(support.ENV.fullmatch(proposal["proof_nodes_env"]) is not None)
    support.require(len(proposal["proof_nodes_sha256"]) == 64)
    support.limits_from(proposal)


def proofs_from_environment(proposal):
    value = os.environ.get(proposal["proof_nodes_env"])
    support.require(isinstance(value, str) and len(value) <= 1024 * 1024)
    return validate_proofs(proposal, json.loads(value))


def validate_proofs(proposal, proofs):
    """Bind both the manifest and every represented file to the approved root."""
    support.require(isinstance(proofs, (list, tuple)))
    support.require(0 < len(proofs) <= 512)
    canonical = json.dumps(proofs, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    support.require(hashlib.sha256(canonical).hexdigest() == proposal["proof_nodes_sha256"])
    nodes = {}
    for proof in proofs:
        support.require(isinstance(proof, str))
        data = base64.b64decode(proof, validate=True)
        nodes[support.digest(data, "02")] = support.node_children(data)
    root = proposal["expected_metadata"]["super_root_id"]
    manifest = proposal["expected_metadata"]["manifest_id"]
    support.require(root in nodes and len(nodes[root]) == 2)
    content, manifest_ref = nodes[root]
    support.require(manifest_ref[0] == manifest and content[0].endswith("02"))
    support.require(sum(size for _, size in nodes[root]) ==
                    proposal["expected_metadata"]["package_size"])
    expected = Counter((file["content_id"], file["size"]) for file in proposal["files"])
    represented = Counter()
    remaining = proposal["limits"]["items"]

    def visit(reference, ancestors):
        nonlocal remaining
        remaining -= 1
        support.require(remaining >= 0 and len(ancestors) < 64)
        identifier, size = reference
        support.require(identifier not in ancestors and identifier in nodes)
        children = nodes[identifier]
        support.require(sum(length for _, length in children) == size)
        for child in children:
            # File roots (including multichunk nodes) are terminals here. Proofs
            # cover the package's content tree, not each file's payload tree.
            if child in expected:
                represented[child] += 1
                support.require(represented[child] <= expected[child])
            else:
                visit(child, ancestors | {identifier})

    visit(content, {root})
    support.require(represented == expected)
    return tuple(proofs)


def run(root, proposal, output, proofs, *, transport=None):
    validate_proposal(proposal)
    proofs = validate_proofs(proposal, proofs)
    budget = support.Budget(support.limits_from(proposal))
    guard = support.GuardTransport(transport or httpx.HTTPTransport(retries=0), budget)
    project = [proposal["project"]] if proposal["scope"] == "project" else []
    expected = proposal["expected_metadata"]
    url = support.route(proposal["services"]["packaging"], *project, "_packaging",
                        proposal["feed"], "upack", "packages", proposal["name"],
                        "versions", proposal["version"])
    guard.routes = {
        ("GET", support.route(proposal["organization"], "_apis", "ResourceAreas")): "discovery",
        ("GET", url): "metadata",
    }
    guard.registration_url = url
    guard.intent = "FetchMetadataOnly"
    guard.registration_body = {
        "manifestId": expected["manifest_id"], "superRootId": expected["super_root_id"],
        "proofNodes": list(proofs),
        **({"description": expected["description"]} if expected["description"] is not None else {}),
    }
    metadata = PackagePushMetadata(expected["manifest_id"], expected["super_root_id"],
                                   proofs, expected["description"])
    options = {
        "feed": proposal["feed"], "name": proposal["name"], "version": proposal["version"],
        "scope": proposal["scope"], **({"project": project[0]} if project else {}),
    }
    # Identity, not proposal/output filename: changing output must not replay a PUT.
    _, organization = _organization_url(proposal["organization"])
    identity = [_organization_url(organization.lower())[0], proposal["scope"],
                str(UUID(proposal["project_id"])) if project else "",
                str(UUID(proposal["feed_id"])),
                proposal["name"], proposal["version"]]
    marker_name = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    marker = root / f"registration-attempt-{marker_name}.json"
    # Credential validation precedes reserving an attempt, but no remote preflight
    # grants authority: catalog absence is never permission to reuse a version.
    auth = support.credential(proposal)
    with marker.open("x", encoding="utf-8") as stream:
        json.dump({"state": "attempt-reserved-never-replay"}, stream)
    reports = [{"id": "registration", "status": "incomplete", "reason": support.Reason.NOT_RUN},
               {"id": "readback", "status": "incomplete", "reason": support.Reason.NOT_RUN}]
    if proposal["conflict_probe"]:
        reports.extend([
            {"id": "approved-conflict", "status": "incomplete", "reason": support.Reason.NOT_RUN},
            {"id": "conflict-readback", "status": "incomplete", "reason": support.Reason.NOT_RUN},
        ])
    current = 0
    try:
        with support.quiet_http_logs(), UniversalPackageClient(
            proposal["organization"], credential=auth, transport=guard,
            retries=0, max_workers=1,
        ) as client:
            guard.case = "registration"
            guard.puts_remaining = 1
            with support.readonly_filesystem():
                client.add_package(**options, metadata=metadata)
            support.require(guard.puts == 1 and not guard.violated,
                            reason=support.Reason.REQUEST_BOUNDARY)
            reports[0].update(status="pass", reason=support.Reason.OK)
            current = 1
            guard.case = "readback"
            with support.readonly_filesystem():
                actual = client.get_package_metadata(**options, intent="FetchMetadataOnly")
            support.require(support.normalized(actual) == expected,
                            reason=support.Reason.BASELINE_MISMATCH)
            reports[1].update(status="pass", reason=support.Reason.OK)
            if proposal["conflict_probe"]:
                current = 2
                guard.case = "approved-conflict"
                guard.puts_remaining = 1
                try:
                    with support.readonly_filesystem():
                        client.add_package(**options, metadata=metadata)
                except ConflictError:
                    support.require(guard.puts == 2, reason=support.Reason.REQUEST_BOUNDARY)
                else:
                    raise support.BoundaryError("Conflict was not confirmed",
                                                reason=support.Reason.BASELINE_MISMATCH)
                reports[2].update(status="pass", reason=support.Reason.EXPECTED_ERROR)
                current = 3
                guard.case = "conflict-readback"
                with support.readonly_filesystem():
                    actual = client.get_package_metadata(**options, intent="FetchMetadataOnly")
                support.require(support.normalized(actual) == expected,
                                reason=support.Reason.BASELINE_MISMATCH)
                reports[3].update(status="pass", reason=support.Reason.OK)
    except support.Incomplete as error:
        reports[current].update(status="incomplete", reason=support.reason_code(error))
    except Exception as error:
        # No reconciliation-success fallback, even if a lost response committed.
        reports[current].update(status="fail", reason=support.reason_code(error))
    report = {"schema": 1, "cases": reports, "registration_attempts": guard.puts}
    support.write_report(output, report, guard)
    return 0 if all(row["status"] == "pass" for row in reports) else 1


def main(argv=None):
    parser = support.SafeArgumentParser(description=__doc__)
    parser.add_argument("--private-directory", required=True)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--execute-approved", action="store_true")
    parser.add_argument("--proposal-sha256")
    args = parser.parse_args(argv)
    root = support.private_root(args.private_directory)
    path = support.private_path(root, args.proposal)
    if args.execute_approved:
        support.require(os.environ.get("AZ_ARTIFACTS_RUN_REGISTRATION_INTEROP") == "1")
        support.require(isinstance(args.proposal_sha256, str))
    proposal = support.load_config(
        root, path, "issue3-registration",
        expected_sha256=args.proposal_sha256 if args.execute_approved else None,
    )
    if not args.execute_approved:
        # Preview never prints destination, version, references, or opaque proofs.
        print(json.dumps({"registration": "preview-only", "network_requests": 0}))
        return 0
    validate_proposal(proposal)
    proofs = proofs_from_environment(proposal)
    output = support.private_path(root, proposal["evidence_directory"], exists=False)
    output.mkdir()
    return run(root, proposal, output, proofs)


def cli(argv=None):
    try:
        return main(argv)
    except Exception as error:
        print(json.dumps({
            "status_counts": {"incomplete": 1}, "reason": support.reason_code(error),
        }))
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
