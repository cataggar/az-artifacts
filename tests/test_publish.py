"""Fault-injection simulator using captured shapes, NOT a remote interop claim.

Receipts here are synthetic SHA256 values with no service authority. The actual
capture and local ArtifactTool SDK formatter vectors are checked separately.
"""

import base64
import hashlib
import json
import threading
import time
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from az_artifacts import (
    AmbiguousPublishError,
    AuthenticationError,
    BearerToken,
    IncompleteUploadError,
    PackageConflictError,
    PermissionDeniedError,
    PublishRequest,
    PublishResult,
    UniversalPackageClient,
)
from az_artifacts._dedup import content_hash, parse_node
from az_artifacts._prepare import PreparedPackage
from az_artifacts._upload import Receipt, receipts, summary_headers
from az_artifacts.errors import ProtocolError
from az_artifacts.models import BlobRef


class ProtocolSimulator:
    def __init__(self):
        self.calls = []
        self.blobs = {}
        self.metadata = None
        self.expiry = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
        self.stale = set()
        self.chunk_status = 200
        self.node_failure = None
        self.rich_node_receipts = False
        self.registration = "success"
        self.readback_status = 200
        self.registration_calls = 0
        self.after_root = None
        self.maximum_active = 0
        self.active = 0
        self.lock = threading.Lock()
        self.instance_id = "11111111-2222-3333-4444-555555555555"

    def receipt(self, identifier):
        expiry = datetime(2000, 1, 1, tzinfo=UTC) if identifier in self.stale else self.expiry
        return {
            "Signature": base64.b64encode(
                hashlib.sha256(b"offline-only-receipt:" + identifier.encode()).digest()
            ).decode(),
            "KeepUntil": {"KeepUntil": expiry.strftime("%Y-%m-%dT%H:%M:%SZ")},
        }

    def node_receipts(self, identifier, children):
        found = {identifier: self.receipt(identifier)}
        if self.rich_node_receipts:
            found.update({child.id: self.receipt(child.id) for child in children})
        return found

    def body(self, identifier):
        data = self.blobs[identifier]
        if identifier.endswith("01"):
            return data
        return b"".join(self.body(child.id) for child in parse_node(data))

    def __call__(self, request):
        self.calls.append(request)
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(200, json={"instanceId": self.instance_id})
        if request.url.host == "synthetic.blob.core.windows.net":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=self.blobs[request.url.path.lstrip("/")])
        if request.url.path.endswith("/dedup/urls"):
            return httpx.Response(
                200,
                json={
                    identifier: "https://synthetic.blob.core.windows.net/" + identifier
                    for identifier in json.loads(request.content)
                },
            )
        if request.url.path.endswith("/ResourceAreas"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"name": "dedup", "locationUrl": "https://org.vsblob.visualstudio.com/"},
                        {
                            "name": "PackagingApi",
                            "locationUrl": "https://org.pkgs.visualstudio.com/",
                        },
                    ]
                },
            )
        if "/upack/packages/" in request.url.path:
            if request.method == "GET":
                if self.registration_calls and self.readback_status != 200:
                    return httpx.Response(self.readback_status)
                return (
                    httpx.Response(404)
                    if self.metadata is None
                    else httpx.Response(200, json=self.metadata)
                )
            self.registration_calls += 1
            if self.registration == "conflict":
                return httpx.Response(409)
            if self.registration == "unavailable":
                return httpx.Response(503)
            if self.registration == "disconnect-before":
                raise httpx.ReadTimeout("SECRET_TRANSPORT_CANARY")
            obj = json.loads(request.content)
            assert obj["superRootId"] in self.blobs
            assert obj["manifestId"] in self.blobs
            proofs = [base64.b64decode(value) for value in obj["proofNodes"]]
            assert len(set(proofs)) == len(proofs)
            assert all(content_hash(proof) + "02" in self.blobs for proof in proofs)
            assert content_hash(proofs[-1]) + "02" == obj["superRootId"]
            manifest = self.body(obj["manifestId"])
            self.metadata = {
                **{key: obj[key] for key in ("manifestId", "superRootId", "description")},
                "version": request.url.path.rsplit("/", 1)[1],
                "packageSize": len(manifest)
                + sum(item["blob"]["size"] for item in json.loads(manifest)["items"]),
            }
            if self.registration == "different-content":
                self.metadata["packageSize"] += 1
                raise httpx.ReadTimeout("SECRET_TRANSPORT_CANARY")
            if self.registration == "invalid-description":
                self.metadata["description"] = False
            if self.registration == "wrong-version":
                self.metadata["version"] = "9.9.9"
            if self.registration == "disconnect-after":
                raise httpx.ReadTimeout("SECRET_TRANSPORT_CANARY")
            if self.registration == "unavailable-after":
                return httpx.Response(503)
            return httpx.Response(204)
        assert request.method == "PUT"
        assert request.headers["content-range"] == f"bytes */{len(request.content)}"
        assert (
            request.headers["content-type"] == "application/octet-stream; api-version=1.0-preview"
        )
        if request.url.path.endswith("/chunks"):
            with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            try:
                time.sleep(0.005)
                if self.chunk_status != 200:
                    return httpx.Response(self.chunk_status)
                found = {}
                offset = 0
                for key, value in request.headers.items():
                    if key.startswith("x-ms-chunk-"):
                        size, flag = value.split("/")
                        assert flag == "false"
                        size = int(size)
                        data = request.content[offset : offset + size]
                        identifier = key.removeprefix("x-ms-chunk-").upper()
                        assert content_hash(data) + "01" == identifier
                        self.blobs[identifier] = data
                        self.stale.discard(identifier)
                        found[identifier] = self.receipt(identifier)
                        offset += size
                assert offset == len(request.content)
                assert 1 <= len(found) <= 64
                assert len(request.content) <= 64 * 131072
                return httpx.Response(200, json=found)
            finally:
                with self.lock:
                    self.active -= 1
        identifier = request.url.path.rsplit("/", 1)[1]
        assert content_hash(request.content) + "02" == identifier
        children = parse_node(request.content)
        if self.node_failure == "foreign-child":
            return httpx.Response(
                409,
                json={
                    "Missing": ["A" * 64 + "01"],
                    "InsufficientKeepUntil": [],
                    "Receipts": {},
                },
            )
        if self.node_failure == "empty-receipts":
            return httpx.Response(200, json={})
        if self.node_failure == "stuck":
            return httpx.Response(
                409,
                json={
                    "Missing": [child.id for child in children],
                    "InsufficientKeepUntil": [],
                    "Receipts": {},
                },
            )
        if identifier in self.blobs and identifier not in self.stale:
            return httpx.Response(200, json=self.node_receipts(identifier, children))
        missing = list(dict.fromkeys(child.id for child in children if child.id not in self.blobs))
        insufficient = list(dict.fromkeys(child.id for child in children if child.id in self.stale))
        found = {child.id: self.receipt(child.id) for child in children if child.id not in missing}
        if missing or insufficient or "x-ms-signature" not in request.headers:
            return httpx.Response(
                409,
                json={
                    "Missing": missing,
                    "InsufficientKeepUntil": insufficient,
                    "Receipts": found,
                },
            )
        expected = summary_headers(children, receipts(found, set(found)))
        assert request.headers["x-ms-signature"] == expected["X-MS-Signature"]
        assert request.headers["x-ms-keepuntils"] == expected["X-MS-KeepUntils"]
        self.blobs[identifier] = request.content
        self.stale.discard(identifier)
        if self.after_root:
            self.after_root(identifier)
        return httpx.Response(200, json=self.node_receipts(identifier, children))


@pytest.fixture
def source(tmp_path):
    (tmp_path / "hello.txt").write_bytes(b"az-artifacts native publishing feasibility\r\n")
    return tmp_path


def request(source, **kwargs):
    return PublishRequest("feed", "example", "1.2.3", source, **kwargs)


def client(server, credential="fake-offline-pat", **kwargs):
    return UniversalPackageClient(
        "org", credential=credential, transport=httpx.MockTransport(server), **kwargs
    )


@pytest.mark.parametrize("credential", ["fake-offline-pat", BearerToken("fake-offline-oauth")])
@pytest.mark.parametrize("scope", ["organization", "project"])
def test_public_api_publish_and_download(source, tmp_path, credential, scope):
    server = ProtocolSimulator()
    params = {"scope": scope, "project": "Project Name" if scope == "project" else None}
    with client(server, credential=credential) as api:
        result = api.publish(request(source, description="offline simulator only", **params))
        downloaded = api.download(
            feed="feed",
            name="example",
            version="1.2.3",
            path=tmp_path / "download",
            overwrite=False,
            **params,
        )
        assert (downloaded.path / "hello.txt").read_bytes() == (source / "hello.txt").read_bytes()
    assert isinstance(result, PublishResult)
    assert result.path == source.resolve()
    assert result.files == (Path("hello.txt"),)
    assert result.metadata.version == "1.2.3"
    assert result.metadata.package_size == 219
    assert result.bytes_uploaded == sum(
        len(call.content)
        for call in server.calls
        if call.method == "PUT" and "/dedup/" in call.url.path
    )
    assert server.registration_calls == 1
    assert all(
        call.url.path.startswith("/A" + server.instance_id + "/")
        for call in server.calls
        if call.method == "PUT" and "/dedup/" in call.url.path
    )
    registration = next(
        call for call in server.calls if call.method == "PUT" and "/upack/" in call.url.path
    )
    assert registration.url.path.startswith(
        "/Project Name/" if scope == "project" else "/_packaging/"
    )
    assert all(
        call.headers["authorization"].startswith(
            "Basic " if isinstance(credential, str) else "Bearer "
        )
        for call in server.calls
        if call.url.host != "synthetic.blob.core.windows.net"
    )
    manifest = json.loads(server.body(result.metadata.manifest_id))
    assert server.body(manifest["items"][0]["blob"]["id"]) == (source / "hello.txt").read_bytes()


def test_requests_are_frozen(source):
    with pytest.raises(FrozenInstanceError):
        request(source).version = "2.0.0"


def test_invalid_upload_account_discovery_fails_before_any_write(source):
    server = ProtocolSimulator()
    server.instance_id = "not-an-instance-id"
    with client(server) as api, pytest.raises(ProtocolError, match="instance ID"):
        api.publish(request(source))
    assert not any(call.method == "PUT" for call in server.calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Upper"},
        {"name": "a--b"},
        {"version": "*"},
        {"version": "1.0.0+build"},
        {"version": "1.0.0-UP"},
        {"version": "1.0.0-01"},
        {"version": "2147483648.0.0"},
        {"version": "01.0.0"},
        {"scope": "wrong"},
        {"scope": "project"},
        {"project": "unexpected"},
        {"description": 42},
        {"feed": ""},
    ],
)
def test_invalid_requests_never_reach_network(source, changes):
    with client(lambda req: pytest.fail("No network for invalid input")) as api:
        with pytest.raises(ValueError):
            api.publish(replace(request(source), **changes))


def test_existing_version_conflicts_before_upload(source):
    server = ProtocolSimulator()
    server.metadata = {"existing": True}
    with client(server) as api, pytest.raises(PackageConflictError):
        api.publish(request(source))
    assert not any(call.method == "PUT" for call in server.calls)


def test_existing_blobs_upload_only_root_nodes(source):
    server = ProtocolSimulator()
    prepared = PreparedPackage(source, "1.2.3", max_bytes=1048576)
    server.blobs.update({key: chunk.read() for key, chunk in prepared.chunks.items()})
    server.blobs.update({key: node.data for key, node in prepared.nodes.items()})
    with client(server) as api:
        result = api.publish(request(source))
    assert result.bytes_uploaded == 120
    assert not any(call.url.path.endswith("/chunks") for call in server.calls)


def test_insufficient_retention_is_renewed_before_registration(source):
    server = ProtocolSimulator()
    prepared = PreparedPackage(source, "1.2.3", max_bytes=1048576)
    identifier = prepared.items[0].blob.id
    server.blobs[identifier] = prepared.chunks[identifier].read()
    server.stale.add(identifier)
    with client(server) as api:
        api.publish(request(source))
    assert identifier not in server.stale
    assert server.registration_calls == 1


@pytest.mark.parametrize("mode", ["foreign-child", "empty-receipts", "stuck"])
def test_incomplete_node_retention_prevents_registration(source, mode):
    server = ProtocolSimulator()
    server.node_failure = mode
    with client(server) as api, pytest.raises(IncompleteUploadError):
        api.publish(request(source))
    assert server.registration_calls == 0
    assert len([call for call in server.calls if "/dedup/nodes/" in call.url.path]) <= 2


@pytest.mark.parametrize(
    "status,error",
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (409, IncompleteUploadError),
        (500, IncompleteUploadError),
        (429, IncompleteUploadError),
    ],
)
def test_failed_chunk_writes_are_not_retried(source, status, error):
    server = ProtocolSimulator()
    server.chunk_status = status
    with client(server, retries=3) as api, pytest.raises(error):
        api.publish(request(source))
    assert sum(call.url.path.endswith("/chunks") for call in server.calls) == 1
    assert server.registration_calls == 0


@pytest.mark.parametrize("mode", ["disconnect-after", "unavailable-after"])
def test_ambiguous_registration_can_be_confirmed_read_only(source, mode):
    server = ProtocolSimulator()
    server.registration = mode
    with client(server, retries=3) as api:
        assert api.publish(request(source)).metadata.version == "1.2.3"
    assert server.registration_calls == 1
    assert server.calls[-1].method == "GET"


@pytest.mark.parametrize("mode", ["disconnect-before", "unavailable"])
def test_unconfirmed_registration_is_explicit_and_never_retried(source, mode):
    server = ProtocolSimulator()
    server.registration = mode
    with client(server, retries=3) as api, pytest.raises(AmbiguousPublishError) as caught:
        api.publish(request(source))
    assert "SECRET_TRANSPORT_CANARY" not in str(caught.value)
    assert server.registration_calls == 1


@pytest.mark.parametrize("mode", ["conflict", "different-content"])
def test_registration_race_never_overwrites(source, mode):
    server = ProtocolSimulator()
    server.registration = mode
    with client(server) as api, pytest.raises(PackageConflictError):
        api.publish(request(source))
    assert server.registration_calls == 1
    assert all(call.method not in ("DELETE", "PATCH") for call in server.calls)


@pytest.mark.parametrize("mode", ["success", "disconnect-after"])
@pytest.mark.parametrize(
    "submitted,returned,matches",
    [
        (None, None, True),
        (None, "", True),
        ("", None, True),
        ("", "", True),
        ("fixture description", "fixture description", True),
        ("fixture description", "different description", False),
        ("fixture description", None, False),
        (None, "different description", False),
    ],
)
def test_readback_compares_content_separately_from_optional_description(
    source, mode, submitted, returned, matches
):
    server = ProtocolSimulator()
    server.registration = mode

    def handler(req):
        response = server(req)
        if req.method == "GET" and "/upack/packages/" in req.url.path and server.metadata:
            return httpx.Response(200, json={**server.metadata, "description": returned})
        return response

    with UniversalPackageClient(
        "org", credential="offline-only", transport=httpx.MockTransport(handler)
    ) as api:
        if matches:
            result = api.publish(request(source, description=submitted))
            assert result.metadata.description == returned
        else:
            with pytest.raises(PackageConflictError):
                api.publish(request(source, description=submitted))
    assert server.registration_calls == 1


def test_failed_readback_is_ambiguous_even_after_204(source, monkeypatch):
    monkeypatch.setattr("az_artifacts._http.time.sleep", lambda _: None)
    server = ProtocolSimulator()
    server.readback_status = 503
    with client(server) as api, pytest.raises(AmbiguousPublishError):
        api.publish(request(source))
    assert server.registration_calls == 1


class FailingCredential:
    def __init__(self, fail_when, error):
        self.fail_when = fail_when
        self.error = error

    def get_token(self, *scopes):
        if self.fail_when():
            raise self.error
        return SimpleNamespace(token="fake-offline-credential-token")


@pytest.mark.parametrize("mode", ["success", "disconnect-after"])
def test_readback_credential_failure_is_ambiguous_with_original_cause(source, mode):
    server = ProtocolSimulator()
    server.registration = mode
    error = RuntimeError("synthetic credential refresh failure")
    credential = FailingCredential(lambda: server.registration_calls > 0, error)
    with (
        client(server, credential=credential) as api,
        pytest.raises(AmbiguousPublishError) as caught,
    ):
        api.publish(request(source))
    assert caught.value.__cause__ is error
    assert str(error) not in str(caught.value)
    assert server.registration_calls == 1
    assert server.metadata["version"] == "1.2.3"
    assert server.calls[-1].method == "PUT"
    assert "/upack/packages/" in server.calls[-1].url.path


@pytest.mark.parametrize("phase", ["discovery", "upload", "registration"])
def test_pre_registration_credential_failures_remain_unwrapped(source, phase):
    server = ProtocolSimulator()
    prepared = PreparedPackage(source, "1.2.3", max_bytes=1048576)
    fail_when = {
        "discovery": lambda: True,
        "upload": lambda: any("/dedup/nodes/" in call.url.path for call in server.calls),
        "registration": lambda: prepared.super_root.id in server.blobs,
    }[phase]
    error = RuntimeError("synthetic pre-registration credential failure")
    credential = FailingCredential(fail_when, error)
    with client(server, credential=credential) as api, pytest.raises(RuntimeError) as caught:
        api.publish(request(source))
    assert caught.value is error
    assert server.registration_calls == 0
    assert server.metadata is None


@pytest.mark.parametrize("mode", ["success", "disconnect-after"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_readback_credential_control_flow_exceptions_remain_unwrapped(source, mode, error_type):
    server = ProtocolSimulator()
    server.registration = mode
    error = error_type()
    credential = FailingCredential(lambda: server.registration_calls > 0, error)
    with client(server, credential=credential) as api, pytest.raises(error_type) as caught:
        api.publish(request(source))
    assert caught.value is error
    assert server.registration_calls == 1
    assert server.metadata["version"] == "1.2.3"


@pytest.mark.parametrize("mode", ["invalid-description", "wrong-version"])
def test_malformed_readback_is_not_confirmed(source, mode):
    server = ProtocolSimulator()
    server.registration = mode
    with client(server) as api, pytest.raises(AmbiguousPublishError):
        api.publish(request(source))
    assert server.registration_calls == 1


def test_changed_source_prevents_final_registration(source):
    server = ProtocolSimulator()
    prepared = PreparedPackage(source, "1.2.3", max_bytes=1048576)

    def mutate(identifier):
        if identifier == prepared.super_root.id:
            (source / "hello.txt").write_bytes(b"changed")

    server.after_root = mutate
    with client(server) as api, pytest.raises(OSError):
        api.publish(request(source))
    assert server.registration_calls == 0


def test_missing_content_batches_and_concurrency_are_bounded(tmp_path):
    (tmp_path / "large.bin").write_bytes(
        hashlib.shake_256(b"offline-batch-test").digest(12 * 1048576)
    )
    server = ProtocolSimulator()
    with client(server, max_workers=2) as api:
        result = api.publish(request(tmp_path))
    assert 1 <= server.maximum_active <= 2
    assert sum(call.url.path.endswith("/chunks") for call in server.calls) >= 4
    manifest = json.loads(server.body(result.metadata.manifest_id))
    assert (
        hashlib.sha256(server.body(manifest["items"][0]["blob"]["id"])).digest()
        == hashlib.sha256((tmp_path / "large.bin").read_bytes()).digest()
    )


def test_deep_upload_reuses_receipts_across_nested_and_direct_children(tmp_path):
    with (tmp_path / "zeros.bin").open("wb") as stream:
        for _ in range(64):
            stream.write(bytes(1048576))
        stream.write(bytes(131072))
    server = ProtocolSimulator()
    server.rich_node_receipts = True
    with client(server) as api:
        result = api.publish(request(tmp_path))
    manifest = json.loads(server.body(result.metadata.manifest_id))
    file_root = parse_node(server.blobs[manifest["items"][0]["blob"]["id"]])
    assert len(file_root) == 2
    assert len(parse_node(server.blobs[file_root[0].id])) == 512
    zero_chunk = content_hash(bytes(131072)) + "01"
    assert sum("x-ms-chunk-" + zero_chunk.lower() in call.headers for call in server.calls) == 1
    assert server.registration_calls == 1


def test_large_file_collection_and_chunked_manifest(tmp_path):
    for index in range(1200):
        (tmp_path / f"file-{index:04d}.txt").write_bytes(b"same")
    server = ProtocolSimulator()
    with client(server) as api:
        result = api.publish(request(tmp_path))
    assert result.metadata.manifest_id.endswith("02")
    registration = next(
        json.loads(call.content)
        for call in server.calls
        if call.method == "PUT" and "/upack/packages/" in call.url.path
    )
    assert len(registration["proofNodes"]) == 3
    assert len(json.loads(server.body(result.metadata.manifest_id))["items"]) == 1200


def test_signature_summary_is_ordered_sha256_not_xor():
    a = BlobRef("A" * 64 + "01", 3)
    b = BlobRef("B" * 64 + "01", 5)
    first, second = bytes(range(32)), bytes(range(31, 63))
    known = {
        a.id: Receipt(datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC), first),
        b.id: Receipt(datetime(2030, 1, 3, 3, 4, 5, tzinfo=UTC), second),
    }
    result = summary_headers((a, b), known)
    assert result["X-MS-KeepUntils"] == "2030-01-02T03:04:05Z,2030-01-03T03:04:05Z"
    assert base64.b64decode(result["X-MS-Signature"]) == hashlib.sha256(first + second).digest()
    assert "signature" not in repr(known[a.id])


@pytest.mark.parametrize("signature", ["", "not base64", "A" * 4096])
def test_invalid_receipts_never_echo_signature(signature):
    identifier = "A" * 64 + "01"
    with pytest.raises(ProtocolError) as caught:
        receipts(
            {
                identifier: {
                    "Signature": signature,
                    "KeepUntil": {"KeepUntil": "2030-01-02T03:04:05Z"},
                }
            },
            {identifier},
        )
    if signature:
        assert signature not in str(caught.value)


def test_closed_client_cannot_publish(source):
    api = client(lambda req: pytest.fail("No request from a closed client"))
    api.close()
    with pytest.raises(RuntimeError, match="closed"):
        api.publish(request(source))
