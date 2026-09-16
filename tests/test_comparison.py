import io
import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import httpx
import pytest
from conftest import PACKAGE_ID, identifier

from az_artifacts import (
    AuthenticationError,
    FileComparison,
    LocalFileChangedError,
    NotFoundError,
    PackageFile,
    PackageMetadata,
    PermissionDeniedError,
    ProtocolError,
    TransportError,
    UniversalPackageClient,
    UnsafePathError,
    _inspection,
)
from az_artifacts._dedup import MAX_CHUNK_BYTES, BlobReader, parse_node
from az_artifacts.models import BlobRef


@pytest.fixture(autouse=True)
def comparison_intent(service):
    service.metadata_intent = None


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"data")
    return path


def compare(client, source, **kwargs):
    return client.compare_file(
        **{
            "feed": "feed",
            "name": "package",
            "version": "1.2.3",
            "relative_path": "dir/file.txt",
            "local_path": source,
            **kwargs,
        }
    )


def assert_no_payload(service, payload_ids):
    assert set(service.resolve_counts).isdisjoint(payload_ids)
    assert not any(
        request.url.host == "blob.example" and request.url.path.strip("/") in payload_ids
        for request in service.requests
    )
    assert all(
        request.method == "GET" or request.url.path.endswith("/dedup/urls")
        for request in service.requests
    )


@pytest.mark.parametrize("extension", [".exe", ".bat", ".cmd", ".bin"])
@pytest.mark.parametrize("status", ["match", "path_missing", "version_missing"])
def test_executable_filename_stat_hints_do_not_look_like_source_changes(
    client, service, tmp_path, extension, status
):
    source = tmp_path / ("synthetic-control" + extension)
    source.write_bytes(b"data")
    leaf = service.chunk(b"data")
    service.manifest({"dir/file.txt": leaf} if status != "path_missing" else {})
    if status == "version_missing":
        service.versions = []
    assert compare(client, source).status == status
    assert_no_payload(service, {leaf.id})


def test_open_fingerprint_masks_only_windows_execute_hints(monkeypatch):
    values = {"st_dev": 1, "st_ino": 2, "st_mode": stat.S_IFREG | 0o666, "st_size": 4,
              "st_mtime_ns": 5, "st_ctime_ns": 6}
    before = SimpleNamespace(**values)
    hinted = SimpleNamespace(**{**values, "st_mode": values["st_mode"] | 0o111})
    with monkeypatch.context() as patch:
        patch.setattr(_inspection.os, "name", "nt")
        assert _inspection._open_fingerprint(before) == _inspection._open_fingerprint(hinted)
        for field, value in (("st_ino", 3), ("st_mode", stat.S_IFDIR | 0o666),
                             ("st_mode", stat.S_IFREG | 0o444), ("st_size", 7), ("st_mtime_ns", 8)):
            changed = SimpleNamespace(**{**values, field: value})
            assert _inspection._open_fingerprint(before) != _inspection._open_fingerprint(changed)
        with pytest.raises(LocalFileChangedError):
            _inspection._unchanged(before, hinted)
    with monkeypatch.context() as patch:
        patch.setattr(_inspection.os, "name", "posix")
        assert _inspection._open_fingerprint(before) != _inspection._open_fingerprint(hinted)


@pytest.mark.parametrize("kind", ["raw", "compressed", "empty", "empty-node", "node", "nested"])
@pytest.mark.parametrize("chunked_manifest", [False, True])
def test_match_reads_metadata_nodes_not_payloads(
    client, service, source, monkeypatch, kind, chunked_manifest
):
    data = b"" if kind in ("empty", "empty-node") else b"AAAA"
    leaf = service.chunk(data, wire=b"\x00\x00\x00\x40A\x00\x00" if kind == "compressed" else None)
    root = leaf
    nodes = set()
    if kind in ("node", "nested", "empty-node"):
        root = service.node([leaf, leaf])
        nodes.add(root.id)
        data *= 2
    if kind == "nested":
        empty = service.chunk(b"")
        root = service.node([root, empty, root, leaf])
        nodes.add(root.id)
        data = data * 2 + b"AAAA"
    source.write_bytes(data)
    manifest = service.manifest({"/dir/file.txt": root}, chunked=chunked_manifest)
    manifests = {manifest.id}
    if chunked_manifest:
        manifests.update(ref.id for ref in parse_node(service.blobs[manifest.id]))
    payloads = {leaf.id, identifier(b"")}
    original_open = os.open

    def read_only(path, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original_open(path, flags, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Comparison attempted a filesystem write")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", read_only)
        for method in ("write_bytes", "write_text", "mkdir", "unlink"):
            patch.setattr(Path, method, forbidden)
        patch.setattr(os, "replace", forbidden)
        patch.setattr(os, "link", forbidden)
        result = compare(client, source)
    assert result == FileComparison(
        "match",
        PackageMetadata("1.2.3", manifest.id, "AB" * 32 + "02", len(data)),
        PackageFile(PurePosixPath("dir/file.txt"), len(data), root.id),
    )
    with pytest.raises(FrozenInstanceError):
        result.status = "different"
    assert set(service.resolve_counts) == manifests | nodes
    assert len([r for r in service.requests if "/upack/packages/" in r.url.path]) == 1
    fetched = [r.url.path.strip("/") for r in service.requests if r.url.host == "blob.example"]
    for manifest_id in manifests:
        assert fetched.count(manifest_id) == 1
    assert_no_payload(service, payloads)


@pytest.mark.parametrize("data", [b"DATA", b"other size", b""])
@pytest.mark.parametrize("node", [False, True])
def test_different_is_disagreement_not_corruption(client, service, source, data, node):
    leaf = service.chunk(b"data")
    root = service.node([leaf]) if node else leaf
    service.manifest({"dir/file.txt": root})
    source.write_bytes(data)
    result = compare(client, source)
    assert result.status == "different"
    assert result.metadata is not None and result.file is not None
    assert_no_payload(service, {leaf.id})
    if node and len(data) != root.size:
        assert root.id not in service.resolve_counts


@pytest.mark.parametrize("node", [False, True])
def test_missing_payload_is_not_an_availability_audit(client, service, source, node):
    leaf = service.chunk(b"data")
    root = service.node([leaf]) if node else leaf
    service.manifest({"dir/file.txt": root})
    del service.blobs[leaf.id]
    assert compare(client, source).status == "match"
    assert_no_payload(service, {leaf.id})


def test_size_mismatch_does_not_audit_node_health(client, service, source):
    root = service.node([service.chunk(b"different size")])
    service.manifest({"dir/file.txt": root})
    del service.blobs[root.id]
    assert compare(client, source).status == "different"
    assert root.id not in service.resolve_counts


@pytest.mark.parametrize("absence", ["package", "substring", "version", "deleted"])
def test_version_missing_has_no_remote_details(client, service, source, absence):
    if absence == "package":
        service.package_pages = [[]]
    elif absence == "substring":
        service.package_pages = [[{"id": PACKAGE_ID, "name": "package-extra"}]]
    elif absence == "version":
        service.versions = ["2.0.0"]
    else:
        service.versions = [{"version": "1.2.3", "isDeleted": True}]
    service.services = []
    assert compare(client, source) == FileComparison("version_missing", None, None)
    assert not service.resolve_counts
    assert not any("/upack/packages/" in r.url.path for r in service.requests)


@pytest.mark.parametrize("path", ["dir", "DIR/file.txt", "*.txt"])
def test_path_missing_preserves_metadata(client, service, source, path):
    root = service.manifest({"dir/file.txt": service.chunk(b"data")})
    result = compare(client, source, relative_path=path)
    assert result == FileComparison(
        "path_missing", PackageMetadata("1.2.3", root.id, "AB" * 32 + "02", 4), None
    )


def test_comparison_has_no_negative_cache(client, service, source):
    service.package_pages = [[]]
    assert compare(client, source).status == "version_missing"
    service.package_pages = [[{"id": PACKAGE_ID, "name": "package"}]]
    assert compare(client, source).status == "path_missing"
    service.manifest({"dir/file.txt": service.chunk(b"data")})
    assert compare(client, source).status == "match"
    service.versions = []
    assert compare(client, source).status == "version_missing"


@pytest.mark.parametrize("scope", ["organization", "project"])
def test_scope_encoding_prerelease_and_literal_path(client, service, source, scope):
    service.version = "1.2.3-rc.1"
    service.versions = [service.version]
    service.manifest({"dir/*.txt": service.chunk(b"data")})
    options = {"scope": scope}
    if scope == "project":
        options["project"] = "Project / #"
    assert (
        compare(
            client,
            str(source),
            feed="Feed / #",
            version=service.version,
            relative_path=PurePosixPath("dir/*.txt"),
            **options,
        ).status
        == "match"
    )
    metadata = next(r for r in service.requests if "/upack/packages/" in r.url.path)
    assert b"/_packaging/Feed%20%2F%20%23/" in metadata.url.raw_path
    assert metadata.url.path.endswith("/versions/1.2.3-rc.1")
    assert (b"/Project%20%2F%20%23/" in metadata.url.raw_path) == (scope == "project")
    catalog = next(r for r in service.requests if r.url.host == "feeds.dev.azure.com")
    assert b"/Feeds/Feed%20%2F%20%23/packages" in catalog.url.raw_path


@pytest.mark.parametrize(
    "options",
    [
        {"feed": ""},
        {"feed": None},
        {"name": "Invalid"},
        {"name": None},
        {"scope": "wrong"},
        {"scope": "project"},
        {"scope": "project", "project": 1},
        {"scope": "project", "project": ""},
        {"project": "project"},
        *({"version": v} for v in ("*", "1.*", "", "latest", "1.2.3+build", None, 7, True)),
        *(
            {"relative_path": p}
            for p in (
                "",
                "/x",
                "../x",
                "a//b",
                "a/./b",
                "a/",
                "a\\b",
                "C:x",
                "a\0b",
                "a\x7fb",
                Path("x"),
                PureWindowsPath("x"),
                None,
                1,
            )
        ),
        *(
            {"local_path": p}
            for p in (
                "",
                "a\0b",
                Path("a\0b"),
                PurePosixPath("x"),
                PureWindowsPath("x"),
                None,
                1,
                b"x",
            )
        ),
    ],
)
def test_invalid_arguments_before_local_open_and_network(
    client, service, source, monkeypatch, options
):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid argument reached local open")

    monkeypatch.setattr(_inspection, "open_local", forbidden)
    with pytest.raises((TypeError, ValueError, UnsafePathError)):
        compare(client, source, **options)
    assert not service.requests


def test_closed_client_does_not_access_local_file(client, service, monkeypatch, source):
    client.close()

    def forbidden(*args, **kwargs):
        pytest.fail("Closed client reached local open")

    monkeypatch.setattr(_inspection, "open_local", forbidden)
    with pytest.raises(RuntimeError, match="closed"):
        compare(client, source)
    assert not service.requests


@pytest.mark.parametrize(
    "where", ["packages", "versions", "metadata", "resolve", "manifest", "node"]
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
def test_remote_failures_never_become_comparison_results(service, source, where, status, error):
    root = service.node([service.chunk(b"data")])
    manifest = service.manifest({"dir/file.txt": root})

    def handler(request):
        target = (
            (
                where == "packages"
                and request.url.host == "feeds.dev.azure.com"
                and request.url.path.endswith("/packages")
            )
            or (
                where == "versions"
                and request.url.host == "feeds.dev.azure.com"
                and request.url.path.endswith("/versions")
            )
            or (where == "metadata" and "/upack/packages/" in request.url.path)
            or (where == "resolve" and request.url.path.endswith("/dedup/urls"))
            or (where == "manifest" and request.url.path == "/" + manifest.id)
            or (where == "node" and request.url.path == "/" + root.id)
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
        compare(client, source)


@pytest.mark.parametrize(
    "failure",
    [
        "metadata",
        "manifest-json",
        "manifest-id",
        "manifest-path",
        "manifest-hash",
        "manifest-url",
        "node-url",
        "node-hash",
        "node-version",
        "node-child",
        "node-size",
        "nested-size",
        "depth",
        "oversized-leaf",
    ],
)
def test_malformed_metadata_is_not_equality_or_absence(client, service, source, failure):
    leaf = service.chunk(b"data")
    root = service.node([leaf])
    if failure == "node-size":
        root = BlobRef(root.id, root.size + 1)
        source.write_bytes(b"data!")
    elif failure == "nested-size":
        root = service.node([BlobRef(root.id, root.size + 1)])
        source.write_bytes(b"data!")
    elif failure == "depth":
        for _ in range(64):
            root = service.node([root])
    elif failure == "oversized-leaf":
        root = BlobRef(leaf.id, MAX_CHUNK_BYTES + 1)
    elif failure in ("node-version", "node-child"):
        data = b"\x01\x00\x00\x00" if failure == "node-version" else b"\0\0\0\0\x02"
        root = BlobRef(identifier(data, "02"), 4)
        service.blobs[root.id] = data
    manifest = service.manifest({"dir/file.txt": root})
    if failure == "metadata":
        service.metadata["version"] = "9.0.0"
    elif failure == "manifest-json":
        service.metadata["manifestId"] = service.chunk(b"not json").id
    elif failure == "manifest-id":
        service.manifest({"dir/file.txt": BlobRef("AB" * 32 + "03", 4)})
    elif failure == "manifest-path":
        service.manifest({"../bad": leaf})
    elif failure in ("manifest-hash", "node-hash"):
        service.blobs[manifest.id if failure == "manifest-hash" else root.id] = b"corrupt"
    elif failure in ("manifest-url", "node-url"):
        service.missing_urls.add(manifest.id if failure == "manifest-url" else root.id)
    error = UnsafePathError if failure == "manifest-path" else ProtocolError
    with pytest.raises(error):
        compare(client, source)
    assert_no_payload(service, {leaf.id})


def test_signed_node_url_refresh_retains_credential_separation(client, service, source):
    leaf = service.chunk(b"data")
    root = service.node([leaf])
    service.manifest({"dir/file.txt": root})
    service.expire.add(root.id)
    assert compare(client, source).status == "match"
    assert service.resolve_counts[root.id] == 2
    requests = [r for r in service.requests if r.url.path == "/" + root.id]
    assert len(requests) == 2
    assert all("authorization" not in request.headers for request in requests)
    assert_no_payload(service, {leaf.id})


def test_missing_dedup_service_is_not_an_absence(client, service, source):
    service.services = []
    with pytest.raises(ProtocolError, match="dedup"):
        compare(client, source)


@pytest.mark.parametrize("chunked", [False, True])
def test_comparison_respects_manifest_bound(service, source, chunked):
    service.manifest({"dir/file.txt": service.chunk(b"data")}, chunked=chunked)
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
        compare(client, source)


@pytest.mark.parametrize("missing_version", [False, True])
def test_missing_local_file_precedes_remote_absence(client, service, source, missing_version):
    if missing_version:
        service.versions = []
    source.unlink()
    with pytest.raises(FileNotFoundError):
        compare(client, source)
    assert not service.requests


def test_directory_is_rejected_without_open_or_network(client, service, tmp_path):
    with pytest.raises(ValueError, match="regular file"):
        compare(client, tmp_path)
    assert not service.requests


@pytest.mark.parametrize("mode", [stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK])
def test_special_file_is_rejected_before_open(client, service, source, monkeypatch, mode):
    monkeypatch.setattr(Path, "stat", lambda *a, **kw: SimpleNamespace(st_mode=mode))

    def forbidden(*args, **kwargs):
        pytest.fail("Special file reached open")

    monkeypatch.setattr(_inspection, "_nonblocking_open", forbidden)
    with pytest.raises(ValueError, match="regular file"):
        compare(client, source)
    assert not service.requests


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO opening semantics")
def test_fifo_replacement_between_stat_and_open_cannot_block(client, service, source, monkeypatch):
    original_open = _inspection._nonblocking_open

    def replace(path, flags):
        source.unlink()
        os.mkfifo(source)
        return original_open(path, flags)

    monkeypatch.setattr(_inspection, "_nonblocking_open", replace)
    with pytest.raises(LocalFileChangedError):
        compare(client, source)
    assert not service.requests


def test_symlink_policy_follows_regular_target(client, service, source, monkeypatch):
    service.manifest({"dir/file.txt": service.chunk(b"data")})
    alias = source.with_name("link.bin")
    # Exercise following without requiring Windows symlink-creation privileges.
    real_stat, real_open = Path.stat, _inspection._nonblocking_open
    monkeypatch.setattr(
        Path, "stat", lambda path, **kw: real_stat(source if path == alias else path, **kw)
    )
    monkeypatch.setattr(
        _inspection,
        "_nonblocking_open",
        lambda path, flags: real_open(str(source) if Path(path) == alias else path, flags),
    )
    assert compare(client, alias).status == "match"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation may require privileges")
def test_real_symlink_and_broken_symlink(client, service, source):
    service.manifest({"dir/file.txt": service.chunk(b"data")})
    alias = source.with_name("link.bin")
    alias.symlink_to(source)
    assert compare(client, alias).status == "match"
    source.unlink()
    with pytest.raises(FileNotFoundError):
        compare(client, alias)


@pytest.mark.parametrize("operation", ["stat", "open", "read"])
def test_local_io_errors_propagate(client, service, source, monkeypatch, operation):
    service.manifest({"dir/file.txt": service.chunk(b"data")})

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    if operation == "stat":
        monkeypatch.setattr(Path, "stat", denied)
    elif operation == "open":
        monkeypatch.setattr(_inspection, "_nonblocking_open", denied)
    else:
        monkeypatch.setattr(_inspection, "matches", denied)
    with pytest.raises(PermissionError):
        compare(client, source)
    if operation != "read":
        assert not service.requests


@pytest.mark.parametrize("status", ["version_missing", "path_missing", "match", "different"])
@pytest.mark.parametrize("change", ["size", "mtime", "ctime", "replacement", "disappearance"])
def test_every_return_checks_local_changes(client, service, source, monkeypatch, status, change):
    if status == "version_missing":
        service.versions = []
    elif status in ("match", "different"):
        service.manifest({"dir/file.txt": service.chunk(b"data" if status == "match" else b"DATA")})
    real_stat = Path.stat
    calls = 0

    def changed(path, **kwargs):
        nonlocal calls
        result = real_stat(path, **kwargs)
        if path != source:
            return result
        calls += 1
        if calls == 1:
            return result
        if change == "disappearance":
            raise FileNotFoundError("removed")
        fields = {
            key: getattr(result, key)
            for key in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        }
        key = {
            "size": "st_size",
            "mtime": "st_mtime_ns",
            "ctime": "st_ctime_ns",
            "replacement": "st_ino",
        }[change]
        fields[key] += 1
        return SimpleNamespace(**fields)

    monkeypatch.setattr(Path, "stat", changed)
    with pytest.raises(LocalFileChangedError):
        compare(client, source)


def test_actual_source_mutation_is_not_a_different_result(client, service, source, monkeypatch):
    service.manifest({"dir/file.txt": service.chunk(b"data")})
    original = _inspection.matches

    def mutate(stream, size, file, reader):
        source.write_bytes(b"changed size")
        return original(stream, size, file, reader)

    monkeypatch.setattr(_inspection, "matches", mutate)
    with pytest.raises(LocalFileChangedError):
        compare(client, source)


def test_replacement_during_open_is_detected_before_network(client, service, source, monkeypatch):
    other = source.with_name("replacement.bin")
    other.write_bytes(source.read_bytes())
    original = _inspection._nonblocking_open
    monkeypatch.setattr(
        _inspection, "_nonblocking_open", lambda path, flags: original(str(other), flags)
    )
    with pytest.raises(LocalFileChangedError):
        compare(client, source)
    assert not service.requests


@pytest.mark.parametrize("changed", [False, True])
def test_descriptor_has_its_own_ctime_baseline(client, service, source, monkeypatch, changed):
    service.manifest({"dir/file.txt": service.chunk(b"data")})
    real_fstat = os.fstat
    calls = 0

    def descriptor_stat(fd):
        nonlocal calls
        result = real_fstat(fd)
        calls += 1
        fields = {
            key: getattr(result, key)
            for key in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        }
        fields["st_ctime_ns"] += 1000 + (calls if changed else 0)
        return SimpleNamespace(**fields)

    monkeypatch.setattr(os, "fstat", descriptor_stat)
    if changed:
        with pytest.raises(LocalFileChangedError):
            compare(client, source)
    else:
        assert compare(client, source).status == "match"
    assert calls == 2


@pytest.mark.parametrize("data", [b"dat", b"data!"])
def test_observed_short_or_extra_local_data_raises(service, client, data):
    ref = service.chunk(b"data")
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    file = PackageFile(PurePosixPath("x"), 4, ref.id)
    with pytest.raises(LocalFileChangedError):
        _inspection.matches(io.BytesIO(data), 4, file, reader)
    assert not service.requests


def test_streaming_is_bounded_and_accepts_short_reads(service, client):
    data = b"a" * (3 * _inspection._READ_BYTES + 5)
    ref = service.chunk(data)
    file = PackageFile(PurePosixPath("x"), len(data), ref.id)
    sizes = []

    class ShortReads(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= _inspection._READ_BYTES
            sizes.append(size)
            return super().read(min(777, size))

    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    assert _inspection.matches(ShortReads(data), len(data), file, reader)
    assert max(sizes) == _inspection._READ_BYTES
    assert sizes[-1] == 1
    assert not service.requests


def test_local_stream_read_failure_is_not_content_disagreement(service, client):
    ref = service.chunk(b"data")
    file = PackageFile(PurePosixPath("x"), 4, ref.id)
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")

    class FailingRead(io.BytesIO):
        def read(self, size=-1):
            raise OSError("read failed")

    with pytest.raises(OSError, match="read failed"):
        _inspection.matches(FailingRead(b"data"), 4, file, reader)
    assert not service.requests


def test_multi_level_comparison_reads_each_remote_boundary(service, client):
    a, b = service.chunk(b"ab"), service.chunk(b"cde")
    inner = service.node([b, a])
    root = service.node([a, inner, b, inner])
    file = PackageFile(PurePosixPath("x"), root.size, root.id)
    reader = BlobReader(client._http, "https://vsblob.dev.azure.com/org")
    sizes = []

    class ObservedReads(io.BytesIO):
        def read(self, size=-1):
            sizes.append(size)
            return super().read(size)

    assert _inspection.matches(ObservedReads(b"abcdeabcdecdeab"), root.size, file, reader)
    assert sizes == [2, 3, 2, 3, 3, 2, 1]
    assert_no_payload(service, {a.id, b.id})


def test_mismatch_stops_before_unvisited_node(client, service, source):
    leaf = service.chunk(b"ab")
    late = service.node([leaf])
    root = service.node([leaf, late])
    service.manifest({"dir/file.txt": root})
    del service.blobs[late.id]
    assert compare(client, source).status == "different"
    assert late.id not in service.resolve_counts
    assert_no_payload(service, {leaf.id})
