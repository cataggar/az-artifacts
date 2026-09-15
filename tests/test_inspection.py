import gc
import json
import ntpath
import os
import tempfile
import weakref
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import httpx
import pytest
from conftest import PACKAGE_ID, identifier

from az_artifacts import (
    AuthenticationError,
    FileVersion,
    IntegrityError,
    NotFoundError,
    PackageFile,
    PackageNotFoundError,
    PermissionDeniedError,
    ProtocolError,
    TransportError,
    UniversalPackageClient,
    UnsafePathError,
    _paths,
)
from az_artifacts._dedup import parse_node
from az_artifacts.models import BlobRef


@pytest.fixture(autouse=True)
def inspection_intent(service):
    service.metadata_intent = None


def inspect(client, operation="list_files", **kwargs):
    options = {"feed": "feed", "name": "package", **kwargs}
    if operation != "list_file_versions":
        options.setdefault("version", "1.2.3")
    if operation != "list_files":
        options.setdefault("relative_path", "dir/file.txt")
    result = getattr(client, operation)(**options)
    return tuple(result) if operation == "list_file_versions" else result


def history(service, entries):
    service.metadata_by_version = {}
    service.versions = list(entries)
    for version, files in entries.items():
        service.version = version
        service.manifest(files, chunked=True)
        service.metadata_by_version[version] = service.metadata


def metadata_requests(service):
    return [r for r in service.requests if "/upack/packages/" in r.url.path]


def assert_manifest_only(service, payload_ids):
    resolved = {
        key
        for request in service.requests
        if request.url.path.endswith("/dedup/urls")
        for key in json.loads(request.content)
    }
    fetched = {
        request.url.path.strip("/")
        for request in service.requests
        if request.url.host == "blob.example"
    }
    assert resolved
    assert resolved.isdisjoint(payload_ids)
    assert fetched.isdisjoint(payload_ids)
    assert not any(r.url.path.endswith("/Feeds") for r in service.requests)
    for request in service.requests:
        if request.url.host == "feeds.dev.azure.com":
            assert "/Feeds/feed/packages" in request.url.path
            if request.url.path.endswith("/packages"):
                assert request.url.params["packageNameQuery"] == "package"
            else:
                assert request.url.path.endswith(f"/{PACKAGE_ID}/versions")


@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
@pytest.mark.parametrize("chunked", [False, True])
def test_inspection_uses_only_manifest_blobs_without_local_io(
    client, service, monkeypatch, operation, chunked
):
    leaf = service.chunk(b"payload, not a manifest")
    payload = service.node([leaf, leaf])
    empty = service.chunk(b"")
    service.manifest({"/dir/file.txt": payload, "/empty": empty}, chunked=chunked)

    def forbidden(*args, **kwargs):
        pytest.fail("Inspection attempted local filesystem I/O")

    with monkeypatch.context() as patch:
        for method in ("open", "mkdir", "write_bytes", "write_text", "resolve", "exists"):
            patch.setattr(Path, method, forbidden)
        patch.setattr(tempfile, "mkstemp", forbidden)
        patch.setattr(os, "replace", forbidden)
        patch.setattr(os, "link", forbidden)
        result = inspect(client, operation)
    expected = PackageFile(PurePosixPath("dir/file.txt"), payload.size, payload.id)
    if operation == "list_files":
        assert result == (expected, PackageFile(PurePosixPath("empty"), 0, empty.id))
    elif operation == "file_exists":
        assert result is True
    else:
        assert result == (FileVersion("1.2.3", expected),)
    assert_manifest_only(service, {leaf.id, payload.id, empty.id})
    if operation == "list_files":
        assert not any(r.url.host == "feeds.dev.azure.com" for r in service.requests)


def test_models_are_frozen_and_exported():
    file = PackageFile(PurePosixPath("x"), 1, "00" * 32 + "01")
    record = FileVersion("1.2.3", file)
    with pytest.raises(FrozenInstanceError):
        file.size = 2
    with pytest.raises(FrozenInstanceError):
        record.version = "2.0.0"


@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
def test_empty_manifest(client, operation):
    result = inspect(client, operation)
    assert result == (False if operation == "file_exists" else ())


@pytest.mark.parametrize(
    ("patterns", "expected"),
    [
        (None, ["z.txt", "dir/a.txt", ".hidden", "B.TXT", "dir/b.bin", "a.md"]),
        ("**/*.{txt,md}", ["z.txt", "dir/a.txt", "a.md"]),
        (["**", "!**/*.txt", "dir/*.txt"], ["dir/a.txt", ".hidden", "B.TXT", "dir/b.bin", "a.md"]),
        ("*.txt", ["z.txt"]),
        ("**/!(*.bin)", ["z.txt", "dir/a.txt", ".hidden", "B.TXT", "a.md"]),
        ("missing/**", []),
        ("!**", []),
    ],
)
def test_listing_filters_and_order(client, service, patterns, expected):
    ref = service.chunk(b"data")
    service.manifest(
        dict.fromkeys(["/z.txt", "/dir/a.txt", "/.hidden", "/B.TXT", "/dir/b.bin", "/a.md"], ref)
    )
    assert [f.path.as_posix() for f in inspect(client, file_filter=patterns)] == expected
    assert ref.id not in service.resolve_counts


@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
def test_logical_paths_remain_case_sensitive_on_windows(client, service, monkeypatch, operation):
    monkeypatch.setattr(_paths, "os", SimpleNamespace(name="nt", path=ntpath))
    a, b = service.chunk(b"a"), service.chunk(b"b")
    service.manifest({"/Dir/File.txt": a, "/dir/file.txt": b, "/CON": a, "/name?": b})
    result = inspect(client, operation)
    if operation == "list_files":
        assert [f.path.as_posix() for f in result] == [
            "Dir/File.txt",
            "dir/file.txt",
            "CON",
            "name?",
        ]
    elif operation == "file_exists":
        assert result is True
        assert inspect(client, operation, relative_path="DIR/FILE.TXT") is False
    else:
        assert result[0].file.content_id == b.id
        assert inspect(client, operation, relative_path="DIR/FILE.TXT") == ()


def test_file_exists_treats_glob_metacharacters_as_literal(client, service):
    service.manifest({"*.txt": service.chunk(b"a"), "dir/file.txt": service.chunk(b"b")})
    assert inspect(client, "file_exists", relative_path="*.txt")
    assert not inspect(client, "file_exists", relative_path="**/*.txt")
    assert inspect(client, "file_exists", relative_path=PurePosixPath("dir/file.txt"))


@pytest.mark.parametrize(
    "paths",
    [
        ["../bad"],
        ["/"],
        ["//bad"],
        ["./bad"],
        ["a//b"],
        ["a/../b"],
        ["a\\b"],
        ["C:/bad"],
        ["a\x00b"],
        ["a\x7fb"],
        ["/a", "a"],
        ["a", "a/b"],
        ["a/b", "a"],
    ],
)
@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
def test_invalid_logical_manifests_fail_before_filtering(client, service, paths, operation):
    ref = service.chunk(b"payload")
    service.manifest(dict.fromkeys(paths, ref))
    options = {"file_filter": "not-present"} if operation == "list_files" else {}
    with pytest.raises(UnsafePathError):
        inspect(client, operation, **options)
    assert ref.id not in service.resolve_counts


@pytest.mark.parametrize(
    "data",
    [
        b"broken",
        b"\xff",
        b"null",
        b"{}",
        b'{"items":null}',
        b'{"items":[{}]}',
        b'{"items":[{"path":"x","blob":{"id":"bad","size":0}}]}',
        b'{"items":[{"path":"x","blob":{"id":"' + b"00" * 32 + b'01","size":true}}]}',
    ],
)
@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
def test_malformed_manifest_is_not_absence(client, service, data, operation):
    service.metadata["manifestId"] = service.chunk(data).id
    with pytest.raises(ProtocolError):
        inspect(client, operation)


@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
@pytest.mark.parametrize("chunked", [False, True])
def test_manifest_limit_is_shared(service, operation, chunked):
    service.manifest({"/dir/file.txt": service.chunk(b"data")}, chunked=chunked)
    with (
        UniversalPackageClient(
            "org",
            credential="test-pat",
            transport=httpx.MockTransport(service),
            max_manifest_bytes=10,
            retries=0,
        ) as client,
        pytest.raises(ProtocolError),
    ):
        inspect(client, operation)


@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
def test_metadata_version_mismatch(client, service, operation):
    service.metadata["version"] = "9.9.9"
    with pytest.raises(ProtocolError, match="version"):
        inspect(client, operation)
    assert not service.resolve_counts


@pytest.mark.parametrize(
    "options",
    [
        {"feed": ""},
        {"feed": None},
        {"name": "Invalid"},
        {"name": None},
        {"scope": "bad"},
        {"scope": "project"},
        {"scope": "project", "project": ""},
        {"project": "project"},
        {"scope": None},
        {"scope": "project", "project": 3},
    ],
)
@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
def test_common_invalid_arguments_are_eager(client, service, operation, options):
    arguments = {"feed": "feed", "name": "package", **options}
    if operation != "list_files":
        arguments["relative_path"] = "x"
    if operation != "list_file_versions":
        arguments["version"] = "1.2.3"
    with pytest.raises((ValueError, TypeError)):
        getattr(client, operation)(**arguments)
    assert service.requests == []


@pytest.mark.parametrize("version", ["*", "1.*", "latest", "", "1.2.3+build", None, 7, True])
@pytest.mark.parametrize("operation", ["list_files", "file_exists"])
def test_invalid_exact_version_before_requests(client, service, version, operation):
    with pytest.raises((ValueError, TypeError)):
        inspect(client, operation, version=version)
    assert service.requests == []


@pytest.mark.parametrize(
    "patterns",
    [
        "",
        [],
        ["ok", ""],
        ["ok", "!"],
        ["ok", None],
        b"*",
        1,
        {"a": "b"},
        {"*"},
    ],
)
def test_invalid_listing_filter_before_requests(client, service, patterns):
    with pytest.raises(ValueError):
        inspect(client, file_filter=patterns)
    assert service.requests == []


def test_filter_expansion_limit_fails_before_requests(client, service):
    from wcmatch._wcparse import PatternLimitException

    with pytest.raises(PatternLimitException):
        inspect(client, file_filter="{1..1001}")
    assert service.requests == []


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/",
        "/file",
        "//file",
        "./file",
        "a/../b",
        "a//b",
        "C:/file",
        "a\\b",
        "a\0b",
        Path("file"),
        PureWindowsPath("file"),
        PurePosixPath("/file"),
        None,
        1,
    ],
)
@pytest.mark.parametrize("operation", ["file_exists", "list_file_versions"])
def test_invalid_caller_paths_are_eager(client, service, operation, path):
    options = {"feed": "feed", "name": "package", "relative_path": path}
    if operation == "file_exists":
        options["version"] = "1.2.3"
    with pytest.raises((ValueError, UnsafePathError)):
        getattr(client, operation)(**options)
    assert service.requests == []


@pytest.mark.parametrize(
    "versions",
    [
        "1.2.3",
        b"1.2.3",
        1,
        ["*"],
        ["1.2.3", None],
        ["1.2.3", "latest"],
        {"1.2.3"},
        {"1.2.3": None},
    ],
)
def test_invalid_history_versions_are_eager(client, service, versions):
    with pytest.raises(ValueError):
        client.list_file_versions(feed="feed", name="package", relative_path="x", versions=versions)
    assert service.requests == []


def test_history_rejects_iterator_versions_eagerly(client, service):
    with pytest.raises(ValueError):
        client.list_file_versions(
            feed="feed", name="package", relative_path="x", versions=iter(["1.2.3"])
        )
    assert service.requests == []


@pytest.mark.parametrize("absence", ["package", "substring", "version", "deleted"])
def test_file_exists_established_absence_does_not_fetch_metadata(client, service, absence):
    if absence == "package":
        service.package_pages = [[]]
    elif absence == "substring":
        service.package_pages = [[{"id": PACKAGE_ID, "name": "package-extra"}]]
    elif absence == "version":
        service.versions = ["2.0.0"]
    else:
        service.versions = [{"version": "1.2.3", "isDeleted": True}]
    service.services = []
    assert inspect(client, "file_exists") is False
    assert not metadata_requests(service)
    assert not service.resolve_counts


def test_file_exists_does_not_cache_absence(client, service):
    service.package_pages = [[]]
    assert inspect(client, "file_exists") is False
    service.package_pages = [[{"id": PACKAGE_ID, "name": "package"}]]
    service.manifest({"/dir/file.txt": service.chunk(b"data")})
    assert inspect(client, "file_exists") is True
    service.versions = []
    assert inspect(client, "file_exists") is False


@pytest.mark.parametrize(
    ("operation", "where"),
    [
        (operation, where)
        for operation in ("list_files", "file_exists", "list_file_versions")
        for where in ("catalog", "metadata", "resolve", "blob")
        if operation != "list_files" or where != "catalog"
    ],
)
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (None, TransportError),
    ],
)
def test_failures_propagate_instead_of_absence(service, operation, where, status, error):
    service.manifest({"/dir/file.txt": service.chunk(b"data")})

    def handler(request):
        target = (
            (where == "catalog" and request.url.host == "feeds.dev.azure.com")
            or (where == "metadata" and "/upack/packages/" in request.url.path)
            or (where == "resolve" and request.url.path.endswith("/dedup/urls"))
            or (where == "blob" and request.url.host == "blob.example")
        )
        if target:
            service.requests.append(request)
            if status is None:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(status)
        return service(request)

    with (
        UniversalPackageClient(
            "org", credential="test-pat", transport=httpx.MockTransport(handler), retries=0
        ) as client,
        pytest.raises(error),
    ):
        inspect(client, operation)
    if where == "metadata" and operation != "list_files":
        assert any(r.url.path.endswith(f"/{PACKAGE_ID}/versions") for r in service.requests)


@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
@pytest.mark.parametrize("failure", ["missing-url", "corruption", "missing-dedup"])
def test_manifest_failures_propagate(client, service, operation, failure):
    root = service.manifest({"/dir/file.txt": service.chunk(b"data")})
    if failure == "missing-url":
        service.missing_urls.add(root.id)
    elif failure == "corruption":
        service.blobs[root.id] = b"broken"
    else:
        service.services = []
    error = IntegrityError if failure == "corruption" else ProtocolError
    with pytest.raises(error):
        inspect(client, operation)


@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
@pytest.mark.parametrize("failure", ["node-format", "missing-leaf", "logical-size", "depth"])
def test_chunked_manifest_errors_are_not_absence(client, service, operation, failure):
    payload = service.chunk(b"file content")
    root = service.manifest({"/dir/file.txt": payload}, chunked=True)
    if failure == "node-format":
        data = b"\x01\x00\x00\x00"
        manifest_id = identifier(data, "02")
        service.blobs[manifest_id] = data
        service.metadata["manifestId"] = manifest_id
    elif failure == "missing-leaf":
        leaf = parse_node(service.blobs[root.id])[0]
        del service.blobs[leaf.id]
    elif failure == "logical-size":
        wrong = BlobRef(root.id, root.size + 1)
        service.metadata["manifestId"] = service.node([wrong]).id
    else:
        for _ in range(65):
            root = service.node([root])
        service.metadata["manifestId"] = root.id
    error = NotFoundError if failure == "missing-leaf" else ProtocolError
    with pytest.raises(error):
        inspect(client, operation)
    assert payload.id not in service.resolve_counts


@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
def test_inspection_does_not_assert_payload_availability(client, service, operation):
    leaf = service.chunk(b"data")
    root = service.node([leaf])
    service.manifest({"/dir/file.txt": root})
    del service.blobs[leaf.id]
    del service.blobs[root.id]
    assert inspect(client, operation)
    assert_manifest_only(service, {leaf.id, root.id})


def test_parent_directory_is_not_an_independent_file(client, service):
    service.manifest({"/dir/file.txt": service.chunk(b"data")})
    assert not inspect(client, "file_exists", relative_path="dir")
    assert inspect(client, "list_file_versions", relative_path="dir") == ()


def test_history_automatic_order_prereleases_deleted_and_missing_paths(client, service):
    a, b = service.chunk(b"a"), service.chunk(b"b")
    history(
        service,
        {
            "2.0.0-rc.1": {"/dir/file.txt": a},
            "1.0.0": {"elsewhere": b},
            "1.2.3": {"dir/file.txt": b},
        },
    )
    service.versions.insert(1, {"version": "3.0.0", "isDeleted": True})
    service.versions[-1] = {"version": "9.0.0", "normalizedVersion": "1.2.3"}
    iterator = client.list_file_versions(feed="feed", name="package", relative_path="dir/file.txt")
    assert service.requests == []
    first = next(iterator)
    assert first == FileVersion("2.0.0-rc.1", PackageFile(PurePosixPath("dir/file.txt"), 1, a.id))
    assert len(metadata_requests(service)) == 1
    second = next(iterator)
    assert second.version == "1.2.3"
    assert second.file.content_id == b.id
    assert len(metadata_requests(service)) == 3
    assert list(iterator) == []
    assert_manifest_only(service, {a.id, b.id})


def test_explicit_history_copies_versions_preserves_order_and_duplicates(client, service):
    ref = service.chunk(b"data")
    history(
        service,
        {
            "2.0.0-rc.1": {"dir/file.txt": ref},
            "1.0.0": {"dir/file.txt": ref},
        },
    )
    versions = ["1.0.0", "2.0.0-rc.1", "1.0.0"]
    iterator = client.list_file_versions(
        feed="feed", name="package", relative_path="dir/file.txt", versions=versions
    )
    versions.clear()
    assert service.requests == []
    assert [entry.version for entry in iterator] == ["1.0.0", "2.0.0-rc.1", "1.0.0"]
    assert not any(r.url.host == "feeds.dev.azure.com" for r in service.requests)
    assert_manifest_only(service, {ref.id})


def test_empty_explicit_history_is_zero_work(client, service):
    assert inspect(client, "list_file_versions", versions=[]) == ()
    assert service.requests == []


def test_missing_package_in_automatic_history_is_explicit(client, service):
    service.package_pages = [[]]
    with pytest.raises(PackageNotFoundError):
        inspect(client, "list_file_versions")
    assert not metadata_requests(service)


@pytest.mark.parametrize("explicit", [False, True])
def test_history_stops_at_missing_version_instead_of_skipping(client, service, explicit):
    ref = service.chunk(b"data")
    history(service, {"1.0.0": {"dir/file.txt": ref}, "3.0.0": {"dir/file.txt": ref}})
    versions = ["1.0.0", "2.0.0", "3.0.0"]
    service.versions = versions
    iterator = client.list_file_versions(
        feed="feed",
        name="package",
        relative_path="dir/file.txt",
        versions=versions if explicit else None,
    )
    assert next(iterator).version == "1.0.0"
    with pytest.raises(NotFoundError):
        next(iterator)
    assert len(metadata_requests(service)) == 2


@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
def test_closed_client_is_rejected_at_call(client, service, operation):
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        inspect(client, operation)
    assert service.requests == []


@pytest.mark.parametrize("versions", [None, [], ["1.2.3"]])
def test_history_requires_open_client_when_iteration_starts(client, service, versions):
    iterator = client.list_file_versions(
        feed="feed", name="package", relative_path="x", versions=versions
    )
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        next(iterator)
    assert service.requests == []


@pytest.mark.parametrize("explicit", [False, True])
def test_history_checks_open_client_between_buffered_versions(client, service, explicit):
    ref = service.chunk(b"data")
    history(service, {"1.0.0": {"dir/file.txt": ref}, "2.0.0": {"dir/file.txt": ref}})
    iterator = client.list_file_versions(
        feed="feed",
        name="package",
        relative_path="dir/file.txt",
        versions=["1.0.0", "2.0.0"] if explicit else None,
    )
    assert next(iterator).version == "1.0.0"
    before = len(service.requests)
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        next(iterator)
    assert len(service.requests) == before


def test_history_does_not_accumulate_manifests_or_results(client, service, monkeypatch):
    from az_artifacts import _manifest

    refs = []
    original = _manifest.load

    def load(*args, **kwargs):
        gc.collect()
        assert sum(ref() is not None for ref in refs) <= 1
        files = original(*args, **kwargs)
        refs.extend(weakref.ref(file) for file in files)
        return files

    service.manifest(
        {
            "dir/file.txt": service.chunk(b"data"),
            **{f"other/{i}": service.chunk(str(i).encode()) for i in range(20)},
        }
    )
    monkeypatch.setattr(_manifest, "load", load)
    iterator = client.list_file_versions(
        feed="feed", name="package", relative_path="dir/file.txt", versions=["1.2.3"] * 25
    )
    for _ in range(25):
        record = next(iterator)
        record_ref = weakref.ref(record)
        del record
        gc.collect()
        assert record_ref() is None
        assert sum(ref() is not None for ref in refs) == 1
    assert tuple(iterator) == ()
    gc.collect()
    assert all(ref() is None for ref in refs)


@pytest.mark.parametrize("operation", ["list_files", "file_exists", "list_file_versions"])
def test_project_routes_are_encoded_and_prerelease_is_exact(client, service, operation):
    service.metadata["version"] = "1.2.3-rc.1"
    service.versions = ["1.2.3-rc.1"]
    options = {"version": "1.2.3-rc.1"} if operation != "list_file_versions" else {}
    inspect(client, operation, feed="Shared Feed", scope="project", project="My Project", **options)
    assert (
        b"/org/My%20Project/_packaging/Shared%20Feed/" in metadata_requests(service)[0].url.raw_path
    )
    assert all(
        b"/org/My%20Project/_apis/packaging/Feeds/Shared%20Feed/" in r.url.raw_path
        for r in service.requests
        if r.url.host == "feeds.dev.azure.com"
    )
