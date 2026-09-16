import builtins
import json
import os
import traceback
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from az_artifacts import (
    ArtifactsError,
    AuthenticationError,
    BearerToken,
    ConflictError,
    NotFoundError,
    PackageMetadata,
    PackagePushMetadata,
    PermissionDeniedError,
    ProtocolError,
    RegistrationOutcomeUnknownError,
    ServiceError,
    TransportError,
    UniversalPackageClient,
)
from az_artifacts.auth import ADO_SCOPE

PUSH = PackagePushMetadata("ab" * 32 + "01", "cd" * 32 + "02", ("opaque proof", "", "opaque proof"))


def register(client, **options):
    return client.add_package(
        **({"feed": "feed", "name": "package", "version": "1.2.3", "metadata": PUSH} | options)
    )


def assert_single_put(service):
    assert [request.method for request in service.requests] == ["GET", "PUT"]
    assert service.requests[0].url.path.endswith("/ResourceAreas")
    assert not service.resolve_counts


@pytest.mark.parametrize("status", [200, 201, 204])
@pytest.mark.parametrize("description", [None, "", "Résumé\npackage"])
@pytest.mark.parametrize(
    "proofs",
    [(), ("",), ("opaque / proof?", "", "opaque / proof?"), ('{"proof":"opaque"}', " ☃\n\x00 ")],
)
def test_registration_wire_and_acknowledgment(client, service, status, description, proofs):
    service.registration_status = status
    metadata = replace(PUSH, description=description, proof_nodes=proofs)
    assert register(client, metadata=metadata, version="1.2.3-rc.1") is None
    assert_single_put(service)
    request = service.requests[-1]
    assert request.url == (
        "https://pkgs.dev.azure.com/org/_packaging/feed/upack/packages/package/versions/1.2.3-rc.1"
    )
    assert request.headers["accept"] == "application/json; api-version=7.1-preview.1"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["authorization"] == "Basic OnRlc3QtcGF0"
    assert request.headers["x-tfs-fedauthredirect"] == "Suppress"
    assert request.extensions["timeout"] == {
        "connect": 60.0,
        "read": 60.0,
        "write": 60.0,
        "pool": 60.0,
    }
    expected = {
        "manifestId": PUSH.manifest_id.upper(),
        "proofNodes": list(proofs),
        "superRootId": PUSH.super_root_id.upper(),
    }
    if description is not None:
        expected = {"description": description} | expected
    assert json.loads(request.content) == expected
    assert list(json.loads(request.content)) == list(expected)
    assert metadata.manifest_id == PUSH.manifest_id
    assert metadata.super_root_id == PUSH.super_root_id
    assert metadata.proof_nodes is proofs
    assert metadata.description == description


def test_acknowledged_body_has_no_typed_result(client, service):
    service.registration_status = 201
    service.registration_body = b"no response schema"
    service.registration_headers = {"location": "https://pkgs.dev.azure.com/org/package"}
    assert register(client) is None
    assert_single_put(service)


@pytest.mark.parametrize(
    ("scope", "project", "prefix"),
    [
        ("organization", None, b"/org/"),
        ("project", "My Project/#", b"/org/My%20Project%2F%23/"),
    ],
)
def test_scope_and_route_encoding(client, service, scope, project, prefix):
    register(client, feed="Shared Feed/100%?#", name="my.package-1", scope=scope, project=project)
    assert service.requests[-1].url.raw_path == (
        prefix
        + b"_packaging/Shared%20Feed%2F100%25%3F%23/upack/packages/my.package-1/versions/1.2.3"
    )
    assert_single_put(service)


@pytest.mark.parametrize(
    "options",
    [
        {"feed": ""},
        {"feed": " "},
        {"feed": ".."},
        {"feed": None},
        {"feed": 1},
        {"name": ""},
        {"name": "Uppercase"},
        {"name": "a/b"},
        {"name": None},
        {"name": 1},
        {"scope": "other"},
        {"scope": None},
        {"scope": "project"},
        {"scope": "project", "project": ""},
        {"scope": "project", "project": " "},
        {"scope": "project", "project": ".."},
        {"scope": "project", "project": 123},
        {"project": "project"},
    ],
)
def test_invalid_shared_arguments_before_network(client, service, options):
    with pytest.raises((ValueError, TypeError)):
        register(client, **options)
    assert service.requests == []


@pytest.mark.parametrize(
    "version",
    ["*", "1.*", "1.2.*", "", "latest", "1.2", "1.2.3-RC.1", "1.2.3-01", "1.2.3+build", None, 1],
)
def test_exact_version_required_before_network(client, service, version):
    with pytest.raises((ValueError, TypeError)):
        register(client, version=version)
    assert service.requests == []


@pytest.mark.parametrize(
    "metadata",
    [None, {}, "metadata", PackageMetadata("1.2.3", PUSH.manifest_id, PUSH.super_root_id, 0)],
)
def test_wrong_model_class_before_network(client, service, metadata):
    with pytest.raises(TypeError, match="metadata"):
        register(client, metadata=metadata)
    assert service.requests == []


@pytest.mark.parametrize("field", ["manifest_id", "super_root_id"])
@pytest.mark.parametrize("value", ["", "ab" * 32, "ab" * 32 + "03", "a" * 67, "gg" * 32 + "01"])
def test_invalid_id_before_network(client, service, field, value):
    with pytest.raises(ValueError, match=field):
        register(client, metadata=replace(PUSH, **{field: value}))
    assert service.requests == []


@pytest.mark.parametrize("field", ["manifest_id", "super_root_id"])
@pytest.mark.parametrize("value", [None, 1, True, [], b"ab" * 32 + b"01"])
def test_wrong_id_type_before_network(client, service, field, value):
    with pytest.raises(TypeError, match=field):
        register(client, metadata=replace(PUSH, **{field: value}))
    assert service.requests == []


@pytest.mark.parametrize(
    "proofs", [None, [], ["proof"], "proof", b"proof", {}, (None,), (1,), ([],)]
)
def test_wrong_proof_shape_before_network(client, service, proofs):
    with pytest.raises(TypeError, match="proof_nodes"):
        register(client, metadata=replace(PUSH, proof_nodes=proofs))
    assert service.requests == []


@pytest.mark.parametrize("description", [1, True, [], {}, b""])
def test_wrong_description_type_before_network(client, service, description):
    with pytest.raises(TypeError, match="description"):
        register(client, metadata=replace(PUSH, description=description))
    assert service.requests == []


def test_keyword_only(client, service):
    with pytest.raises(TypeError):
        client.add_package("feed", "package", "1.2.3", PUSH)
    assert service.requests == []


@pytest.mark.parametrize("advertise_packaging", [False, True])
def test_no_dedup_blob_or_local_file_access(client, service, monkeypatch, advertise_packaging):
    service.services = (
        [area for area in service.services if area["name"] == "Packaging"]
        if advertise_packaging
        else []
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Registration must not use Dedup, blobs, or local files")

    monkeypatch.setattr(client, "_blob_url", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    register(client)
    assert_single_put(service)


@pytest.mark.parametrize(
    "area",
    [
        {"name": "Packaging"},
        {"name": "UPackPackaging"},
        {"name": "Renamed", "id": "D397749B-F115-4027-B6DD-77A65DD10D21"},
    ],
)
def test_discovered_packaging_service_and_resource_id(service, area):
    service.services = [
        {"name": "Packaging", "locationUrl": "https://pkgs.dev.azure.com/unused"},
        area | {"locationUrl": "https://custom.pkgs.dev.azure.com/location/"},
    ]

    def handler(request):
        if request.url.path.endswith("/ResourceAreas"):
            return service(request)
        service.requests.append(request)
        assert request.method == "PUT"
        assert request.url == (
            "https://custom.pkgs.dev.azure.com/location/"
            "_packaging/feed/upack/packages/package/versions/1.2.3"
        )
        return httpx.Response(204)

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler)
    ) as client:
        register(client)
    assert_single_put(service)


@pytest.mark.parametrize("kind", ["pat", "bearer", "credential"])
def test_explicit_authentication(service, kind):
    scopes = []

    class Credential:
        def get_token(self, *values):
            scopes.append(values)
            return BearerToken("test-token")

    credential = {
        "pat": "test-pat",
        "bearer": BearerToken("test-token"),
        "credential": Credential(),
    }[kind]

    def handler(request):
        service.requests.append(request)
        expected = "Basic OnRlc3QtcGF0" if kind == "pat" else "Bearer test-token"
        assert request.headers["authorization"] == expected
        if request.url.path.endswith("/ResourceAreas"):
            return httpx.Response(200, json={"value": service.services})
        return httpx.Response(204)

    with UniversalPackageClient(
        "org", credential=credential, transport=httpx.MockTransport(handler)
    ) as client:
        register(client)
    assert scopes == ([(ADO_SCOPE,), (ADO_SCOPE,)] if kind == "credential" else [])
    assert_single_put(service)


def test_closed_client(client, service):
    client.discover_services()
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        register(client)
    assert len(service.requests) == 1


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, ServiceError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (422, ServiceError),
        (429, ServiceError),
        (408, RegistrationOutcomeUnknownError),
        (500, RegistrationOutcomeUnknownError),
        (501, RegistrationOutcomeUnknownError),
        (502, RegistrationOutcomeUnknownError),
        (503, RegistrationOutcomeUnknownError),
        (504, RegistrationOutcomeUnknownError),
    ],
)
@pytest.mark.parametrize("id_header", ["x-vss-e2eid", "x-ms-request-id"])
def test_service_errors_never_replay(service, monkeypatch, status, error, id_header):
    service.registration_status = status
    service.registration_body = b"private proof body"
    service.registration_headers = {id_header: "request-id", "retry-after": "0"}
    monkeypatch.setattr("az_artifacts._http.time.sleep", lambda _: pytest.fail("unexpected replay"))
    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(service), retries=4
    ) as client:
        with pytest.raises(error) as caught:
            register(client)
    assert type(caught.value) is error
    assert caught.value.status_code == status
    assert caught.value.request_id == "request-id"
    assert isinstance(caught.value, ArtifactsError)
    assert_single_put(service)
    rendered = "".join(traceback.format_exception(caught.value))
    for private in ["private proof body", "test-pat", "opaque proof", "https://"]:
        assert private not in rendered


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.ConnectError,
        httpx.ReadError,
        httpx.WriteError,
        httpx.RemoteProtocolError,
    ],
)
def test_transport_failure_is_uncertain_with_one_attempt(service, monkeypatch, failure):
    monkeypatch.setattr("az_artifacts._http.time.sleep", lambda _: pytest.fail("unexpected replay"))

    def handler(request):
        if request.url.path.endswith("/ResourceAreas"):
            return service(request)
        service.requests.append(request)
        raise failure(f"{request.url} secret-token opaque proof", request=request)

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=4
    ) as client:
        with pytest.raises(RegistrationOutcomeUnknownError) as caught:
            register(client)
    assert isinstance(caught.value.__cause__, TransportError)
    assert caught.value.status_code is None
    assert caught.value.request_id is None
    assert_single_put(service)
    rendered = "".join(traceback.format_exception(caught.value))
    for private in ["https://", "secret-token", "opaque proof"]:
        assert private not in rendered


def test_read_failure_after_success_headers_is_uncertain(service):
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial body"
            raise httpx.ReadError("private response data")

    def handler(request):
        if request.url.path.endswith("/ResourceAreas"):
            return service(request)
        service.requests.append(request)
        return httpx.Response(201, stream=BrokenBody())

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=4
    ) as client:
        with pytest.raises(RegistrationOutcomeUnknownError):
            register(client)
    assert_single_put(service)


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (202, {}),
        (203, {}),
        (205, {}),
        (206, {}),
        (207, {}),
        (208, {}),
        (226, {}),
        (300, {}),
        (304, {}),
        (200, {"azure-asyncoperation": "https://private.example/operation?sig=secret"}),
        (201, {"operation-location": "https://private.example/operation?sig=secret"}),
        (204, {"content-range": "bytes 0-1/10"}),
        (200, {"x-ms-continuationtoken": "private-token"}),
    ],
)
def test_nonacknowledgment_is_uncertain(client, service, status, headers):
    service.registration_status = status
    service.registration_headers = headers | {"x-vss-e2eid": "request-id"}
    with pytest.raises(RegistrationOutcomeUnknownError) as caught:
        register(client)
    assert caught.value.status_code == status
    assert caught.value.request_id == "request-id"
    assert "private" not in str(caught.value)
    assert_single_put(service)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_is_uncertain_not_followed(client, service, status):
    service.registration_status = status
    service.registration_headers = {"location": "https://private.example/next?sig=secret"}
    with pytest.raises(RegistrationOutcomeUnknownError) as caught:
        register(client)
    assert isinstance(caught.value.__cause__, ProtocolError)
    assert_single_put(service)
    assert "private" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("failure", ["encoding", "oversized"])
def test_response_protocol_failures_are_uncertain(client, service, failure):
    if failure == "encoding":
        service.registration_body = b"not-gzip"
        service.registration_headers = {"content-encoding": "gzip"}
    else:
        service.registration_body = b"x" * (16 * 1024 * 1024 + 1)
    service.registration_status = 200
    with pytest.raises(RegistrationOutcomeUnknownError) as caught:
        register(client)
    assert isinstance(caught.value.__cause__, ProtocolError)
    assert_single_put(service)


@pytest.mark.parametrize("status", [409, 500, 202])
def test_unsafe_request_id_is_not_exposed(client, service, status):
    service.registration_status = status
    service.registration_headers = {"x-vss-e2eid": "https://private.example/?sig=secret"}
    with pytest.raises((ConflictError, RegistrationOutcomeUnknownError)) as caught:
        register(client)
    assert caught.value.request_id is None
    assert_single_put(service)


def test_explicit_later_conflict_does_not_reconcile_unknown_outcome(service):
    attempts = 0

    def handler(request):
        nonlocal attempts
        if request.url.path.endswith("/ResourceAreas"):
            return service(request)
        service.requests.append(request)
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("unknown commit")
        return httpx.Response(409)

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=4
    ) as client:
        with pytest.raises(RegistrationOutcomeUnknownError):
            register(client)
        assert attempts == 1
        with pytest.raises(ConflictError):
            register(client)
    assert [request.method for request in service.requests] == ["GET", "PUT", "PUT"]


def test_discovery_failure_is_not_registration_uncertainty(service):
    def handler(request):
        service.requests.append(request)
        raise httpx.ConnectError("private discovery URL")

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
    ) as client:
        with pytest.raises(TransportError):
            register(client)
    assert [request.method for request in service.requests] == ["GET"]


def test_untrusted_discovery_is_rejected_before_registration(client, service):
    service.services = [{"name": "Packaging", "locationUrl": "https://private.example"}]
    with pytest.raises(ProtocolError):
        register(client)
    assert [request.method for request in service.requests] == ["GET"]
