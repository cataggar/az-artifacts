import json
import ntpath
from threading import Barrier, Lock
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from conftest import PACKAGE_ID

from az_artifacts import (
    IntegrityError,
    NoMatchingFilesError,
    ProtocolError,
    UniversalPackageClient,
    UnsafePathError,
    VersionNotFoundError,
    _paths,
)
from az_artifacts.models import BlobRef


def download(client, tmp_path, **options):
    return client.download(
        feed=options.pop("feed", "feed"),
        name="package",
        version=options.pop("version", "1.2.3"),
        path=tmp_path,
        **options,
    )


def test_raw_file_and_empty_file(client, service, tmp_path):
    service.manifest({"/dir/file.txt": service.chunk(b"hello"), "/empty": service.chunk(b"")})
    result = download(client, tmp_path)
    assert (tmp_path / "dir/file.txt").read_bytes() == b"hello"
    assert (tmp_path / "empty").read_bytes() == b""
    assert result.path == tmp_path.resolve()
    assert [path.as_posix() for path in result.files] == ["dir/file.txt", "empty"]
    assert result.bytes_downloaded == 5
    assert result.metadata.version == "1.2.3"
    assert any("/org/_packaging/feed/" in request.url.path for request in service.requests)


def test_recursive_nodes_repeated_chunks_and_compression(client, service, tmp_path):
    a = service.chunk(b"AAAA", wire=b"\x00\x00\x00\x40A\x00\x00")
    b = service.chunk(b"raw")
    child = service.node([a, b, a])
    root = service.node([child, b])
    service.manifest({"/file": root})
    result = download(client, tmp_path)
    assert (tmp_path / "file").read_bytes() == b"AAAArawAAAAraw"
    assert result.bytes_downloaded == 14


def test_direct_root_can_be_compressed(client, service, tmp_path):
    service.manifest({"/file": service.chunk(b"AAAA", wire=b"\x00\x00\x00\x40A\x00\x00")})
    download(client, tmp_path)
    assert (tmp_path / "file").read_bytes() == b"AAAA"


def test_chunked_manifest(client, service, tmp_path):
    service.manifest({"/file": service.chunk(b"content")}, chunked=True)
    download(client, tmp_path)
    assert (tmp_path / "file").read_bytes() == b"content"


def test_large_node_resolves_children_in_bounded_batches(client, service, tmp_path):
    chunks = [service.chunk(str(i).encode()) for i in range(205)]
    service.manifest({"/file": service.node(chunks)})
    download(client, tmp_path)
    assert (tmp_path / "file").read_bytes() == b"".join(str(i).encode() for i in range(205))
    batches = [
        json.loads(request.content)
        for request in service.requests
        if request.url.path.endswith("/dedup/urls")
    ]
    assert max(map(len, batches)) == 100


def test_worker_pool_downloads_multiple_files(service, tmp_path):
    files = {f"/file{i}": service.chunk(str(i).encode()) for i in range(4)}
    service.manifest(files)
    identifiers = {ref.id for ref in files.values()}
    barrier = Barrier(2, timeout=10)
    lock = Lock()
    active = 0
    peak = 0

    def handler(request):
        nonlocal active, peak
        is_content = (
            request.url.host == "blob.example" and request.url.path.strip("/") in identifiers
        )
        if not is_content:
            return service(request)
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            barrier.wait()
            return service(request)
        finally:
            with lock:
                active -= 1

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), max_workers=2
    ) as client:
        result = download(client, tmp_path)
    assert len(result.files) == 4
    assert peak == 2
    for i in range(4):
        assert (tmp_path / f"file{i}").read_bytes() == str(i).encode()


def test_filters_skip_unselected_blob_downloads(client, service, tmp_path):
    selected = service.chunk(b"selected")
    excluded = service.chunk(b"excluded")
    service.manifest({"/root.txt": selected, "/dir/other.bin": excluded})
    result = download(client, tmp_path, file_filter="**/*.txt")
    assert len(result.files) == 1
    assert result.bytes_downloaded == len(b"selected")
    assert not (tmp_path / "dir").exists()
    assert excluded.id not in service.resolve_counts


def test_no_matching_files_is_explicit(client, service, tmp_path):
    service.manifest({"/file.txt": service.chunk(b"content")})
    with pytest.raises(NoMatchingFilesError):
        download(client, tmp_path, file_filter="*.bin")


def test_project_and_feed_are_url_encoded(client, service, tmp_path):
    download(client, tmp_path, scope="project", project="My Project", feed="Shared Feed")
    assert any(
        b"/org/My%20Project/_packaging/Shared%20Feed/" in request.url.raw_path
        for request in service.requests
    )


def test_empty_package(client, tmp_path):
    result = download(client, tmp_path)
    assert result.files == ()
    assert result.bytes_downloaded == 0


def test_discovery_is_cached(client, service, tmp_path):
    download(client, tmp_path)
    download(client, tmp_path)
    assert sum(request.url.path.endswith("ResourceAreas") for request in service.requests) == 1
    locations = client.discover_services()
    locations.clear()
    assert client.discover_services()


def test_packaging_url_fallback(client, service, tmp_path):
    service.services = [area for area in service.services if area["name"] == "Dedup"]
    download(client, tmp_path)


def test_dedup_service_required(client, service, tmp_path):
    service.services = []
    with pytest.raises(ProtocolError, match="dedup"):
        download(client, tmp_path)


def test_missing_blob_url(client, service, tmp_path):
    ref = service.chunk(b"file")
    service.manifest({"/file": ref})
    service.missing_urls.add(ref.id)
    with pytest.raises(ProtocolError, match="blob URL"):
        download(client, tmp_path)


def test_expired_signed_url_is_resolved_once_more(client, service, tmp_path):
    ref = service.chunk(b"file")
    service.manifest({"/file": ref})
    service.expire.add(ref.id)
    download(client, tmp_path)
    assert service.resolve_counts[ref.id] == 2
    assert (tmp_path / "file").read_bytes() == b"file"


def test_existing_file_is_replaced_only_on_success(client, service, tmp_path):
    (tmp_path / "file").write_bytes(b"old")
    service.manifest({"/file": service.chunk(b"new")})
    download(client, tmp_path)
    assert (tmp_path / "file").read_bytes() == b"new"


def test_failed_file_preserves_existing_content_and_cleans_temporary(client, service, tmp_path):
    (tmp_path / "file").write_bytes(b"old")
    service.manifest({"/file": service.chunk(b"new", wire=b"broken")})
    with pytest.raises(IntegrityError):
        download(client, tmp_path)
    assert (tmp_path / "file").read_bytes() == b"old"
    assert list(tmp_path.glob(".az-artifacts-*")) == []


def test_no_overwrite(client, service, tmp_path):
    ref = service.chunk(b"new")
    service.manifest({"/file": ref})
    (tmp_path / "file").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        download(client, tmp_path, overwrite=False)
    assert (tmp_path / "file").read_bytes() == b"old"
    assert ref.id not in service.resolve_counts


def test_no_overwrite_creates_file_atomically(client, service, tmp_path):
    service.manifest({"/file": service.chunk(b"new")})
    download(client, tmp_path, overwrite=False)
    assert (tmp_path / "file").read_bytes() == b"new"
    assert list(tmp_path.glob(".az-artifacts-*")) == []


def test_node_logical_size_is_checked(client, service, tmp_path):
    root = service.node([service.chunk(b"abc")])
    service.manifest({"/file": BlobRef(root.id, root.size + 1)})
    with pytest.raises(IntegrityError, match="logical size"):
        download(client, tmp_path)
    assert not (tmp_path / "file").exists()


def test_deep_tree_is_rejected(client, service, tmp_path):
    root = service.chunk(b"data")
    for _ in range(65):
        root = service.node([root])
    service.manifest({"/file": root})
    with pytest.raises(ProtocolError, match="depth"):
        download(client, tmp_path)
    assert not (tmp_path / "file").exists()
    assert list(tmp_path.glob(".az-artifacts-*")) == []


def test_manifest_size_limit(service, tmp_path):
    service.manifest({"/file": service.chunk(b"abc")})
    with UniversalPackageClient(
        "org",
        credential="test-pat",
        transport=httpx.MockTransport(service),
        max_manifest_bytes=10,
    ) as client:
        with pytest.raises(ProtocolError):
            download(client, tmp_path)


def test_latest_matching_stable_version(client, service, tmp_path):
    service.versions = [
        "1.2.3",
        "1.9.9",
        "1.10.0",
        "1.11.0-rc.1",
        "2.0.0",
        {"version": "1.12.0", "isDeleted": True},
    ]
    service.metadata["version"] = "1.10.0"
    result = download(client, tmp_path, version="1.*")
    assert result.metadata.version == "1.10.0"
    assert any(request.url.path.endswith("/versions/1.10.0") for request in service.requests)


@pytest.mark.parametrize(
    ("pattern", "expected"), [("*", "2.0.0"), ("1.2.*", "1.2.10"), ("0.*", "0.1.0")]
)
def test_other_version_patterns(client, service, tmp_path, pattern, expected):
    service.versions = ["0.1.0", "1.2.3", "1.2.10", "2.0.0"]
    service.metadata["version"] = expected
    assert download(client, tmp_path, version=pattern).metadata.version == expected


def test_exact_prerelease_does_not_list_versions(client, service, tmp_path):
    service.metadata["version"] = "1.2.3-rc.1"
    download(client, tmp_path, version="1.2.3-rc.1")
    assert not any(request.url.host == "feeds.dev.azure.com" for request in service.requests)


def test_no_stable_version_match(client, service, tmp_path):
    service.versions = ["1.2.3-rc.1"]
    with pytest.raises(VersionNotFoundError):
        download(client, tmp_path, version="*")


def test_project_scoped_version_resolution(client, service, tmp_path):
    download(client, tmp_path, version="*", scope="project", project="My Project")
    assert all(
        b"/org/My%20Project/_apis/packaging/" in request.url.raw_path
        for request in service.requests
        if request.url.host == "feeds.dev.azure.com"
    )


def test_wildcards_share_discovered_feed_service_and_cache(service, tmp_path):
    service.services.append(
        {
            "name": "Renamed",
            "id": "7ab4e64e-c4d8-4f50-ae73-5ef2e21642a5",
            "locationUrl": "https://custom.feeds.dev.azure.com/catalog/",
        }
    )

    def handler(request):
        if request.url.host != "custom.feeds.dev.azure.com":
            return service(request)
        service.requests.append(request)
        assert request.url.path.startswith("/catalog/_apis/packaging/Feeds/feed/packages")
        values = (
            service.package_pages[0]
            if request.url.path.endswith("/packages")
            else [{"version": "1.2.3"}]
        )
        return httpx.Response(200, json={"value": values})

    with UniversalPackageClient(
        "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
    ) as client:
        assert client.package_version_exists(feed="feed", name="package", version="1.2.3")
        assert download(client, tmp_path, version="*").metadata.version == "1.2.3"
    assert sum(r.url.path.endswith("ResourceAreas") for r in service.requests) == 1
    assert sum(r.url.host == "custom.feeds.dev.azure.com" for r in service.requests) == 4


def test_package_name_substrings_do_not_match(client, service, tmp_path):
    service.package_pages = [[{"id": PACKAGE_ID, "name": "package-extra"}]]
    with pytest.raises(VersionNotFoundError):
        download(client, tmp_path, version="*")


def test_package_search_matches_exact_name_and_paginates(client, service, tmp_path):
    service.package_pages = [
        [{"id": str(UUID(int=i + 1)), "name": f"package-{i}"} for i in range(100)],
        [{"id": PACKAGE_ID, "name": "Package", "normalizedName": "package"}],
    ]
    download(client, tmp_path, version="*")
    listings = [r for r in service.requests if r.url.path.endswith("/packages")]
    assert [request.url.params["$skip"] for request in listings] == ["0", "100"]


def test_repeating_package_page_is_rejected(client, service, tmp_path):
    page = [{"id": str(UUID(int=i + 1)), "name": f"package-{i}"} for i in range(100)]
    service.package_pages = [page, page]
    with pytest.raises(ProtocolError, match="pagination"):
        download(client, tmp_path, version="*")


def test_wrong_metadata_version_is_rejected(client, service, tmp_path):
    service.metadata["version"] = "9.9.9"
    with pytest.raises(ProtocolError, match="version"):
        download(client, tmp_path)


@pytest.mark.parametrize(
    "options",
    [
        {"scope": "project"},
        {"project": "project"},
        {"scope": "other"},
        {"version": "latest"},
        {"feed": ".."},
        {"overwrite": "yes"},
        {"file_filter": []},
        {"file_filter": "!"},
        {"file_filter": [None]},
        {"file_filter": b"*"},
    ],
)
def test_invalid_options_do_not_send_requests(client, service, tmp_path, options):
    with pytest.raises(ValueError):
        download(client, tmp_path, **options)
    assert service.requests == []


@pytest.mark.parametrize(
    "organization",
    [
        "http://dev.azure.com/org",
        "https://dev.azure.com",
        "https://example.com/org",
        "https://dev.azure.com/org/project",
        "https://dev.azure.com/org?query=value",
        "https://dev.azure.com/org%2Fproject",
    ],
)
def test_invalid_organizations(organization):
    with pytest.raises((ValueError, ProtocolError)):
        UniversalPackageClient(organization, credential="pat")


def test_legacy_organization_url():
    with UniversalPackageClient(
        "https://org.visualstudio.com/",
        credential="pat",
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
    ) as client:
        assert client.organization == "https://org.visualstudio.com"


@pytest.mark.parametrize(
    "options",
    [
        {"credential": ""},
        {"max_workers": 0},
        {"max_workers": True},
        {"retries": -1},
        {"timeout": 0},
        {"timeout": float("nan")},
        {"max_manifest_bytes": 0},
    ],
)
def test_invalid_client_options(options):
    with pytest.raises(ValueError):
        UniversalPackageClient("org", **({"credential": "pat"} | options))


def test_closed_client(client, tmp_path):
    client.close()
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        download(client, tmp_path)


@pytest.mark.parametrize("paths", [("safe", "../bad"), ("safe", "/safe"), ("dir", "dir/file")])
def test_download_rejects_invalid_unselected_paths_without_writes(client, service, tmp_path, paths):
    ref = service.chunk(b"file")
    service.manifest(dict.fromkeys(paths, ref))
    output = tmp_path / "output"
    with pytest.raises(UnsafePathError):
        download(client, output, file_filter="unmatched")
    assert not output.exists()
    assert ref.id not in service.resolve_counts


@pytest.mark.parametrize("paths", [("safe", "CON"), ("A", "a"), ("DIR", "dir/file")])
def test_download_host_guards_apply_to_unselected_files(
    client, service, tmp_path, monkeypatch, paths
):
    monkeypatch.setattr(_paths, "os", SimpleNamespace(name="nt", path=ntpath))
    ref = service.chunk(b"file")
    service.manifest(dict.fromkeys(paths, ref))
    output = tmp_path / "output"
    with pytest.raises(UnsafePathError):
        download(client, output, file_filter="unmatched")
    assert not output.exists()
    assert ref.id not in service.resolve_counts
