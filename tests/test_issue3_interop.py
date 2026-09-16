"""Offline acceptance-harness tests; all remote responses and identities are synthetic."""

import base64
import copy
import gzip
import hashlib
import io
import json
import logging
import os
import struct

import httpx
import pytest
from interop import acceptance_support as support
from interop import issue3_example, issue3_readonly, issue3_registration


def wire(value):
    if isinstance(value, dict):
        return {
            key.split("_")[0] + "".join(part.title() for part in key.split("_")[1:]): wire(child)
            for key, child in value.items() if child is not None
        }
    if isinstance(value, list):
        return [wire(child) for child in value]
    return value


class SyntheticService:
    def __init__(self, config, baseline):
        self.config = config
        self.baseline = baseline
        self.requests = []
        self.metadata = {}
        self.registration_status = 204
        self.registration_metadata = None
        for entry in baseline["cases"]:
            if entry["request"]["method"] == "get_package_metadata":
                self.metadata[entry["expected"]["version"]] = entry["expected"]
        # Raw independent fixture data, not results returned by the native client.
        self.metadata["1.1.0-preview.1"] = {
            **self.metadata["1.0.0"], "version": "1.1.0-preview.1",
            "description": "Synthetic prerelease",
            "manifest_id": baseline["manifests"][1]["manifest_id"],
        }
        self.blobs = {key: base64.b64decode(value) for manifest in baseline["manifests"]
                      for key, value in manifest["blobs"].items()}

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "blob.example":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=self.blobs[path.strip("/")])
        if path.endswith("/ResourceAreas"):
            return httpx.Response(200, json={"value": [
                {"name": name, "locationUrl": self.config["services"][key]}
                for name, key in (("Feed", "feeds"), ("Packaging", "packaging"), ("Dedup", "dedup"))
            ]})
        if path.endswith("/dedup/urls"):
            return httpx.Response(200, json={
                identifier: f"https://blob.example/{identifier}?sig=synthetic-secret"
                for identifier in json.loads(request.content)
            })
        if "/upack/packages/" in path:
            if request.method == "PUT":
                return httpx.Response(self.registration_status)
            if path.endswith("/versions"):
                return httpx.Response(200, json=self.baseline["cases"][5]["expected"])
            if self.registration_metadata is not None:
                return httpx.Response(200, json=wire(self.registration_metadata))
            version = path.rsplit("/", 1)[-1]
            if version not in self.metadata:
                return httpx.Response(404)
            return httpx.Response(200, json=wire(self.metadata[version]))
        if path.endswith("/Feeds"):
            project = "/fixture-project/" in path or f"/{issue3_example.PROJECT_ID}/" in path
            return httpx.Response(200, json={
                "count": 1, "value": wire(self.baseline["cases"][20 if project else 0]["expected"]),
            })
        if path.endswith("/packages"):
            values = wire(self.baseline["cases"][1]["expected"])
            start, size = int(request.url.params["$skip"]), int(request.url.params["$top"])
            values = values[start:start + size]
            return httpx.Response(200, json={"count": len(values), "value": values})
        if path.endswith("/versions"):
            values = wire(self.baseline["cases"][2]["expected"])
            return httpx.Response(200, json={"count": len(values), "value": values})
        raise AssertionError("Unexpected synthetic route")


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    config, baseline, sources = issue3_example.example()
    config["fixture_only"] = False
    config["credential_kind"] = "pat"
    (tmp_path / "sources").mkdir()
    for name, value in sources.items():
        (tmp_path / "sources" / name).write_bytes(value)
    monkeypatch.setenv(config["credential_env"], "synthetic-token-not-a-credential")
    output = tmp_path / "evidence"
    output.mkdir()
    return tmp_path, config, baseline, output, SyntheticService(config, baseline)


def execute(acceptance):
    root, config, baseline, output, service = acceptance
    code = issue3_readonly.run(root, config, baseline, output,
                              transport=httpx.MockTransport(service))
    report = json.loads((output / "report.json").read_text())
    return code, {row["id"]: row["status"] for row in report["cases"]}


def case_report(acceptance, identifier):
    report = json.loads((acceptance[3] / "report.json").read_text())
    return next(row for row in report["cases"] if row["id"] == identifier)


@pytest.mark.parametrize("status", [200, 206])
def test_compressed_response_decodes_once_preserving_other_header_semantics(status):
    data = b'{"value":[],"count":0}'
    compressed = gzip.compress(data)
    url = "https://feeds.dev.azure.com/fixtureorg/_apis/packaging/Feeds"
    original = httpx.Response(status, stream=httpx.ByteStream(compressed), headers=[
        ("Content-Encoding", "gzip"), ("Content-Length", str(len(compressed))),
        ("Content-Type", "application/json"), ("ETag", "synthetic-etag"),
        ("X-MS-ContinuationToken", "synthetic-continuation"),
        ("Content-Range", "items 0-0/2"), ("Operation-Location", "https://example.test/operation"),
        ("Location", "https://example.test/redirect"), ("X-Synthetic", "first"),
        ("X-Synthetic", "second"),
    ])
    guard = support.GuardTransport(httpx.MockTransport(lambda _: original),
                                   support.Budget(issue3_example.LIMITS))
    guard.routes["GET", url] = "feeds"
    with httpx.Client(transport=guard) as client:
        response = client.get(url)
    assert response.content == data
    assert "content-encoding" not in response.headers
    assert response.headers["content-length"] == str(len(data))
    assert [(key, value) for key, value in response.headers.raw
            if key.lower() != b"content-length"] == [
                (key, value) for key, value in original.headers.raw
                if key.lower() not in (b"content-encoding", b"content-length")
            ]
    assert original.headers["content-encoding"] == "gzip"
    assert original.is_closed
    assert guard.budget.bytes == len(data)
    assert guard.records[0]["bytes"] == len(data)


@pytest.mark.parametrize("limit", ["response_bytes", "total_bytes"])
def test_compression_does_not_bypass_decoded_byte_budgets(limit):
    data = json.dumps({"value": [], "count": 0, "padding": "x" * 10000}).encode()
    compressed = gzip.compress(data)
    assert len(compressed) < 128 < len(data)
    response = httpx.Response(200, stream=httpx.ByteStream(compressed),
                              headers={"content-encoding": "gzip"})
    guard = support.GuardTransport(
        httpx.MockTransport(lambda _: response),
        support.Budget({**issue3_example.LIMITS, limit: 128}),
    )
    url = "https://feeds.dev.azure.com/fixtureorg/_apis/packaging/Feeds"
    guard.routes["GET", url] = "feeds"
    with pytest.raises(support.Incomplete) as caught:
        guard.handle_request(httpx.Request("GET", url))
    assert support.reason_code(caught.value) == support.Reason.BUDGET
    assert response.is_closed


def test_full_synthetic_matrix_with_http_gzip_including_manifest_and_node_reads(acceptance):
    service = acceptance[4]

    def compressed_response(request):
        response = service(request)
        data = gzip.compress(response.content)
        headers = httpx.Headers(response.headers)
        headers["content-encoding"] = "gzip"
        headers["content-length"] = str(len(data))
        return httpx.Response(response.status_code, headers=headers, stream=httpx.ByteStream(data))

    code, statuses = execute((*acceptance[:4], compressed_response))
    assert code == 1
    assert {key for key, value in statuses.items() if value != "pass"} == {
        "coverage-inaccessible", "coverage-deleted",
    }


def test_full_synthetic_matrix_and_explicit_unavailable_gaps(acceptance, capsys):
    code, statuses = execute(acceptance)
    assert code == 1
    assert {key for key, value in statuses.items() if value != "pass"} == {
        "coverage-inaccessible", "coverage-deleted",
    }
    output = acceptance[3]
    persisted = "".join(path.read_text() for path in output.iterdir()) + capsys.readouterr().out
    for secret in ("fixtureorg", "fixture-package", "single.bin", "sig=", "synthetic-secret",
                   "synthetic-token", issue3_example.FEED_ID, "Authorization", "https://"):
        assert secret not in persisted
    records = json.loads((output / "requests.json").read_text())
    assert any(record["operation"] == "manifest-or-node" for record in records)
    assert all(record["method"] in ("GET", "POST") for record in records)
    assert all(record["operation"] == "resolver" for record in records
               if record["method"] == "POST")
    reports = json.loads((output / "report.json").read_text())["cases"]
    assert all(row["reason"] in support.Reason for row in reports)
    assert case_report(acceptance, "case-0001")["reason"] == "ok"
    assert case_report(acceptance, "coverage-inaccessible")["reason"] == "coverage-gap"


def test_http_logging_cannot_persist_signed_urls(acceptance, caplog):
    caplog.set_level(logging.DEBUG)
    execute(acceptance)
    assert "sig=" not in caplog.text
    assert "fixtureorg" not in caplog.text


def test_unrecognized_cli_values_are_not_echoed(capsys):
    assert issue3_readonly.cli(["--unexpected", "synthetic-secret"]) == 1
    assert issue3_registration.cli(["--unexpected", "synthetic-secret"]) == 1
    assert "synthetic-secret" not in capsys.readouterr().out


def test_missing_baseline_never_becomes_skip_or_pass(acceptance):
    acceptance[2]["cases"][0] = None
    _, statuses = execute(acceptance)
    assert statuses["case-0001"] == "incomplete"
    assert statuses["matrix-01-organization-name"] == "incomplete"
    assert case_report(acceptance, "case-0001")["reason"] == "missing-fixture"


@pytest.mark.parametrize("index", [6, 9])
def test_unused_manifest_cannot_receive_coverage_credit(acceptance, index):
    config, baseline = acceptance[1], acceptance[2]
    service = SyntheticService(copy.deepcopy(config), copy.deepcopy(baseline))
    config["cases"] = [config["cases"][index]]
    entry = baseline["cases"][index]
    entry["manifests"] = [0, 1]
    baseline["cases"] = [entry]
    _, statuses = execute((*acceptance[:4], service))
    assert statuses["case-0001"] == "pass"
    assert statuses["coverage-raw-manifest"] == "pass"
    assert statuses["coverage-chunked-manifest"] == "incomplete"


def test_inventory_without_manifest_requests_cannot_establish_coverage(acceptance, monkeypatch):
    config, baseline = acceptance[1], acceptance[2]
    config["cases"] = [config["cases"][6]]
    baseline["cases"] = [baseline["cases"][6]]
    monkeypatch.setattr(
        issue3_readonly.UniversalPackageClient, "list_files",
        lambda *args, **kwargs: baseline["cases"][0]["expected"],
    )
    _, statuses = execute(acceptance)
    assert statuses["case-0001"] == "fail"
    assert statuses["coverage-raw-manifest"] == "incomplete"
    assert case_report(acceptance, "case-0001")["reason"] == "baseline-mismatch"


def test_each_case_resets_observed_manifest_roots(acceptance):
    config = acceptance[1]
    guard = support.GuardTransport(httpx.MockTransport(lambda _: pytest.fail("No network")),
                                   support.Budget(issue3_example.LIMITS))
    guard.metadata_roots.add("AA" * 32 + "01")
    guard.fetched.add("AA" * 32 + "01")
    support.configure_routes(guard, config, config["targets"][0], "name", config["cases"][6])
    assert not guard.metadata_roots and not guard.fetched


@pytest.mark.parametrize("body", [
    b"{}", b'{"manifestId":null}', b'{"manifestId":42}',
    b'{"manifestId":"invalid"}', b"[]", b"invalid-json", b"\xff",
])
def test_metadata_observation_preserves_expected_native_protocol_errors(acceptance, body):
    config, baseline, service = acceptance[1], acceptance[2], acceptance[4]
    case = config["cases"][4]
    config["cases"] = [case]
    baseline["cases"] = [{
        "request": case, "error": "ProtocolError", "operation": "metadata",
    }]

    def response(request):
        if "/upack/packages/" in request.url.path:
            return httpx.Response(200, content=body)
        return service(request)

    _, statuses = execute((*acceptance[:4], response))
    assert statuses["case-0001"] == "pass"
    assert case_report(acceptance, "case-0001")["reason"] == "expected-error"
    assert statuses["coverage-raw-manifest"] == "incomplete"
    assert statuses["coverage-chunked-manifest"] == "incomplete"


def test_missing_required_project_fixture_is_incomplete(acceptance):
    acceptance[1]["targets"][1].pop("project")
    _, statuses = execute(acceptance)
    assert statuses["case-0021"] == "incomplete"
    assert statuses["matrix-01-project-name"] == "incomplete"


def test_baseline_request_binding_rejects_reordered_case(acceptance):
    acceptance[2]["cases"][0] = copy.deepcopy(acceptance[2]["cases"][1])
    _, statuses = execute(acceptance)
    assert statuses["case-0001"] == "fail"
    assert case_report(acceptance, "case-0001")["reason"] == "baseline-mismatch"


@pytest.mark.parametrize("change", ["none", "count", "description", "order", "missing-field"])
def test_digest_only_feed_oracle_preserves_all_data_without_recording_it(
    acceptance, capsys, change,
):
    root, config, baseline, output, service = acceptance
    case = config["cases"][0]
    feeds = copy.deepcopy(baseline["cases"][0]["expected"])
    secret = "https://example.test/feed?sig=synthetic-feed-secret#fragment"
    feeds[0]["description"] = secret
    feeds.append({**feeds[0], "id": issue3_example.OTHER_ID, "name": "other-feed"})
    expected = copy.deepcopy(feeds)
    if change == "missing-field":
        expected[0].pop("deleted_date")
    entry = {
        "request": case, "expected_count": len(feeds) + (change == "count"),
        "expected_sha256": hashlib.sha256(json.dumps(
            expected, separators=(",", ":"), ensure_ascii=True, sort_keys=True,
        ).encode("ascii")).hexdigest(),
    }
    config["cases"] = [case]
    baseline["cases"] = [entry]
    baseline["fixture_only"] = False
    (root / config["baseline_file"]).write_text(json.dumps(baseline))
    loaded = issue3_readonly.baseline_from(root, config)
    if change == "description":
        feeds[0]["description"] += "-changed"
    elif change == "order":
        feeds.reverse()

    def response(request):
        if request.url.path.endswith("/Feeds"):
            return httpx.Response(200, json={"count": len(feeds), "value": wire(feeds)})
        return service(request)

    _, statuses = execute((root, config, loaded, output, response))
    assert statuses["case-0001"] == ("pass" if change == "none" else "fail")
    persisted = "".join(path.read_text() for path in output.iterdir()) + capsys.readouterr().out
    assert secret not in persisted and "synthetic-feed-secret" not in persisted
    assert secret not in (root / config["baseline_file"]).read_text()


@pytest.mark.parametrize("mode", ["other-method", "mixed", "bad-digest", "missing-count"])
def test_digest_oracle_requires_exclusive_complete_feed_expectation(acceptance, mode):
    config, baseline, service = acceptance[1], acceptance[2], acceptance[4]
    case = config["cases"][4 if mode == "other-method" else 0]
    entry = {"request": case, "expected_sha256": "a" * 64, "expected_count": 0}
    if mode == "mixed":
        entry["expected"] = []
    elif mode == "bad-digest":
        entry["expected_sha256"] = "invalid"
    elif mode == "missing-count":
        entry.pop("expected_count")
    config["cases"] = [case]
    baseline["cases"] = [entry]
    _, statuses = execute(acceptance)
    assert statuses["case-0001"] == "fail"
    assert not service.requests


@pytest.mark.parametrize("limit,value", [("requests", 1), ("items", 1), ("total_bytes", 1)])
def test_global_caps_report_incomplete(acceptance, limit, value):
    acceptance[1]["limits"][limit] = value
    code, statuses = execute(acceptance)
    assert code == 1
    assert "incomplete" in statuses.values()
    assert not any(record.method in ("PUT", "PATCH", "DELETE") for record in acceptance[4].requests)
    reports = json.loads((acceptance[3] / "report.json").read_text())["cases"]
    assert any(row["reason"] == "budget-exhausted" for row in reports)


def test_deadline_prevents_next_request():
    budget = support.Budget(issue3_example.LIMITS)
    budget.deadline = 0
    with pytest.raises(support.Incomplete):
        support.GuardTransport(httpx.MockTransport(lambda _: pytest.fail("No network")),
                               budget).handle_request(httpx.Request("GET", "https://blob.example/x"))


def test_short_catalog_is_incomplete_not_pagination_pass(acceptance):
    acceptance[2]["cases"][1]["expected"] = acceptance[2]["cases"][1]["expected"][:1]
    _, statuses = execute(acceptance)
    assert statuses["case-0002"] == "incomplete"


def test_limited_metadata_preserves_server_count_not_length(acceptance):
    for entry in acceptance[2]["cases"]:
        if entry["request"]["method"] == "get_package_versions_metadata":
            entry["expected"]["count"] = 7
    _, statuses = execute(acceptance)
    assert statuses["coverage-limited-count-descriptions"] == "pass"
    records = json.loads((acceptance[3] / "requests.json").read_text())
    assert any(row.get("count") == 7 and row.get("items") == 2 for row in records)


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_service_failure_is_not_absence(acceptance, status):
    service = acceptance[4]
    original = service.__call__

    def denied(request):
        if request.url.path.endswith("/packages"):
            return httpx.Response(status)
        return original(request)

    changed = (*acceptance[:4], denied)
    _, statuses = execute(changed)
    assert statuses["case-0004"] == "fail"
    assert statuses["coverage-missing-version"] == "incomplete"
    assert case_report(acceptance, "case-0004")["reason"] == {
        401: "authentication-error", 403: "permission-error",
        404: "not-found", 500: "service-error",
    }[status]


def test_expected_denial_does_not_fill_success_matrix(acceptance):
    config, baseline = acceptance[1:3]
    case = {"target": 0, "addressing": "name", "method": "package_version_exists",
            "args": {"name": "fixture-package", "version": "1.0.0"}}
    config["cases"] = [case]
    baseline["cases"] = [{"request": case, "error": "PermissionDeniedError",
                          "status_code": 403, "condition": "inaccessible", "operation": "packages"}]
    service = acceptance[4]

    def denied(request):
        if request.url.path.endswith("/packages"):
            return httpx.Response(403)
        return service(request)

    code, statuses = execute((*acceptance[:4], denied))
    assert code == 1
    assert statuses["case-0001"] == "pass"
    assert statuses["coverage-inaccessible"] == "pass"
    assert statuses["matrix-04-organization-name"] == "incomplete"
    assert case_report(acceptance, "case-0001")["reason"] == "expected-error"


def test_manifest_oracle_independently_distinguishes_manifest_leaves_and_file_payloads():
    _, baseline, _ = issue3_example.example()
    oracle = support.ManifestOracle(baseline["manifests"][1],
                                    support.Budget(issue3_example.LIMITS))
    assert oracle.root in oracle.allowed
    assert len(oracle.allowed) > len(oracle.file_nodes) + 1
    single = next(file for file in oracle.files if file["path"] == "single.bin")
    assert single["content_id"] not in oracle.allowed
    guard = support.GuardTransport(httpx.MockTransport(lambda _: pytest.fail("Payload blocked")),
                                   support.Budget(issue3_example.LIMITS))
    guard.allowed = oracle.allowed
    url = "https://vsblob.dev.azure.com/fixtureorg/_apis/dedup/urls"
    guard.routes["POST", url] = "resolver"
    with pytest.raises(support.BoundaryError):
        guard.handle_request(httpx.Request("POST", url, json=[single["content_id"]]))
    assert guard.violated


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "POST"])
def test_arbitrary_writes_forbidden_before_transport(method):
    guard = support.GuardTransport(httpx.MockTransport(lambda _: pytest.fail("No write")),
                                   support.Budget(issue3_example.LIMITS))
    with pytest.raises(support.BoundaryError):
        guard.handle_request(httpx.Request(method, "https://pkgs.dev.azure.com/fixtureorg/x"))


def test_redirects_are_traced_and_bounded_without_persisting_urls():
    count = 0

    def redirected(request):
        nonlocal count
        count += 1
        return httpx.Response(302, headers={"location": "https://other.example/a?sig=secret"})

    guard = support.GuardTransport(httpx.MockTransport(redirected),
                                   support.Budget({**issue3_example.LIMITS, "requests": 1}))
    guard.urls["https://blob.example/a?sig=secret"] = "A" * 64 + "01"
    guard.handle_request(httpx.Request("GET", "https://blob.example/a?sig=secret"))
    with pytest.raises(support.Incomplete):
        guard.handle_request(httpx.Request("GET", "https://other.example/a?sig=secret"))
    assert count == 1
    assert "secret" not in json.dumps(guard.records)


@pytest.mark.parametrize("action", ["write", "mkdir", "utime", "remove"])
def test_filesystem_mutations_blocked_and_source_unchanged(tmp_path, action):
    source = tmp_path / "control"
    source.write_bytes(b"abc")
    with pytest.raises(support.BoundaryError), support.readonly_filesystem():
        if action == "write":
            source.write_bytes(b"xyz")
        elif action == "mkdir":
            (tmp_path / "output").mkdir()
        elif action == "utime":
            os.utime(source, None)
        else:
            source.unlink()
    assert source.read_bytes() == b"abc"
    assert not (tmp_path / "output").exists()


def test_missing_manifest_node_is_incomplete():
    _, baseline, _ = issue3_example.example()
    entry = baseline["manifests"][0]
    entry["blobs"].pop(next(key for key in entry["blobs"] if key.endswith("02")))
    with pytest.raises(support.Incomplete):
        support.ManifestOracle(entry, support.Budget(issue3_example.LIMITS))


def test_corrupt_independent_manifest_has_baseline_mismatch_reason(acceptance):
    entry = acceptance[2]["manifests"][0]
    entry["blobs"][entry["manifest_id"]] = base64.b64encode(b"invalid-fixture").decode()
    _, statuses = execute(acceptance)
    assert statuses["case-0007"] == "fail"
    assert case_report(acceptance, "case-0007")["reason"] == "baseline-mismatch"


def test_corrupt_remote_manifest_has_protocol_reason(acceptance):
    entry = acceptance[2]["manifests"][0]
    acceptance[4].blobs[entry["manifest_id"]] = b"invalid-remote-content"
    _, statuses = execute(acceptance)
    assert statuses["case-0007"] == "fail"
    assert case_report(acceptance, "case-0007")["reason"] == "protocol-error"


def test_file_payload_capture_in_baseline_is_rejected():
    _, baseline, _ = issue3_example.example()
    entry = baseline["manifests"][0]
    entry["blobs"][support.digest(b"abc")] = base64.b64encode(b"abc").decode()
    with pytest.raises(support.BoundaryError):
        support.ManifestOracle(entry, support.Budget(issue3_example.LIMITS))


@pytest.mark.parametrize("value", [
    {"token": "secret"}, {"headers": {}}, {"receipts": {}}, {"proofNodes": ["secret"]},
    {"url": "https://blob.example/a?sig=secret"}, {"url": "https://user:pass@example.test"},
])
def test_raw_credential_and_capability_inputs_rejected(value):
    with pytest.raises(support.BoundaryError):
        support.clean_input(value)


def test_private_artifacts_cannot_be_in_git_or_escape_root(tmp_path):
    with pytest.raises(support.BoundaryError):
        support.private_root(str(support.ROOT))
    with pytest.raises(support.BoundaryError):
        support.private_path(tmp_path, "..\\escape.json", exists=False)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: fixture")
    with pytest.raises(support.BoundaryError):
        support.private_path(tmp_path, "nested\\evidence", exists=False)


def test_copied_public_config_cannot_be_live(tmp_path):
    config, _, _ = issue3_example.example()
    path = tmp_path / "copied.json"
    path.write_text(json.dumps(config))
    with pytest.raises(support.BoundaryError):
        support.load_config(tmp_path, path, "issue3-readonly")


@pytest.mark.parametrize("flag,env", [(False, "1"), (True, "0"), (True, "")])
def test_readonly_double_gate_before_client(monkeypatch, flag, env):
    monkeypatch.setenv("AZ_ARTIFACTS_RUN_READONLY_INTEROP", env)
    monkeypatch.setattr(issue3_readonly, "UniversalPackageClient",
                        lambda *a, **kw: pytest.fail("Gate must precede network"))
    args = ["--private-directory", "X:\\private", "--config", "X:\\private\\config.json"]
    if flag:
        args.append("--execute-readonly")
    assert issue3_readonly.cli(args) == 1


def test_native_output_not_allowed_as_ground_truth(tmp_path):
    config, baseline, _ = issue3_example.example()
    baseline["fixture_only"] = False
    baseline["provenance"]["kind"] = "native"
    (tmp_path / "baseline.json").write_text(json.dumps(baseline))
    with pytest.raises(support.BoundaryError):
        issue3_readonly.baseline_from(tmp_path, config)


def test_complete_matrix_can_pass_only_with_explicit_resource_fixtures(acceptance):
    config, baseline, service = acceptance[1], acceptance[2], acceptance[4]
    deleted = copy.deepcopy(baseline["cases"][2]["expected"][0])
    deleted.update(version="0.9.0", is_deleted=True, is_latest=False)
    case = {"target": 0, "addressing": "name", "method": "list_package_versions",
            "args": {"name": "fixture-package", "include_deleted": True}}
    config["cases"].append(case)
    baseline["cases"].append({"request": case, "expected": [
        *baseline["cases"][2]["expected"], deleted,
    ]})
    denied = {"target": 0, "addressing": "name", "method": "get_package_metadata",
              "args": {"name": "fixture-denied", "version": "1.0.0"}}
    config["cases"].append(denied)
    baseline["cases"].append({"request": denied, "error": "PermissionDeniedError",
                              "status_code": 403, "condition": "inaccessible",
                              "operation": "metadata"})

    def response(request):
        if "/fixture-denied/" in request.url.path:
            return httpx.Response(403)
        if (request.url.host == "feeds.dev.azure.com" and request.url.path.endswith("/versions")
                and request.url.params.get("isDeleted") is None):
            values = baseline["cases"][-2]["expected"]
            return httpx.Response(200, json={"count": len(values), "value": wire(values)})
        return service(request)

    code, statuses = execute((*acceptance[:4], response))
    assert code == 0
    assert set(statuses.values()) == {"pass"}


def test_history_cannot_change_package_route(acceptance):
    config, _, _ = acceptance[1], acceptance[2], acceptance[4]
    guard = support.GuardTransport(httpx.MockTransport(lambda _: pytest.fail("No network")),
                                   support.Budget(issue3_example.LIMITS))
    case = config["cases"][8]
    support.configure_routes(guard, config, config["targets"][0], "name", case)
    other_url = support.route(config["services"]["feeds"], "_apis", "packaging", "Feeds",
                              "fixture-feed", "packages", issue3_example.OTHER_ID, "versions")
    with pytest.raises(support.BoundaryError):
        guard.handle_request(httpx.Request("GET", other_url))


def test_versionless_metadata_cannot_send_intent_or_exact_version(acceptance):
    config = acceptance[1]
    guard = support.GuardTransport(httpx.MockTransport(lambda _: pytest.fail("No network")),
                                   support.Budget(issue3_example.LIMITS))
    case = config["cases"][5]
    support.configure_routes(guard, config, config["targets"][0], "name", case)
    url = support.route(config["services"]["packaging"], "_packaging", "fixture-feed",
                        "upack", "packages", "fixture-package", "versions")
    with pytest.raises(support.BoundaryError):
        guard.handle_request(httpx.Request("GET", url, params={"intent": "Download"}))
    with pytest.raises(support.BoundaryError):
        guard.handle_request(httpx.Request("GET", url + "/1.0.0"))


def test_inspection_write_attempt_is_failure_not_missing_path(acceptance, monkeypatch):
    destination = acceptance[0] / "forbidden"

    def mutation(*args, **kwargs):
        destination.mkdir()
        return False

    monkeypatch.setattr(issue3_readonly.UniversalPackageClient, "file_exists", mutation)
    _, statuses = execute(acceptance)
    assert statuses["case-0008"] == "fail"
    assert case_report(acceptance, "case-0008")["reason"] == "filesystem-boundary"
    assert not destination.exists()


def test_wrong_approved_route_has_request_boundary_reason(acceptance):
    acceptance[1]["services"]["feeds"] += "/unexpected"
    # Discovery is independently supplied, so emulate the unaltered service root.
    service = acceptance[4]

    def response(request):
        if request.url.path.endswith("/ResourceAreas"):
            return httpx.Response(200, json={"value": [
                {"name": "Feed", "locationUrl": "https://feeds.dev.azure.com/fixtureorg"},
            ]})
        return service(request)

    _, statuses = execute((*acceptance[:4], response))
    assert statuses["case-0001"] == "fail"
    assert case_report(acceptance, "case-0001")["reason"] == "request-boundary"


@pytest.mark.parametrize("error_type,reason", [
    (support.ProtocolError, "protocol-error"),
    (support.TransportError, "transport-error"),
    (RuntimeError, "unexpected-error"),
])
def test_error_categories_never_serialize_exception_content(
    acceptance, monkeypatch, capsys, error_type, reason,
):
    secret = "https://private.example/file?sig=synthetic-secret"

    def failure(*args, **kwargs):
        error = error_type(secret)
        error.reason = secret
        raise error

    monkeypatch.setattr(issue3_readonly.UniversalPackageClient, "list_feeds", failure)
    _, statuses = execute(acceptance)
    assert statuses["case-0001"] == "fail"
    assert case_report(acceptance, "case-0001")["reason"] == reason
    assert secret not in capsys.readouterr().out
    assert secret not in "".join(path.read_text() for path in acceptance[3].iterdir())


def test_untrusted_boundary_reason_cannot_escape_enum():
    error = support.BoundaryError("synthetic-secret")
    error.reason = "synthetic-secret"
    assert support.reason_code(error) == "unexpected-error"


def test_corrupt_http_gzip_is_protocol_failure_not_missing_fixture(acceptance):
    service = acceptance[4]

    def response(request):
        if request.url.path.endswith("/Feeds"):
            return httpx.Response(200, headers={"content-encoding": "gzip"},
                                  stream=httpx.ByteStream(b"invalid-gzip"))
        return service(request)

    _, statuses = execute((*acceptance[:4], response))
    assert statuses["case-0001"] == "fail"
    assert case_report(acceptance, "case-0001")["reason"] == "protocol-error"


def registration_proposal(config, baseline):
    manifest = baseline["manifests"][0]["manifest_id"]
    manifest_bytes = base64.b64decode(baseline["manifests"][0]["blobs"][manifest])
    files = [{"path": item["path"].removeprefix("/"), "content_id": item["blob"]["id"],
              "size": item["blob"]["size"]} for item in json.loads(manifest_bytes)["items"]]
    content = registration_node([(file["content_id"], file["size"]) for file in files])
    node = registration_node([
        (support.digest(content, "02"), sum(file["size"] for file in files)),
        (manifest, len(manifest_bytes)),
    ])
    proofs = [base64.b64encode(value).decode() for value in (content, node)]
    expected = {**baseline["cases"][4]["expected"], "version": "2.0.0-disposable.1",
                "super_root_id": support.digest(node, "02")}
    proposal = {
        **{key: value for key, value in config.items()
           if key in ("organization", "credential_env", "credential_kind", "services", "limits")},
        "schema": 1, "kind": "issue3-registration", "fixture_only": False,
        "publisher": "registration-only", "approved": True, "disposable_version": True,
        "preuploaded_references_verified": True, "conflict_probe": False,
        "feed": "fixture-feed", "feed_id": issue3_example.FEED_ID,
        "scope": "organization", "name": "fixture-package",
        "version": expected["version"], "evidence_directory": "registration-evidence",
        "expected_metadata": expected, "proof_nodes_env": "AZ_ARTIFACTS_SYNTHETIC_PROOFS",
        "files": files,
        "proof_nodes_sha256": hashlib.sha256(json.dumps(
            proofs, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")).hexdigest(),
    }
    return proposal, proofs


def registration_node(children):
    value = bytearray(struct.pack("<HH", 0, len(children) - 1))
    for identifier, size in children:
        node = identifier.endswith("02")
        value.append(int(node))
        value.extend(size.to_bytes(7 if node else 3, "little"))
        value.extend(bytes.fromhex(identifier[:-2]))
    return bytes(value)


def test_registration_only_one_put_and_public_readback(acceptance, monkeypatch, capsys):
    root, config, baseline, output, service = acceptance
    proposal, proofs = registration_proposal(config, baseline)
    monkeypatch.setenv(proposal["proof_nodes_env"], json.dumps(proofs))
    verified = issue3_registration.proofs_from_environment(proposal)
    service.registration_metadata = proposal["expected_metadata"]
    assert issue3_registration.run(root, proposal, output, verified,
                                   transport=httpx.MockTransport(service)) == 0
    assert [r.method for r in service.requests] == ["GET", "PUT", "GET"]
    persisted = "".join(p.read_text() for p in output.iterdir()) + capsys.readouterr().out
    assert all(proof not in persisted for proof in proofs)
    assert "fixture-package" not in persisted
    assert "sig=" not in persisted
    new_output = root / "new-output"
    new_output.mkdir()
    proposal["feed"] = proposal["feed_id"]
    with pytest.raises(FileExistsError):
        issue3_registration.run(root, proposal, new_output, verified,
                                transport=httpx.MockTransport(service))
    assert sum(r.method == "PUT" for r in service.requests) == 1


@pytest.mark.parametrize("field", ["feed_id", "project_id", "organization", "irrelevant_project"])
@pytest.mark.parametrize("spelling", ["braces", "hex", "uppercase", "urn"])
def test_equivalent_destination_spellings_cannot_replay_registration(acceptance, field, spelling):
    root, config, baseline, output, service = acceptance
    proposal, proofs = registration_proposal(config, baseline)
    if field == "project_id":
        proposal.update(scope="project", project="fixture-project",
                        project_id=issue3_example.PROJECT_ID)
    service.registration_metadata = proposal["expected_metadata"]
    assert issue3_registration.run(
        root, proposal, output, tuple(proofs), transport=httpx.MockTransport(service),
    ) == 0
    if field == "organization":
        proposal[field] = "https://fixtureorg.visualstudio.com/DefaultCollection"
    elif field == "irrelevant_project":
        proposal["project_id"] = issue3_example.PROJECT_ID
    else:
        identifier = proposal[field]
        proposal[field] = {
            "braces": "{" + identifier + "}", "hex": identifier.replace("-", ""),
            "uppercase": identifier.upper(), "urn": "urn:uuid:" + identifier,
        }[spelling]
    with pytest.raises(FileExistsError):
        issue3_registration.run(
            root, proposal, output, tuple(proofs),
            transport=httpx.MockTransport(lambda _: pytest.fail("No replay")),
        )
    assert sum(request.method == "PUT" for request in service.requests) == 1
    assert len(list(root.glob("registration-attempt-*"))) == 1


@pytest.mark.parametrize("status", [202, 206, 400, 408, 409, 500, 503])
def test_registration_unknown_or_conflict_never_retries_or_reconciles_success(
    acceptance, status,
):
    root, config, baseline, output, service = acceptance
    proposal, proofs = registration_proposal(config, baseline)
    service.registration_status = status
    service.registration_metadata = proposal["expected_metadata"]
    assert issue3_registration.run(root, proposal, output, tuple(proofs),
                                   transport=httpx.MockTransport(service)) == 1
    assert sum(r.method == "PUT" for r in service.requests) == 1
    assert len(service.requests) == 2
    report = json.loads((output / "report.json").read_text())
    assert report["cases"][0]["status"] == "fail"
    assert report["cases"][0]["reason"] == (
        "service-error" if status in (400, 409) else "registration-unknown"
    )
    assert report["cases"][1]["status"] == "incomplete"
    assert report["cases"][1]["reason"] == "not-run"


def test_conflict_probe_requires_exact_approved_proposal(acceptance):
    root, config, baseline, output, service = acceptance
    proposal, proofs = registration_proposal(config, baseline)
    proposal["conflict_probe"] = True
    service.registration_metadata = proposal["expected_metadata"]
    puts = 0

    def response(request):
        nonlocal puts
        if request.method == "PUT":
            puts += 1
            service.registration_status = 204 if puts == 1 else 409
        return service(request)

    assert issue3_registration.run(root, proposal, output, tuple(proofs),
                                   transport=httpx.MockTransport(response)) == 0
    assert [r.method for r in service.requests] == ["GET", "PUT", "GET", "PUT", "GET"]


@pytest.mark.parametrize("field,value", [
    ("fixture_only", True), ("approved", False), ("approved", 1),
    ("preuploaded_references_verified", False), ("disposable_version", False),
    ("publisher", "native-python"), ("completed", True), ("conflict_probe", "true"),
])
def test_registration_public_fixture_and_write_approval_gates(acceptance, field, value):
    root, config, baseline, output, _ = acceptance
    proposal, proofs = registration_proposal(config, baseline)
    proposal[field] = value
    with pytest.raises(support.BoundaryError):
        issue3_registration.run(root, proposal, output, tuple(proofs),
                                transport=httpx.MockTransport(lambda _: pytest.fail("No write")))
    assert not list(root.glob("registration-attempt-*"))


def test_registration_proof_hash_binds_approved_references(acceptance, monkeypatch):
    proposal, proofs = registration_proposal(acceptance[1], acceptance[2])
    monkeypatch.setenv(proposal["proof_nodes_env"], json.dumps(proofs + proofs))
    with pytest.raises(support.BoundaryError):
        issue3_registration.proofs_from_environment(proposal)


@pytest.mark.parametrize("change", ["missing-content", "wrong-size", "wrong-file", "missing-file"])
def test_registration_rejects_incomplete_content_proofs_before_any_attempt(acceptance, change):
    root, config, baseline, output, _ = acceptance
    proposal, proofs = registration_proposal(config, baseline)
    if change == "missing-content":
        proofs = proofs[1:]
    elif change == "wrong-size":
        proposal["expected_metadata"]["package_size"] += 1
    elif change == "wrong-file":
        proposal["files"][0]["content_id"] = "EF" * 32 + "01"
    else:
        proposal["files"].pop()
    proposal["proof_nodes_sha256"] = hashlib.sha256(json.dumps(
        proofs, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    with pytest.raises(support.BoundaryError):
        issue3_registration.run(
            root, proposal, output, tuple(proofs),
            transport=httpx.MockTransport(lambda _: pytest.fail("No network")),
        )
    assert not list(root.glob("registration-attempt-*"))


def test_registration_packed_content_tree_requires_all_intermediate_proofs(acceptance):
    proposal, proofs = registration_proposal(acceptance[1], acceptance[2])
    content = base64.b64decode(proofs[0])
    packed = registration_node([
        (support.digest(content, "02"), sum(file["size"] for file in proposal["files"])),
    ])
    root_children = support.node_children(base64.b64decode(proofs[1]))
    root = registration_node([
        (support.digest(packed, "02"), root_children[0][1]), root_children[1],
    ])
    proposal["expected_metadata"]["super_root_id"] = support.digest(root, "02")
    proofs = [base64.b64encode(value).decode() for value in (content, packed, root)]
    proposal["proof_nodes_sha256"] = hashlib.sha256(json.dumps(
        proofs, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    assert issue3_registration.validate_proofs(proposal, proofs) == tuple(proofs)
    proofs.pop(0)
    proposal["proof_nodes_sha256"] = hashlib.sha256(json.dumps(
        proofs, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    with pytest.raises(support.BoundaryError):
        issue3_registration.validate_proofs(proposal, proofs)


@pytest.mark.parametrize("enabled,matching", [(False, True), (True, False)])
def test_registration_environment_and_exact_digest_gate(acceptance, monkeypatch, enabled, matching):
    root, config, baseline, _, _ = acceptance
    proposal, _ = registration_proposal(config, baseline)
    path = root / "proposal.json"
    path.write_text(json.dumps(proposal))
    monkeypatch.setattr(support, "private_root", lambda _: root)
    monkeypatch.setenv("AZ_ARTIFACTS_RUN_REGISTRATION_INTEROP", "1" if enabled else "0")
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if matching else "0" * 64
    monkeypatch.setattr(issue3_registration, "UniversalPackageClient",
                        lambda *a, **kw: pytest.fail("No write without exact approval"))
    assert issue3_registration.cli([
        "--private-directory", str(root), "--proposal", str(path), "--execute-approved",
        "--proposal-sha256", digest,
    ]) == 1
    assert not list(root.glob("registration-attempt-*"))


@pytest.mark.parametrize("first_approved", [False, True])
def test_registration_hashes_and_executes_the_same_single_read(
    acceptance, monkeypatch, first_approved,
):
    root, config, baseline, _, _ = acceptance
    proposal, _ = registration_proposal(config, baseline)
    approved = json.dumps(proposal).encode("utf-8")
    unapproved = json.dumps({**proposal, "conflict_probe": True}).encode("utf-8")
    path = root / "proposal.json"
    path.write_bytes(approved)
    original_open = type(path).open
    reads = []

    def replacing_open(self, mode="r", *args, **kwargs):
        if self == path and mode in ("r", "rb"):
            reads.append(mode)
            data = approved if (len(reads) == 1) == first_approved else unapproved
            return io.BytesIO(data) if mode == "rb" else io.StringIO(data.decode("utf-8"))
        return original_open(self, mode, *args, **kwargs)

    executed = []
    monkeypatch.setattr(type(path), "open", replacing_open)
    monkeypatch.setattr(support, "private_root", lambda _: root)
    monkeypatch.setenv("AZ_ARTIFACTS_RUN_REGISTRATION_INTEROP", "1")
    monkeypatch.setattr(issue3_registration, "proofs_from_environment", lambda _: ())

    def run(root, actual, output, proofs):
        executed.append(actual)
        return 0

    monkeypatch.setattr(issue3_registration, "run", run)
    code = issue3_registration.cli([
        "--private-directory", str(root), "--proposal", str(path), "--execute-approved",
        "--proposal-sha256", hashlib.sha256(approved).hexdigest(),
    ])
    assert reads == ["rb"]
    assert code == (0 if first_approved else 1)
    assert executed == ([proposal] if first_approved else [])


def test_private_json_read_is_bounded_before_parsing(tmp_path):
    path = tmp_path / "input.json"
    path.write_bytes(b" " * 33)
    with pytest.raises(support.Incomplete) as caught:
        support.read_json(path, max_bytes=32)
    assert support.reason_code(caught.value) == "budget-exhausted"


def test_registration_preview_does_not_read_proofs_or_construct_client(acceptance, monkeypatch):
    root, config, baseline, _, _ = acceptance
    proposal, _ = registration_proposal(config, baseline)
    proposal["approved"] = False
    path = root / "proposal.json"
    path.write_text(json.dumps(proposal))
    monkeypatch.setattr(support, "private_root", lambda _: root)
    monkeypatch.setattr(issue3_registration, "UniversalPackageClient",
                        lambda *a, **kw: pytest.fail("Preview has no network"))
    code = issue3_registration.main(["--private-directory", str(root), "--proposal", str(path)])
    assert code == 0
    assert not list(root.glob("registration-attempt-*"))


def test_registration_transport_loss_never_retries(acceptance):
    root, config, baseline, output, service = acceptance
    proposal, proofs = registration_proposal(config, baseline)
    puts = 0

    def response(request):
        nonlocal puts
        if request.method == "PUT":
            puts += 1
            raise httpx.ReadError("synthetic signed-url?sig=must-not-persist")
        return service(request)

    assert issue3_registration.run(root, proposal, output, tuple(proofs),
                                   transport=httpx.MockTransport(response)) == 1
    assert puts == 1
    assert "must-not-persist" not in "".join(p.read_text() for p in output.iterdir())


@pytest.mark.skipif(
    os.environ.get("AZ_ARTIFACTS_RUN_READONLY_INTEROP") != "1",
    reason="Live read-only acceptance explicitly disabled; offline tests need no credentials",
)
def test_live_readonly_acceptance():
    code = issue3_readonly.cli([
        "--private-directory", os.environ.get("AZ_ARTIFACTS_PRIVATE_DIRECTORY", ""),
        "--config", os.environ.get("AZ_ARTIFACTS_READONLY_CONFIG", ""),
        "--execute-readonly",
    ])
    assert code == 0


@pytest.mark.skipif(
    os.environ.get("AZ_ARTIFACTS_RUN_REGISTRATION_INTEROP") != "1",
    reason="Separate exact registration approval required; never enabled by read-only fixtures",
)
def test_live_registration_only():
    code = issue3_registration.cli([
        "--private-directory", os.environ.get("AZ_ARTIFACTS_PRIVATE_DIRECTORY", ""),
        "--proposal", os.environ.get("AZ_ARTIFACTS_REGISTRATION_PROPOSAL", ""),
        "--proposal-sha256", os.environ.get("AZ_ARTIFACTS_REGISTRATION_PROPOSAL_SHA256", ""),
        "--execute-approved",
    ])
    assert code == 0
