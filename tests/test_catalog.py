import builtins
import os
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from conftest import FEED_ID, PACKAGE_ID, PROJECT_ID

from az_artifacts import (
    ArtifactsError,
    AuthenticationError,
    Feed,
    NotFoundError,
    Package,
    PackageNotFoundError,
    PackageVersion,
    PermissionDeniedError,
    ProjectReference,
    ProtocolError,
    ServiceError,
    TransportError,
    UniversalPackageClient,
)
from az_artifacts._download import Downloader

METHODS = ["list_feeds", "list_packages", "list_package_versions", "package_version_exists"]
FEED_METHODS = METHODS[1:]
NAMED_METHODS = METHODS[2:]


def call_catalog(client, method, **options):
    arguments = {}
    if method in FEED_METHODS:
        arguments["feed"] = "feed"
    if method in NAMED_METHODS:
        arguments["name"] = "package"
    if method == "package_version_exists":
        arguments["version"] = "1.2.3"
    return getattr(client, method)(**(arguments | options))


def read_catalog(client, method, **options):
    result = call_catalog(client, method, **options)
    return tuple(result) if method == "list_packages" else result


def records(start, count):
    return [
        {"id": str(UUID(int=i + 1)), "name": f"package-{i}"} for i in range(start, start + count)
    ]


def catalog_requests(service):
    return [request for request in service.requests if "/_apis/packaging/" in request.url.path]


def test_list_feeds_retains_actual_project_association(client, service):
    service.feeds.append(
        {
            "id": PACKAGE_ID,
            "name": "project-feed",
            "description": "",
            "project": {"id": PROJECT_ID, "name": "Actual Project", "visibility": "private"},
        }
    )
    expected = (
        Feed(FEED_ID, "feed"),
        Feed(
            PACKAGE_ID,
            "project-feed",
            project=ProjectReference(PROJECT_ID, "Actual Project", "private"),
            description="",
        ),
    )
    assert client.list_feeds() == expected
    assert client.list_feeds(project="Query Project") == expected
    requests = catalog_requests(service)
    assert requests[0].url.path == "/org/_apis/packaging/Feeds"
    assert requests[1].url.raw_path == (
        b"/org/Query%20Project/_apis/packaging/Feeds?api-version=7.1"
    )
    assert all(
        request.url.params == httpx.QueryParams({"api-version": "7.1"}) for request in requests
    )


def test_empty_feeds_and_packages(client, service):
    service.feeds = []
    service.package_pages = []
    assert client.list_feeds() == ()
    assert tuple(client.list_packages(feed="feed")) == ()


@pytest.mark.parametrize("final_count", [0, 1])
def test_package_pagination_is_lazy_and_complete(client, service, final_count):
    service.package_pages = [records(0, 2), records(2, 2), records(4, final_count)]
    packages = client.list_packages(feed="feed", page_size=2)
    assert iter(packages) is packages
    assert service.requests == []
    assert next(packages) == Package(str(UUID(int=1)), "package-0")
    assert len(catalog_requests(service)) == 1
    assert next(packages).name == "package-1"
    assert len(catalog_requests(service)) == 1
    assert [package.name for package in packages] == [
        f"package-{i}" for i in range(2, 4 + final_count)
    ]
    assert [r.url.params["$skip"] for r in catalog_requests(service)] == ["0", "2", "4"]
    for request in catalog_requests(service):
        assert request.url.params == httpx.QueryParams(
            {
                "api-version": "7.1",
                "protocolType": "upack",
                "includeDeleted": "false",
                "includeAllVersions": "false",
                "getTopPackageVersions": "false",
                "$top": "2",
                "$skip": request.url.params["$skip"],
            }
        )


def test_substring_query_is_forwarded_not_exact_filtered(client, service):
    service.package_pages = [records(0, 2)]
    assert len(tuple(client.list_packages(feed="feed", name_query="package-"))) == 2
    assert catalog_requests(service)[0].url.params["packageNameQuery"] == "package-"


def test_version_lookup_uses_all_candidate_pages_and_exact_identity(client, service):
    service.package_pages = [
        records(0, 100),
        [{"id": PACKAGE_ID, "name": "Package", "normalizedName": "package"}],
    ]
    assert client.list_package_versions(feed="feed", name="package") == (PackageVersion("1.2.3"),)
    requests = catalog_requests(service)
    assert [r.url.params["$skip"] for r in requests[:-1]] == ["0", "100"]
    assert all(r.url.params["packageNameQuery"] == "package" for r in requests[:-1])
    assert requests[-1].url.path.endswith(f"/packages/{PACKAGE_ID}/versions")


@pytest.mark.parametrize("method", NAMED_METHODS)
def test_substring_is_established_missing_not_http_404(client, service, method):
    service.package_pages = [[{"id": PACKAGE_ID, "name": "package-extra"}]]
    if method == "list_package_versions":
        with pytest.raises(PackageNotFoundError) as caught:
            read_catalog(client, method)
        assert isinstance(caught.value, ArtifactsError)
        assert not isinstance(caught.value, NotFoundError)
        assert not hasattr(caught.value, "status_code")
    else:
        assert read_catalog(client, method) is False
    assert len(catalog_requests(service)) == 1


@pytest.mark.parametrize("method", NAMED_METHODS)
def test_normalized_name_not_display_name_controls_identity(client, service, method):
    service.package_pages = [
        [{"id": PACKAGE_ID, "name": "package", "normalizedName": "package-extra"}]
    ]
    if method == "list_package_versions":
        with pytest.raises(PackageNotFoundError):
            read_catalog(client, method)
    else:
        assert read_catalog(client, method) is False


@pytest.mark.parametrize("method", NAMED_METHODS)
def test_later_bad_candidate_is_not_silently_ignored(client, service, method):
    service.package_pages = [
        [{"id": PACKAGE_ID, "name": "package"}, *records(0, 99)],
        [{"id": str(UUID(int=100)), "name": "package-extra", "normalizedName": 1}],
    ]
    with pytest.raises(ProtocolError):
        read_catalog(client, method)
    assert len(catalog_requests(service)) == 2


def test_ambiguous_exact_name_is_not_arbitrarily_selected(client, service):
    service.package_pages = [
        [{"id": PACKAGE_ID, "name": "package"}, {"id": FEED_ID, "name": "package"}]
    ]
    with pytest.raises(ProtocolError, match="ambiguous"):
        client.list_package_versions(feed="feed", name="package")


@pytest.mark.parametrize("include_deleted", [False, True])
def test_versions_keep_order_prereleases_and_optional_deletion_states(
    client, service, include_deleted
):
    service.versions = [
        {"version": "2.0.0-rc.1", "isDeleted": False},
        {"version": "1.0.0", "isDeleted": True},
        {"version": "1.2.3"},
    ]
    result = client.list_package_versions(
        feed="feed", name="package", include_deleted=include_deleted
    )
    expected = [PackageVersion("2.0.0-rc.1", is_deleted=False)]
    if include_deleted:
        expected.append(PackageVersion("1.0.0", is_deleted=True))
    expected.append(PackageVersion("1.2.3"))
    assert result == tuple(expected)
    requests = catalog_requests(service)
    assert requests[0].url.params["includeDeleted"] == str(include_deleted).lower()
    params = {"api-version": "7.1"}
    if not include_deleted:
        params["isDeleted"] = "false"
    assert requests[-1].url.params == httpx.QueryParams(params)


@pytest.mark.parametrize("version", ["1.2.3", "2.0.0-rc.1"])
def test_exists_supports_stable_and_prerelease_versions(client, service, version):
    service.versions = ["1.2.3", "2.0.0-rc.1"]
    assert client.package_version_exists(feed="feed", name="package", version=version) is True


def test_exists_uses_normalized_version_identity(client, service):
    service.versions = [{"version": "1.2.3", "normalizedVersion": "1.2.4"}]
    assert client.package_version_exists(feed="feed", name="package", version="1.2.3") is False
    assert client.package_version_exists(feed="feed", name="package", version="1.2.4") is True


@pytest.mark.parametrize("versions", [[], ["1.2.4"], [{"version": "1.2.3", "isDeleted": True}]])
def test_successful_live_version_absence(client, service, versions):
    service.versions = versions
    assert client.package_version_exists(feed="feed", name="package", version="1.2.3") is False
    assert len(catalog_requests(service)) == 2


def test_no_negative_or_positive_catalog_cache(client, service):
    service.package_pages = []
    assert not client.package_version_exists(feed="feed", name="package", version="1.2.3")
    service.package_pages = [[{"id": PACKAGE_ID, "name": "package"}]]
    service.versions = []
    assert not client.package_version_exists(feed="feed", name="package", version="1.2.3")
    service.versions = ["1.2.3"]
    assert client.package_version_exists(feed="feed", name="package", version="1.2.3")
    service.versions = []
    assert not client.package_version_exists(feed="feed", name="package", version="1.2.3")
    assert sum(r.url.path.endswith("ResourceAreas") for r in service.requests) == 1
    assert len(catalog_requests(service)) == 7


@pytest.mark.parametrize("method", METHODS)
def test_catalog_requires_no_dedup_or_transfer_service(client, service, method):
    service.services = []
    read_catalog(client, method)
    assert all(r.url.host == "feeds.dev.azure.com" for r in catalog_requests(service))
    assert not service.resolve_counts


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize(
    "area",
    [
        {"name": "Feed"},
        {"name": "Renamed", "id": "7AB4E64E-C4D8-4F50-AE73-5EF2E21642A5"},
    ],
)
def test_feed_service_discovery_by_name_or_id_preserves_public_mapping(service, method, area):
    location = "https://custom.feeds.dev.azure.com/location"
    service.services = [
        {"name": "Feed", "locationUrl": "https://feeds.dev.azure.com/unused"},
        area | {"locationUrl": location + "/"},
    ]

    def handler(request):
        if request.url.path.endswith("/ResourceAreas"):
            return service(request)
        service.requests.append(request)
        assert request.url.host == "custom.feeds.dev.azure.com"
        assert request.url.path.startswith("/location/_apis/packaging/Feeds")
        assert request.headers["authorization"] == "Basic OnRlc3QtcGF0"
        assert request.headers["accept"] == "application/json; api-version=7.1"
        if request.url.path.endswith("/Feeds"):
            values = service.feeds
        elif request.url.path.endswith("/packages"):
            values = service.package_pages[0]
        else:
            values = [{"version": "1.2.3"}]
        return httpx.Response(200, json={"count": len(values), "value": values})

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
    ) as client:
        read_catalog(client, method)
        read_catalog(client, method)
        mapping = client.discover_services()
        assert mapping[area["name"].lower()] == location
        mapping.clear()
        assert client.discover_services()
    assert sum(r.url.path.endswith("ResourceAreas") for r in service.requests) == 1


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("project", [None, "My Project/#"])
def test_project_and_feed_route_encoding(client, service, method, project):
    options = {}
    if project is not None:
        options["project"] = project
        if method != "list_feeds":
            options["scope"] = "project"
    if method != "list_feeds":
        options["feed"] = "Shared Feed/100%?#"
    read_catalog(client, method, **options)
    prefix = b"/org/" + (b"My%20Project%2F%23/" if project else b"")
    prefix += b"_apis/packaging/Feeds"
    if method != "list_feeds":
        prefix += b"/Shared%20Feed%2F100%25%3F%23/packages"
    assert all(r.url.raw_path.startswith(prefix) for r in catalog_requests(service))


@pytest.mark.parametrize("organization", ["org name", "https://org.visualstudio.com/"])
def test_feed_fallback_uses_encoded_organization(organization, service):
    service.services = []
    with UniversalPackageClient(
        organization, credential="test-pat", transport=httpx.MockTransport(service), retries=0
    ) as client:
        client.list_feeds()
    expected = b"/org%20name/" if organization == "org name" else b"/org/"
    assert service.requests[-1].url.raw_path.startswith(expected + b"_apis/packaging/Feeds?")


@pytest.mark.parametrize("method", METHODS)
def test_keyword_only_and_closed_methods_fail_before_network(client, service, method):
    with pytest.raises(TypeError):
        getattr(client, method)("feed")
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        call_catalog(client, method)
    assert service.requests == []


@pytest.mark.parametrize("consumed", [0, 1, 2])
def test_advancing_iterator_after_close_rejects_even_buffered_entries(client, service, consumed):
    service.package_pages = [records(0, 2), records(2, 1)]
    packages = client.list_packages(feed="feed", page_size=2)
    for _ in range(consumed):
        next(packages)
    count = len(service.requests)
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        next(packages)
    assert len(service.requests) == count


@pytest.mark.parametrize("project", ["", " ", ".", "..", 0, False])
def test_list_feeds_validates_project_eagerly(client, service, project):
    with pytest.raises(ValueError):
        client.list_feeds(project=project)
    assert service.requests == []


@pytest.mark.parametrize("method", FEED_METHODS)
@pytest.mark.parametrize(
    "options",
    [
        {"feed": ""},
        {"feed": " "},
        {"feed": ".."},
        {"feed": None},
        {"feed": 1},
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
def test_shared_arguments_validated_before_requests(client, service, method, options):
    with pytest.raises((ValueError, TypeError)):
        call_catalog(client, method, **options)
    assert service.requests == []


@pytest.mark.parametrize("method", NAMED_METHODS)
@pytest.mark.parametrize("name", ["", "Uppercase", "a/b", "a..b", None, 1])
def test_exact_name_validation(client, service, method, name):
    with pytest.raises((ValueError, TypeError)):
        call_catalog(client, method, name=name)
    assert service.requests == []


@pytest.mark.parametrize(
    "options",
    [
        {"name_query": ""},
        {"name_query": 1},
        {"name_query": False},
        {"page_size": 0},
        {"page_size": -1},
        {"page_size": True},
        {"page_size": 1.0},
        {"page_size": "100"},
        {"page_size": None},
        {"page_size": 2_147_483_648},
    ],
)
def test_package_listing_arguments_validated_eagerly(client, service, options):
    with pytest.raises(ValueError):
        client.list_packages(feed="feed", **options)
    assert service.requests == []


@pytest.mark.parametrize("value", [None, 0, 1, "true", []])
def test_include_deleted_requires_boolean(client, service, value):
    with pytest.raises(ValueError, match="boolean"):
        client.list_package_versions(feed="feed", name="package", include_deleted=value)
    assert service.requests == []


@pytest.mark.parametrize(
    "version",
    ["*", "1.*", "1.2.*", "", "latest", "1.2", "1.2.3-RC.1", "1.2.3-01", "1.2.3+build", None, 1],
)
def test_existence_requires_valid_exact_version(client, service, version):
    with pytest.raises((ValueError, TypeError)):
        client.package_version_exists(feed="feed", name="package", version=version)
    assert service.requests == []


@pytest.mark.parametrize("cycle_length", [1, 2, 3, 5])
def test_repeated_and_cyclic_pages_fail_with_bounded_history(service, cycle_length):
    pages = [records(i * 2, 2) for i in range(cycle_length)]

    def handler(request):
        if request.url.path.endswith("/ResourceAreas"):
            return service(request)
        service.requests.append(request)
        page = int(request.url.params["$skip"]) // 2
        assert page < cycle_length * 4 + 3
        return httpx.Response(200, json={"value": pages[page % cycle_length]})

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
    ) as client:
        with pytest.raises(ProtocolError, match="pagination"):
            tuple(client.list_packages(feed="feed", page_size=2))


def test_duplicate_package_ids_within_page_are_rejected(client, service):
    service.package_pages = [[*records(0, 1), *records(0, 1)]]
    with pytest.raises(ProtocolError, match="duplicate"):
        tuple(client.list_packages(feed="feed"))


def test_oversized_page_is_not_yielded(client, service):
    service.package_pages = [records(0, 3)]
    with pytest.raises(ProtocolError, match="page size"):
        next(client.list_packages(feed="feed", page_size=2))


FAULT_TARGETS = [
    ("list_feeds", "/Feeds"),
    ("list_packages", "/packages"),
    ("list_package_versions", "/packages"),
    ("list_package_versions", "/versions"),
    ("package_version_exists", "/packages"),
    ("package_version_exists", "/versions"),
]


@pytest.mark.parametrize(("method", "suffix"), FAULT_TARGETS)
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
def test_http_errors_are_never_absence_or_endpoint_fallback(service, method, suffix, status, error):
    def handler(request):
        if not request.url.path.endswith(suffix):
            return service(request)
        service.requests.append(request)
        return httpx.Response(status, text="private details", headers={"x-vss-e2eid": "request-id"})

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
    ) as client:
        with pytest.raises(error) as caught:
            read_catalog(client, method)
    assert caught.value.status_code == status
    assert caught.value.request_id == "request-id"
    assert "private details" not in str(caught.value)
    assert sum(r.url.path.endswith(suffix) for r in service.requests) == 1


@pytest.mark.parametrize(("method", "suffix"), FAULT_TARGETS)
def test_network_errors_are_not_absence(service, method, suffix):
    def handler(request):
        if not request.url.path.endswith(suffix):
            return service(request)
        service.requests.append(request)
        raise httpx.ReadTimeout("private details")

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
    ) as client:
        with pytest.raises(TransportError, match="Unable to complete"):
            read_catalog(client, method)


@pytest.mark.parametrize(("method", "suffix"), FAULT_TARGETS)
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(206, json={"value": []}),
        httpx.Response(202, json={"value": []}),
        httpx.Response(200, json={"value": []}, headers={"x-ms-continuationtoken": "next"}),
        httpx.Response(200, json={"value": []}, headers={"content-range": "items 0-1/10"}),
        httpx.Response(200, json={"value": []}, headers={"link": '<https://other>; rel="next"'}),
        httpx.Response(200, json={"value": [], "continuationToken": "next"}),
        httpx.Response(200, json={"value": [], "nextLink": "next"}),
        httpx.Response(200, json={"value": [], "@odata.nextLink": "next"}),
        httpx.Response(200, json={"value": [], "continuationToken": False}),
        httpx.Response(200, json={"value": [], "count": 1}),
        httpx.Response(200, json={"value": [], "count": True}),
        httpx.Response(200, json={"value": None}),
        httpx.Response(200, json={"value": [None]}),
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"not JSON"),
    ],
)
def test_partial_malformed_or_continued_responses_never_imply_absence(
    service, method, suffix, response
):
    def handler(request):
        if not request.url.path.endswith(suffix):
            return service(request)
        service.requests.append(request)
        return response

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
    ) as client:
        with pytest.raises(ProtocolError):
            read_catalog(client, method)
    assert sum(r.url.path.endswith(suffix) for r in service.requests) == 1


@pytest.mark.parametrize("method", METHODS)
def test_catalog_does_not_use_payloads_or_filesystem(client, service, method, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Catalog must not use the filesystem or downloader")

    with monkeypatch.context() as patch:
        patch.setattr(Downloader, "download", unexpected)
        patch.setattr(Path, "open", unexpected)
        patch.setattr(Path, "mkdir", unexpected)
        patch.setattr(builtins, "open", unexpected)
        patch.setattr(os, "open", unexpected)
        read_catalog(client, method)
    assert not service.resolve_counts
    assert all(request.method == "GET" for request in service.requests)
    assert all(
        request.url.path.endswith("ResourceAreas") or "/_apis/packaging/Feeds" in request.url.path
        for request in service.requests
    )
