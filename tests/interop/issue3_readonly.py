"""Explicitly opt-in, metadata-only issue 3 acceptance against independent fixtures."""

import hashlib
import json
import os
import re
from uuid import UUID

import httpx

from az_artifacts import UniversalPackageClient

if __package__:
    from . import acceptance_support as support
else:
    import acceptance_support as support


FEATURES = (
    "pagination", "stable", "prerelease", "raw-manifest", "chunked-manifest",
    "empty-file", "single-chunk", "multichunk", "multilevel-node", "same-size-different",
    "existing-version", "missing-version", "existing-path", "missing-path", "file-version-missing",
    "compare-version-missing", "compare-path-missing", "history",
    "intent-omitted", "intent-explicit", "limited-count-descriptions",
    "inaccessible", "deleted",
)
ERRORS = {
    "AuthenticationError", "PermissionDeniedError", "NotFoundError",
    "PackageNotFoundError", "ProtocolError", "IntegrityError", "TransportError",
}
ARGUMENTS = {
    "list_feeds": {"max_response_bytes"},
    "list_packages": {"name_query", "page_size"},
    "list_package_versions": {"name", "include_deleted"},
    "package_version_exists": {"name", "version"},
    "get_package_metadata": {"name", "version", "intent"},
    "get_package_versions_metadata": {"name"},
    "list_files": {"name", "version", "file_filter"},
    "file_exists": {"name", "version", "relative_path"},
    "list_file_versions": {"name", "relative_path", "versions"},
    "compare_file": {"name", "version", "relative_path", "local_path"},
}


def baseline_from(root, config):
    baseline = support.read_json(support.private_path(root, config["baseline_file"]))
    support.require(baseline.get("schema") == 1)
    support.require(baseline.get("fixture_only") is False)
    support.require(baseline["provenance"]["kind"] in ("rest", "sdk", "approved-fixture"))
    support.require(baseline["provenance"].get("independent_of_native") is True)
    support.require(baseline["provenance"].get("complete") is True)
    support.require(isinstance(baseline["cases"], list))
    support.require(isinstance(baseline["manifests"], list))
    return baseline


def expected_result(entry):
    modes = sum(key in entry for key in ("expected", "expected_sha256", "error"))
    if not modes:
        raise support.Incomplete("Independent expectation unavailable")
    support.require(modes == 1)
    if "expected_sha256" in entry:
        support.require(entry["request"]["method"] == "list_feeds")
        support.require(isinstance(entry["expected_sha256"], str) and
                        re.fullmatch(r"[0-9a-f]{64}", entry["expected_sha256"]) is not None)
        support.require(type(entry.get("expected_count")) is int and entry["expected_count"] >= 0)
    if "error" in entry:
        support.require(entry["error"] in ERRORS)
        support.require(entry.get("operation") in {
            "discovery", "feeds", "packages", "versions", "metadata",
            "limited-metadata", "resolver", "manifest-or-node",
        })


def hash_local(source, budget):
    checksum = hashlib.sha256()
    size = 0
    with source.open("rb") as stream:
        while data := stream.read(64 * 1024):
            budget.check()
            size += len(data)
            if size > budget.limits["total_bytes"]:
                raise support.Incomplete("Local control exceeds byte budget",
                                         reason=support.Reason.BUDGET)
            checksum.update(data)
    budget.check()
    return checksum.hexdigest()


def prepare_case(root, config, baseline, case, entry, guard):
    method, args = case["method"], dict(case["args"])
    support.require(method in support.METHODS and set(args) <= ARGUMENTS[method])
    addressing = case["addressing"]
    support.require(addressing in ("name", "id"))
    support.require(type(case["target"]) is int and 0 <= case["target"] < len(config["targets"]))
    target = config["targets"][case["target"]]
    support.require(target["scope"] in ("organization", "project"))
    for field in ("feed", *(("project",) if target["scope"] == "project" else ())):
        support.require(set(target[field]) == {"name", "id"})
        support.require(all(isinstance(value, str) and value for value in target[field].values()))
        UUID(target[field]["id"])
        support.require(target[field]["name"] != target[field]["id"])
    for identifier in target["package_ids"].values():
        UUID(identifier)
    expected_result(entry)
    options = support.configure_routes(guard, config, target, addressing, case)
    if method == "list_feeds":
        options = {key: value for key, value in options.items() if key == "project"}
    if method == "list_packages":
        # Deliberately force more than one page without scanning an entire tenant.
        support.require(args.get("page_size") == 1 and bool(args.get("name_query")))
        if "expected" in entry and len(entry["expected"]) < 2:
            raise support.Incomplete("Pagination needs at least two matching entries")
    if method == "list_file_versions":
        # Explicit lists are bounded and make the acceptance snapshot reproducible.
        if not isinstance(args.get("versions"), list) or not args["versions"]:
            raise support.Incomplete("History needs explicit approved exact versions")
        if len(args["versions"]) > guard.budget.limits["items"]:
            raise support.Incomplete("History fixture exceeds budget", reason=support.Reason.BUDGET)
    oracles = []
    if method in ("list_files", "file_exists", "list_file_versions", "compare_file"):
        for index in entry.get("manifests", []):
            support.require(type(index) is int and index >= 0)
            if index >= len(baseline["manifests"]):
                raise support.Incomplete("Independent manifest capture unavailable")
            try:
                oracle = support.ManifestOracle(baseline["manifests"][index], guard.budget)
            except (support.Incomplete, KeyError):
                raise
            except Exception:
                raise support.BoundaryError(
                    "Independent manifest capture mismatch", reason=support.Reason.BASELINE_MISMATCH
                ) from None
            guard.allowed |= oracle.allowed
            oracles.append(oracle)
        if not oracles and entry.get("condition") != "missing-version" and "error" not in entry:
            raise support.Incomplete("Independent manifest capture unavailable")
    control = None
    if method == "compare_file":
        control = entry.get("local_control")
        if not control:
            raise support.Incomplete("Independent local control unavailable")
        source = support.private_path(root, args["local_path"])
        support.require(source.is_file())
        before = source.stat()
        checksum = hash_local(source, guard.budget)
        support.require(before.st_size == control["size"] and checksum == control["sha256"],
                        reason=support.Reason.BASELINE_MISMATCH)
        args["local_path"] = source
        control = (source, before, checksum, control)
    return target, options | args, oracles, control


def observe(method, case, entry, actual, oracles, control, guard):
    features = set()
    oracles = [oracle for oracle in oracles
               if oracle.root in guard.metadata_roots & guard.fetched]
    condition = entry.get("condition")
    if "error" in entry:
        if condition == "inaccessible" and entry["operation"] != "discovery" and entry["error"] in (
            "PermissionDeniedError", "AuthenticationError", "NotFoundError",
        ):
            features.add("inaccessible")
        if condition == "deleted":
            features.add("deleted")
        return features
    if method == "list_packages":
        support.require(len(set(guard.page_offsets)) >= 2 and len(actual) >= 2,
                        reason=support.Reason.REQUEST_BOUNDARY)
        features.add("pagination")
    if method == "list_package_versions":
        if any("-" not in value["version"] for value in actual):
            features.add("stable")
        if any("-" in value["version"] for value in actual):
            features.add("prerelease")
        if (case["args"].get("include_deleted") is True
                and any(value["is_deleted"] is True for value in actual)):
            features.add("deleted")
    if method == "package_version_exists":
        features.add("existing-version" if actual else "missing-version")
    if method == "file_exists":
        if actual:
            features.add("existing-path")
        elif condition == "missing-path":
            features.add("missing-path")
        elif condition == "missing-version":
            features.add("file-version-missing")
    if method == "get_package_metadata":
        features.add("intent-explicit" if case["args"].get("intent") else "intent-omitted")
    if method == "get_package_versions_metadata":
        support.require(type(actual["count"]) is int)
        # A baseline omitting descriptions is insufficient evidence of preservation.
        if (actual["value"] and all("description" in value for value in actual["value"])
                and any(value["description"] for value in actual["value"])):
            features.add("limited-count-descriptions")
    if method == "list_file_versions" and actual:
        features.add("history")
    if method == "list_files":
        if "file_filter" not in case["args"]:
            inventories = [[{key: value for key, value in file.items() if key != "feature"}
                            for file in oracle.files] for oracle in oracles]
            support.require(actual in inventories, reason=support.Reason.BASELINE_MISMATCH)
        for oracle in oracles:
            features |= oracle.features
    if method == "compare_file":
        status = actual["status"]
        if status == "match":
            for oracle in oracles:
                for file in oracle.files:
                    if file["path"] == case["args"]["relative_path"]:
                        support.require(
                            {key: value for key, value in file.items() if key != "feature"} ==
                            actual["file"], reason=support.Reason.BASELINE_MISMATCH,
                        )
                        features.add(file["feature"])
                        features |= oracle.features
        elif status == "different":
            if control[3]["size"] == actual["file"]["size"]:
                features.add("same-size-different")
        elif status == "version_missing":
            features.add("compare-version-missing")
        elif status == "path_missing":
            features.add("compare-path-missing")
    return features


def execute_case(client, method, args, entry, guard):
    error = None
    actual = None
    with support.readonly_filesystem():
        try:
            value = getattr(client, method)(**args)
            if method in ("list_packages", "list_file_versions"):
                value = support.bounded_values(value, guard.budget)
            actual = support.normalized(value)
        except Exception as caught:
            error = caught
    if guard.violated:
        raise support.BoundaryError("Request boundary violated",
                                    reason=support.Reason.REQUEST_BOUNDARY)
    if isinstance(error, (support.Incomplete, support.BoundaryError)):
        raise error
    if error is not None:
        if (type(error).__name__ != entry.get("error")
                or getattr(error, "status_code", None) != entry.get("status_code")
                or not guard.records or guard.records[-1]["case"] != guard.case
                or guard.records[-1]["operation"] != entry.get("operation")):
            raise support.BoundaryError("Unexpected service outcome",
                                        reason=support.reason_code(error)) from None
    elif "expected_sha256" in entry:
        support.require(isinstance(actual, list) and len(actual) == entry["expected_count"],
                        reason=support.Reason.BASELINE_MISMATCH)
        canonical = json.dumps(actual, separators=(",", ":"), ensure_ascii=True, sort_keys=True)
        support.require(hashlib.sha256(canonical.encode("ascii")).hexdigest() ==
                        entry["expected_sha256"], reason=support.Reason.BASELINE_MISMATCH)
    elif "error" in entry or actual != entry["expected"]:
        raise support.BoundaryError("Independent baseline mismatch",
                                    reason=support.Reason.BASELINE_MISMATCH)
    return actual


def run(root, config, baseline, output, *, transport=None):
    limits = support.limits_from(config)
    budget = support.Budget(limits)
    guard = support.GuardTransport(transport or httpx.HTTPTransport(retries=0), budget)
    reports, covered = [], set()
    cases = config["cases"]
    support.require(isinstance(cases, list))
    if len(cases) > limits["cases"]:
        raise support.Incomplete("Case budget exhausted", reason=support.Reason.BUDGET)
    with support.quiet_http_logs(), UniversalPackageClient(
        config["organization"], credential=support.credential(config), retries=0,
        max_workers=1, max_manifest_bytes=limits["manifest_bytes"], transport=guard,
    ) as client:
        for index, case in enumerate(cases):
            case_id = f"case-{index + 1:04d}"
            guard.case = case_id
            record = {"id": case_id, "status": "incomplete", "reason": support.Reason.NOT_RUN}
            reports.append(record)
            try:
                budget.check()
                if index >= len(baseline["cases"]) or baseline["cases"][index] is None:
                    raise support.Incomplete("Case baseline unavailable")
                entry = baseline["cases"][index]
                # Prevent accidental baseline reuse after case reordering/edits.
                support.require(entry["request"] == case, reason=support.Reason.BASELINE_MISMATCH)
                target, args, oracles, control = prepare_case(
                    root, config, baseline, case, entry, guard
                )
                start = len(guard.records)
                actual = execute_case(client, case["method"], args, entry, guard)
                if control:
                    source, before, checksum, _ = control
                    after = source.stat()
                    unchanged = hash_local(source, guard.budget) == checksum
                    support.require(unchanged and (before.st_size, before.st_mtime_ns,
                                                  before.st_ino) ==
                                    (after.st_size, after.st_mtime_ns, after.st_ino),
                                    reason=support.Reason.LOCAL_SOURCE)
                gained = observe(case["method"], case, entry, actual, oracles, control, guard)
                # An expected denial is not evidence that the successful API works.
                if "error" not in entry:
                    covered.add((case["method"], target["scope"], case["addressing"]))
                covered |= gained
                record.update(
                    status="pass", requests=len(guard.records) - start,
                    reason=support.Reason.EXPECTED_ERROR if "error" in entry else support.Reason.OK,
                )
            except (support.Incomplete, FileNotFoundError, KeyError) as error:
                record.update(status="incomplete", reason=support.reason_code(error))
            except Exception as error:
                # Never persist exception text, types supplied by transports, or locals.
                record.update(status="fail", reason=support.reason_code(error))
    for index, method in enumerate(support.METHODS):
        for scope in ("organization", "project"):
            for addressing in ("name", "id"):
                reports.append({
                    "id": f"matrix-{index + 1:02d}-{scope}-{addressing}",
                    "status": "pass" if (method, scope, addressing) in covered else "incomplete",
                    "reason": (support.Reason.OK if (method, scope, addressing) in covered
                               else support.Reason.COVERAGE_GAP),
                })
    for feature in FEATURES:
        reports.append({"id": f"coverage-{feature}",
                        "status": "pass" if feature in covered else "incomplete",
                        "reason": support.Reason.OK if feature in covered
                        else support.Reason.COVERAGE_GAP})
    report = {"schema": 1, "cases": reports, "requests": budget.requests,
              "items": budget.items, "bytes": budget.bytes}
    support.write_report(output, report, guard)
    return 0 if all(row["status"] == "pass" for row in reports) else 1


def main(argv=None):
    parser = support.SafeArgumentParser(description=__doc__)
    parser.add_argument("--private-directory", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--execute-readonly", action="store_true")
    args = parser.parse_args(argv)
    support.require(args.execute_readonly
                    and os.environ.get("AZ_ARTIFACTS_RUN_READONLY_INTEROP") == "1")
    root = support.private_root(args.private_directory)
    config = support.load_config(root, args.config, "issue3-readonly")
    baseline = baseline_from(root, config)
    output = support.private_path(root, config["evidence_directory"], exists=False)
    # Never overwrite evidence; pick a new explicit evidence_directory for each run.
    output.mkdir()
    return run(root, config, baseline, output)


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
