"""Generate a complete synthetic schema example; it can NEVER authorize live work."""

import base64
import hashlib
import json
import struct

if __package__:
    from . import acceptance_support as support
else:
    import acceptance_support as support


LIMITS = {
    "requests": 2000, "items": 10000, "seconds": 600,
    "response_bytes": 4 * 1024 * 1024, "total_bytes": 64 * 1024 * 1024,
    "manifest_bytes": 4 * 1024 * 1024, "cases": 200,
}
FEED_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_FEED_ID = "55555555-5555-4555-8555-555555555555"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
PACKAGE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "44444444-4444-4444-8444-444444444444"


def example():
    """Return config, independent expected values, and tiny local controls."""
    blobs = {}

    def chunk(data, *, keep=False):
        identifier = hashlib.sha512(data).digest()[:32].hex().upper() + "01"
        if keep:
            blobs[identifier] = base64.b64encode(data).decode("ascii")
        return identifier, len(data)

    def node(children):
        data = bytearray(struct.pack("<HH", 0, len(children) - 1))
        for identifier, size in children:
            is_node = identifier.endswith("02")
            data.append(int(is_node))
            data.extend(size.to_bytes(7 if is_node else 3, "little"))
            data.extend(bytes.fromhex(identifier[:-2]))
        identifier = hashlib.sha512(data).digest()[:32].hex().upper() + "02"
        blobs[identifier] = base64.b64encode(data).decode("ascii")
        return identifier, sum(size for _, size in children)

    sources = {"empty.bin": b"", "single.bin": b"abc", "multi.bin": b"abcdef",
               "tree.bin": b"abcdefghi", "different.bin": b"xyz"}
    first, second, third = chunk(b"abc"), chunk(b"def"), chunk(b"ghi")
    multi = node([first, second])
    tree = node([multi, third])
    files = {"empty.bin": chunk(b""), "single.bin": first, "multi.bin": multi, "tree.bin": tree}
    manifest = json.dumps({"items": [
        {"path": "/" + path, "blob": {"id": identifier, "size": size}}
        for path, (identifier, size) in files.items()
    ]}, separators=(",", ":")).encode()
    raw = chunk(manifest, keep=True)
    halfway = len(manifest) // 2
    chunked = node([chunk(manifest[:halfway], keep=True), chunk(manifest[halfway:], keep=True)])
    file_nodes = {key: value for key, value in blobs.items() if key in (multi[0], tree[0])}
    manifest_nodes = {
        key: value for key, value in blobs.items() if key not in file_nodes and key != raw[0]
    }
    manifests = [
        {"manifest_id": raw[0], "blobs": {**file_nodes, raw[0]: blobs[raw[0]]}},
        {"manifest_id": chunked[0], "blobs": {**file_nodes, **manifest_nodes}},
    ]
    inventory = [{"path": path, "size": size, "content_id": identifier}
                 for path, (identifier, size) in files.items()]
    versions = ["1.0.0", "1.1.0-preview.1"]
    metadatas = {
        version: {"version": version, "manifest_id": manifests[index]["manifest_id"],
                  "super_root_id": "AB" * 32 + "02", "package_size": len(manifest) + 18,
                  "description": "Synthetic stable" if index == 0 else "Synthetic prerelease"}
        for index, version in enumerate(versions)
    }
    version_values = [{
        "version": version, "id": None, "normalized_version": version, "is_deleted": False,
        "is_latest": index == 1, "publish_date": "2000-01-01T00:00:00+00:00",
        "deleted_date": None, "description": metadatas[version]["description"],
        "package_description": "",
    } for index, version in enumerate(versions)]
    package_values = [{
        "id": identifier, "name": name, "normalized_name": name,
        "protocol_type": "upack", "versions": None,
    } for identifier, name in ((PACKAGE_ID, "fixture-package"), (OTHER_ID, "fixture-package-two"))]
    config = {
        "schema": 1, "kind": "issue3-readonly", "fixture_only": True,
        "organization": "https://dev.azure.com/fixtureorg",
        "credential_env": "AZ_ARTIFACTS_REFERENCE_TOKEN", "credential_kind": "bearer",
        "services": {"feeds": "https://feeds.dev.azure.com/fixtureorg",
                     "packaging": "https://pkgs.dev.azure.com/fixtureorg",
                     "dedup": "https://vsblob.dev.azure.com/fixtureorg"},
        "baseline_file": "baseline.json", "evidence_directory": "readonly-evidence",
        "limits": dict(LIMITS),
        "targets": [
            {"scope": "organization", "feed": {"name": "fixture-feed", "id": FEED_ID},
             "package_ids": {"fixture-package": PACKAGE_ID}},
            {"scope": "project", "feed": {"name": "fixture-project-feed", "id": PROJECT_FEED_ID},
             "project": {"name": "fixture-project", "id": PROJECT_ID},
             "package_ids": {"fixture-package": PACKAGE_ID}},
        ],
        "cases": [],
    }
    baseline = {
        "schema": 1, "fixture_only": True,
        "provenance": {"kind": "approved-fixture", "independent_of_native": True,
                       "complete": True,
                       "reference": "Synthetic data, not a tenant capture or write approval"},
        "manifests": manifests, "cases": [],
    }

    def add(target, addressing, method, args, expected, **extra):
        case = {"target": target, "addressing": addressing, "method": method, "args": args}
        config["cases"].append(case)
        baseline["cases"].append({"request": case, "expected": expected, **extra})

    for target in range(2):
        for addressing in ("name", "id"):
            common = {"name": "fixture-package"}
            exact = {**common, "version": versions[0]}
            project = ({"id": PROJECT_ID, "name": "fixture-project", "visibility": "private"}
                       if target else None)
            feed = {"id": config["targets"][target]["feed"]["id"],
                    "name": config["targets"][target]["feed"]["name"],
                    "project": project, "description": "Synthetic feed", "deleted_date": None}
            add(target, addressing, "list_feeds", {}, [feed])
            add(target, addressing, "list_packages",
                {"name_query": "fixture-package", "page_size": 1}, package_values)
            add(target, addressing, "list_package_versions", common, version_values)
            add(target, addressing, "package_version_exists", exact, True,
                condition="existing-version")
            add(target, addressing, "get_package_metadata", exact, metadatas[versions[0]])
            add(target, addressing, "get_package_versions_metadata", common, {
                "count": 2, "value": [{"version": version, "description": metadatas[version][
                    "description"]} for version in versions],
            })
            add(target, addressing, "list_files", exact, inventory, manifests=[0])
            add(target, addressing, "file_exists",
                {**exact, "relative_path": "single.bin"}, True, manifests=[0],
                condition="existing-path")
            add(target, addressing, "list_file_versions",
                {**common, "relative_path": "single.bin", "versions": versions},
                [{"version": version, "file": inventory[1]} for version in versions],
                manifests=[0, 1])
            add(target, addressing, "compare_file",
                {**exact, "relative_path": "single.bin", "local_path": "sources/single.bin"},
                {"status": "match", "metadata": metadatas[versions[0]], "file": inventory[1]},
                manifests=[0], local_control={
                    "size": 3, "sha256": hashlib.sha256(b"abc").hexdigest(),
                })
    common = {"name": "fixture-package"}
    exact = {**common, "version": versions[0]}
    add(0, "name", "get_package_metadata", {**exact, "intent": "FetchMetadataOnly"},
        metadatas[versions[0]])
    add(0, "name", "list_files", {**common, "version": versions[1]}, inventory, manifests=[1])
    add(0, "name", "package_version_exists", {**common, "version": "9.9.9"}, False,
        condition="missing-version")
    add(0, "name", "file_exists", {**exact, "relative_path": "missing.bin"}, False,
        manifests=[0], condition="missing-path")
    add(0, "name", "file_exists", {**common, "version": "9.9.9", "relative_path": "single.bin"},
        False, condition="missing-version")
    for index, path in enumerate(files):
        add(0, "name", "compare_file", {**exact, "relative_path": path,
                                      "local_path": "sources/" + path},
            {"status": "match", "metadata": metadatas[versions[0]], "file": inventory[index]},
            manifests=[0], local_control={"size": len(sources[path]),
                                         "sha256": hashlib.sha256(sources[path]).hexdigest()})
    control = {"size": 3, "sha256": hashlib.sha256(b"xyz").hexdigest()}
    add(0, "name", "compare_file",
        {**exact, "relative_path": "single.bin", "local_path": "sources/different.bin"},
        {"status": "different", "metadata": metadatas[versions[0]], "file": inventory[1]},
        manifests=[0], local_control=control)
    add(0, "name", "compare_file",
        {**exact, "relative_path": "missing.bin", "local_path": "sources/different.bin"},
        {"status": "path_missing", "metadata": metadatas[versions[0]], "file": None},
        manifests=[0], local_control=control, condition="missing-path")
    add(0, "name", "compare_file",
        {**common, "version": "9.9.9", "relative_path": "single.bin",
         "local_path": "sources/different.bin"},
        {"status": "version_missing", "metadata": None, "file": None},
        local_control=control, condition="missing-version")
    # Deliberately no inaccessible/deleted live fixture. The report must call
    # those gaps incomplete, rather than concealing them behind passing skips.
    return config, baseline, sources


def main(argv=None):
    parser = support.SafeArgumentParser(description=__doc__)
    parser.add_argument("--private-directory", required=True)
    args = parser.parse_args(argv)
    root = support.private_root(args.private_directory)
    config, baseline, sources = example()
    for filename, value in (("config.example.json", config), ("baseline.example.json", baseline)):
        with (root / filename).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
    source = root / "sources"
    source.mkdir()
    for name, data in sources.items():
        with (source / name).open("xb") as stream:
            stream.write(data)
    print(json.dumps({"synthetic_example": True, "live_authority": False}))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        print(json.dumps({"synthetic_example": False, "live_authority": False}))
        code = 1
    raise SystemExit(code)
