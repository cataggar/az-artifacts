import builtins
import os
from pathlib import Path

import httpx
import pytest

from az_artifacts import (
    AuthenticationError,
    BearerToken,
    LimitedPackageMetadata,
    LimitedPackageMetadataListResponse,
    NotFoundError,
    PackageMetadata,
    PermissionDeniedError,
    ProtocolError,
    ServiceError,
    TransportError,
    UniversalPackageClient,
)
from az_artifacts._download import Downloader

METHODS = ["get_package_metadata", "get_package_versions_metadata"]


@pytest.fixture(autouse=True)
def metadata_defaults(service):
    service.metadata_intent = None


def read_metadata(client, method, **options):
    arguments = {"feed": "feed", "name": "package"}
    if method == "get_package_metadata":
        arguments["version"] = "1.2.3"
    return getattr(client, method)(**(arguments | options))


@pytest.mark.parametrize("description", [None, "", "A package"])
def test_exact_metadata(client, service, description):
    service.metadata["description"] = description
    result = client.get_package_metadata(feed="feed", name="package", version="1.2.3")
    assert result == PackageMetadata(
        "1.2.3",
        service.metadata["manifestId"].upper(),
        service.metadata["superRootId"].upper(),
        0,
        description,
    )
    assert service.requests[-1].url.path == (
        "/org/_packaging/feed/upack/packages/package/versions/1.2.3"
    )
    assert not service.requests[-1].url.params


def test_exact_prerelease_does_not_query_catalog(client, service):
    service.metadata["version"] = "1.2.3-rc.1"
    result = client.get_package_metadata(feed="feed", name="package", version="1.2.3-rc.1")
    assert result.version == "1.2.3-rc.1"
    assert len(service.requests) == 2
    assert service.requests[-1].url.path.endswith("/versions/1.2.3-rc.1")


@pytest.mark.parametrize("intent", [None, "Download", "Inspect", " custom / intent? "])
def test_optional_intent_is_forwarded_unchanged(client, service, intent):
    service.metadata_intent = intent
    client.get_package_metadata(feed="feed", name="package", version="1.2.3", intent=intent)
    expected = {} if intent is None else {"intent": intent}
    assert service.requests[-1].url.params == httpx.QueryParams(expected)


def test_limited_metadata_retains_count_prereleases_and_order(client, service):
    service.versions_metadata = {
        "count": 10,
        "value": [
            {"version": "2.0.0-rc.1", "description": ""},
            {"version": "1.0.0", "description": "First"},
            {"version": "1.1.0"},
        ],
    }
    result = client.get_package_versions_metadata(feed="feed", name="package")
    assert result == LimitedPackageMetadataListResponse(
        10,
        (
            LimitedPackageMetadata("2.0.0-rc.1", ""),
            LimitedPackageMetadata("1.0.0", "First"),
            LimitedPackageMetadata("1.1.0"),
        ),
    )
    assert service.requests[-1].url.path == "/org/_packaging/feed/upack/packages/package/versions"
    assert not service.requests[-1].url.params


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(
    ("scope", "project", "prefix"),
    [
        ("organization", None, b"/org/"),
        ("project", "My Project/#", b"/org/My%20Project%2F%23/"),
    ],
)
def test_scope_and_encoded_route_segments(client, service, method, scope, project, prefix):
    read_metadata(
        client, method, feed="Shared Feed/100%?#", scope=scope, project=project, name="my.package-1"
    )
    expected = (
        prefix + b"_packaging/Shared%20Feed%2F100%25%3F%23/upack/packages/my.package-1/versions"
    )
    if method == "get_package_metadata":
        expected += b"/1.2.3"
    assert service.requests[-1].url.raw_path == expected


@pytest.mark.parametrize("method", METHODS)
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
def test_invalid_shared_arguments_fail_before_requests(client, service, method, options):
    with pytest.raises((ValueError, TypeError)):
        read_metadata(client, method, **options)
    assert service.requests == []


@pytest.mark.parametrize(
    "version",
    ["*", "1.*", "1.2.*", "", "latest", "1.2", "1.2.3-RC.1", "1.2.3-01", "1.2.3+build", None, 1],
)
def test_exact_version_is_required_before_requests(client, service, version):
    with pytest.raises((ValueError, TypeError)):
        client.get_package_metadata(feed="feed", name="package", version=version)
    assert service.requests == []


@pytest.mark.parametrize("intent", ["", 1, False, [], {}])
def test_invalid_intent_fails_before_requests(client, service, intent):
    with pytest.raises(ValueError, match="intent"):
        client.get_package_metadata(feed="feed", name="package", version="1.2.3", intent=intent)
    assert service.requests == []


@pytest.mark.parametrize("method", METHODS)
def test_metadata_arguments_are_keyword_only(client, service, method):
    with pytest.raises(TypeError):
        getattr(client, method)("feed", "package")
    assert service.requests == []


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("advertise_packaging", [False, True])
def test_metadata_does_not_require_dedup(client, service, method, advertise_packaging):
    service.services = (
        [area for area in service.services if area["name"] == "Packaging"]
        if advertise_packaging
        else []
    )
    read_metadata(client, method)
    assert len(service.requests) == 2
    assert service.requests[-1].url.host == "pkgs.dev.azure.com"
    assert not service.resolve_counts


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(
    "area",
    [
        {"name": "Packaging"},
        {"name": "UPackPackaging"},
        {"name": "Renamed", "id": "D397749B-F115-4027-B6DD-77A65DD10D21"},
    ],
)
def test_discovered_packaging_url_and_resource_id(service, method, area):
    service.services = [
        {"name": "Packaging", "locationUrl": "https://pkgs.dev.azure.com/unused"},
        area | {"locationUrl": "https://custom.pkgs.dev.azure.com/location/"},
    ]

    def handler(request):
        if request.url.path.endswith("/ResourceAreas"):
            return service(request)
        service.requests.append(request)
        assert request.url.host == "custom.pkgs.dev.azure.com"
        assert request.url.path.startswith("/location/_packaging/")
        assert request.headers["authorization"] == "Basic OnRlc3QtcGF0"
        response = (
            service.metadata if method == "get_package_metadata" else service.versions_metadata
        )
        return httpx.Response(200, json=response)

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
    ) as client:
        read_metadata(client, method)
        assert client.discover_services()[area["name"].lower()] == (
            "https://custom.pkgs.dev.azure.com/location"
        )
    assert len(service.requests) == 2


@pytest.mark.parametrize("method", METHODS)
def test_metadata_uses_supplied_bearer_credential(service, method):
    def handler(request):
        service.requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if request.url.path.endswith("/ResourceAreas"):
            return httpx.Response(200, json={"value": service.services})
        response = (
            service.metadata if method == "get_package_metadata" else service.versions_metadata
        )
        return httpx.Response(200, json=response)

    with UniversalPackageClient(
        "org",
        credential=BearerToken("test-token"),
        transport=httpx.MockTransport(handler),
        retries=0,
    ) as client:
        read_metadata(client, method)
    assert len(service.requests) == 2


def test_discovery_is_shared_with_download(client, service, tmp_path):
    client.get_package_metadata(feed="feed", name="package", version="1.2.3")
    client.get_package_versions_metadata(feed="feed", name="package")
    service.metadata_intent = "Download"
    service.metadata["description"] = "Downloaded description"
    result = client.download(feed="feed", name="package", version="1.2.3", path=tmp_path)
    assert result.metadata.description == "Downloaded description"
    assert sum(request.url.path.endswith("/ResourceAreas") for request in service.requests) == 1
    assert [
        request.url.params.get("intent")
        for request in service.requests
        if "/_packaging/" in request.url.path
    ] == [None, None, "Download"]


@pytest.mark.parametrize("method", METHODS)
def test_closed_client_rejects_metadata_even_after_discovery(client, service, method):
    client.discover_services()
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        read_metadata(client, method)
    with pytest.raises(RuntimeError, match="closed"):
        client.discover_services()
    with pytest.raises(RuntimeError, match="closed"):
        client.__enter__()
    assert len(service.requests) == 1


def test_wrong_exact_version_is_an_error(client, service):
    service.metadata["version"] = "1.2.4"
    with pytest.raises(ProtocolError, match="does not match"):
        client.get_package_metadata(feed="feed", name="package", version="1.2.3")
    assert len(service.requests) == 2


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("wire", [None, {}, [], {"description": False}])
def test_malformed_metadata_response_is_an_error(client, service, method, wire):
    if method == "get_package_metadata":
        service.metadata = wire
    else:
        service.versions_metadata = wire
    with pytest.raises(ProtocolError):
        read_metadata(client, method)
    assert len(service.requests) == 2


@pytest.mark.parametrize(
    ("status", "headers"),
    [(206, {}), (200, {"x-ms-continuationtoken": "next"})],
)
def test_partial_limited_metadata_is_an_error(client, service, status, headers):
    service.versions_metadata_status = status
    service.versions_metadata_headers = headers
    with pytest.raises(ProtocolError, match="partial or continued"):
        client.get_package_versions_metadata(feed="feed", name="package")
    assert len(service.requests) == 2


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (429, ServiceError),
        (500, ServiceError),
    ],
)
def test_service_errors_propagate_without_endpoint_fallback(service, method, status, error):
    def handler(request):
        if request.url.path.endswith("/ResourceAreas"):
            return service(request)
        service.requests.append(request)
        return httpx.Response(status, text="private body", headers={"x-vss-e2eid": "request-id"})

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
    ) as client:
        with pytest.raises(error) as caught:
            read_metadata(client, method)
    assert caught.value.status_code == status
    assert caught.value.request_id == "request-id"
    assert "private body" not in str(caught.value)
    assert len(service.requests) == 2


@pytest.mark.parametrize("method", METHODS)
def test_transport_errors_propagate(service, method):
    def handler(request):
        if request.url.path.endswith("/ResourceAreas"):
            return service(request)
        service.requests.append(request)
        raise httpx.ReadTimeout("private details")

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
    ) as client:
        with pytest.raises(TransportError, match="Unable to complete"):
            read_metadata(client, method)
    assert len(service.requests) == 2


@pytest.mark.parametrize("method", METHODS)
def test_metadata_never_downloads_blobs_or_touches_files(client, service, method, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Metadata must not use the filesystem or downloader")

    with monkeypatch.context() as patch:
        patch.setattr(Downloader, "download", unexpected)
        patch.setattr(Path, "open", unexpected)
        patch.setattr(Path, "mkdir", unexpected)
        patch.setattr(builtins, "open", unexpected)
        patch.setattr(os, "open", unexpected)
        read_metadata(client, method)
    assert len(service.requests) == 2
    assert all(request.method == "GET" for request in service.requests)
    assert service.requests[0].url.path.endswith("/ResourceAreas")
    assert service.requests[1].url.path.startswith("/org/_packaging/")
    assert not service.resolve_counts
