# az-artifacts

Native Python **discovery, metadata, file inspection/comparison, and downloads** for Azure
DevOps Universal Packages, without ArtifactTool or an Azure CLI runtime dependency.

**Status: early, experimental implementation (0.1.0).**
This implements a private transfer protocol using the Rust implementation as
reference. Mock-based tests are not proof of service compatibility: **live Azure
DevOps interoperability has not yet been verified**. Treat this as experimental,
not a production-ready replacement for Microsoft's tooling.

Universal Package publishing/uploading is **not supported**. There is no command-line
entry point, subprocess wrapper, or fallback to ArtifactTool. Azure DevOps Server
(on-premises) is also unsupported; the client targets Azure DevOps Services.

## Installation

Requires Python **3.11 or newer**. The runtime dependencies are `httpx` and `wcmatch`.

Install the package from [PyPI](https://pypi.org/project/az-artifacts/):

```bash
uv add az-artifacts
```

To work from this checkout:

```bash
uv sync --locked --group dev
uv run --locked python
```

To use the checkout from another uv project:

```bash
uv add /path/to/az-artifacts
```

## Download a package

Provide a Personal Access Token (PAT) with **Packaging: Read** and access to the
feed. Inject it through your shell's secret handling or CI secret store; do not
put it in source code, URLs, or command-line arguments.

```python
import os

from az_artifacts import UniversalPackageClient

pat = os.environ["AZURE_DEVOPS_EXT_PAT"]

with UniversalPackageClient("https://dev.azure.com/org", credential=pat) as client:
    result = client.download(
        feed="feed",
        name="package",
        version="1.2.3",
        path="./download",
        scope="project",
        project="project",
        file_filter="**/*.txt",
        overwrite=True,
    )

print(result.metadata.version)
print(result.path)
print(result.files)
print(result.bytes_downloaded)
```

Reading `AZURE_DEVOPS_EXT_PAT` above is **explicit application code**: the library
does not read that variable, Azure CLI credentials/configuration, or Git remotes
automatically.

### Authentication options

- A plain `credential` string is always a PAT, sent using HTTP Basic authentication.
- Wrap an already-acquired OAuth access token in `BearerToken` to send it as a
  bearer token, rather than accidentally treating it as a PAT:

  ```python
  import os

  from az_artifacts import BearerToken, UniversalPackageClient

  with UniversalPackageClient(
      "https://dev.azure.com/org",
      credential=BearerToken(os.environ["SYSTEM_ACCESSTOKEN"]),
  ) as client:
      result = client.download(feed="feed", name="package", version="1.2.3", path="./download")
  ```

  In Azure Pipelines, explicitly map `System.AccessToken` into that environment
  variable and grant the pipeline identity access to the feed.

- Optionally install the `azure-identity` extra and pass a synchronous
  `TokenCredential`, such as `DefaultAzureCredential`:

  ```bash
  uv sync --locked --group dev --extra azure-identity
  ```

  ```python
  from azure.identity import DefaultAzureCredential

  from az_artifacts import UniversalPackageClient

  with DefaultAzureCredential(
      exclude_cli_credential=True,
      exclude_developer_cli_credential=True,
      exclude_powershell_credential=True,
  ) as credential:
      with UniversalPackageClient("https://dev.azure.com/org", credential=credential) as client:
          result = client.download(feed="feed", name="package", version="1.2.3", path="./download")
  ```

  Configure an appropriate environment, workload, or managed identity with Azure
  DevOps/feed access. The example excludes CLI-based credential providers so it
  does not depend on installed command-line tools. The library requests the Azure
  DevOps scope `499b84ac-1321-427f-aa17-267ca6975798/.default`. The caller owns the
  credential's lifetime; closing the client does not close a supplied credential.

### Client and download options

Use the client as a context manager, or call `close()` when finished.
The public `discover_services()` method returns the discovered service
name-to-URL mapping and caches the resource-area lookup for the client's
lifetime. Downloads, catalog, and metadata methods share this discovery.

| Client argument | Default / meaning |
| --- | --- |
| `organization` | Required organization name, `https://dev.azure.com/org`, or legacy `https://org.visualstudio.com` URL. No automatic organization detection. |
| `credential` | Required explicit PAT, `BearerToken`, or synchronous `TokenCredential`. |
| `timeout` | `60.0` seconds; positive, finite HTTP timeout. |
| `retries` | `3`; bounded retries for transient request failures. Use `0` to disable retries. |
| `max_workers` | `4`; bounds concurrent file download workers. |
| `max_manifest_bytes` | `64 * 1024 * 1024`; limits the decoded manifest size. |
| `transport` | `None`; optional `httpx.BaseTransport`, such as `httpx.MockTransport` for tests. |

All arguments to `download()` are keyword-only:

| Argument | Default / meaning |
| --- | --- |
| `feed` | Required feed name or ID. |
| `name` | Required Universal Package name. |
| `version` | Required exact version or supported wildcard selector. |
| `path` | Required destination as a string or `pathlib.Path`. |
| `scope` | `"organization"` by default; also accepts `"project"`. |
| `project` | Required project name or ID for `scope="project"`; omit for organization scope. |
| `file_filter` | `None` downloads every file; otherwise a glob string or sequence of patterns. |
| `overwrite` | `True` replaces existing files only after each replacement is complete; `False` raises `FileExistsError` on an existing destination file. |

Project-scoped and organization-scoped feeds are distinct. The project cannot be
inferred from the feed name; pass both `scope="project"` and `project=...` when
needed. Passing `project` with the default `scope="organization"` raises
`ValueError`.

### Version selection and file filters

Exact versions, including prereleases such as `1.2.3-rc.1`, are accepted.
Wildcard selectors `*`, `1.*`, and `1.2.*` resolve to the latest **stable** matching
numeric SemVer, not lexical order (`1.10.0` is newer than `1.9.0`). Wildcards do not
select prereleases; use an exact prerelease version instead.

Filters match package-relative **POSIX paths**, using `/` even on Windows.
Supported forms include `*`, `?`, character classes (`[ab]`), recursive `**`,
extended globs (`@(src|docs)/**/*.txt`), and brace expansion.

```python
filters = ["**/*", "!**/*.tmp", "!private/**"]
```

A sequence is evaluated in order: matching include patterns select a file and
matching `!`-prefixed exclude patterns remove it. A later include can select it
again. Start with an include such as `"**/*"` when excluding from the entire
package; an exclusion by itself does not imply "include everything." The extended
glob form `!(...)` is not interpreted as an exclusion prefix. Matching is
case-sensitive and includes dotfiles. A supplied filter matching no files raises
`NoMatchingFilesError` for downloads; `list_files()` instead returns an empty
tuple. Full Azure CLI/ArtifactTool glob parity is not claimed.

### Results and failure behavior

`download()` returns a `DownloadResult`:

| Field | Meaning |
| --- | --- |
| `metadata.version` | Resolved exact package version. |
| `metadata.manifest_id` | Package manifest identifier. |
| `metadata.super_root_id` | Package super-root identifier. |
| `metadata.package_size` | Advertised whole-package size, before filtering. |
| `metadata.description` | Optional package description; missing/null is `None`, and an empty string is preserved. |
| `path` | Resolved destination `pathlib.Path`. |
| `files` | Tuple of relative `pathlib.Path` objects for downloaded files; combine with `result.path` to locate them. |
| `bytes_downloaded` | Logical file bytes written for the selected files, **not** network bytes, compressed transfer size, or bytes spent on metadata/retries. |

Downloads validate content hashes and sizes, bound manifest/decompression work,
use bounded workers and retries, and reject unsafe manifest/destination paths.
The decoder distinguishes raw chunks from the supported LZ77-compressed form
using the expected SHA-512 content hash truncated to 256 bits, and enforces both
hash and size checks. Typed recursive deduplication nodes and chunked manifests
are supported. The node wire format limits each content chunk to **16 MiB minus
1 byte**; tree depth is limited to **64**. Decoded manifests default to a
**64 MiB** limit, configurable through `max_manifest_bytes`.

Authenticated service requests and signed blob downloads are separate: **Azure
DevOps credentials are not sent with signed blob URL downloads**.

Files are finalized atomically **one file at a time**, after validation.
The default `overwrite=True` uses `os.replace`; `overwrite=False` uses an
exclusive hard link and therefore **requires filesystem hard-link support**.
Downloads restore file contents and relative paths, not original permissions,
executable bits, or symlinks.

If a file download fails, its existing destination is preserved; other files
that already completed remain in place. This is **not** an all-or-nothing package
transaction, and directories created during an unsuccessful download may remain.
Do not modify the output directory concurrently, including from another download
client or process.

Library errors derive from `ArtifactsError`, including `AuthenticationError`,
`PermissionDeniedError`, `NotFoundError`, `PackageNotFoundError`, `VersionNotFoundError`,
`NoMatchingFilesError`, `TransportError`, `ProtocolError`, `IntegrityError`,
`LocalFileChangedError`, and `UnsafePathError`. Invalid arguments can raise
`ValueError`/`TypeError`, and local filesystem failures can raise ordinary `OSError`
subclasses.

## Discover feeds, packages, and versions

The hierarchy is **organization → feed → package → version → files**.
Project-scoped feeds additionally belong to a project. A file is part of a package
version, not independently versioned. Manifest-only inspection, path history, and
content comparison are available below. Registration APIs are **not implemented yet**.

```python
import os

from az_artifacts import UniversalPackageClient

with UniversalPackageClient(
    "https://dev.azure.com/org",
    credential=os.environ["AZURE_DEVOPS_EXT_PAT"],
) as client:
    for feed in client.list_feeds():
        options = (
            {"scope": "project", "project": feed.project.id}
            if feed.project is not None
            else {"scope": "organization"}
        )
        print(feed.name, feed.description)
        for package in client.list_packages(feed=feed.id, name_query="tools", **options):
            print(package.name, package.normalized_name)

    versions = client.list_package_versions(feed="feed", name="tools", include_deleted=True)
    for version in versions:
        print(version.version, version.publish_date, version.is_deleted)

    exists = client.package_version_exists(feed="feed", name="tools", version="1.2.3-rc.1")
    print(exists)
```

All arguments are keyword-only. Catalog methods require an open client but no
Dedup service; they never fetch manifests/payloads or access local files.

| Method | Result and arguments |
| --- | --- |
| `list_feeds(project=None)` | Tuple of all accessible feeds in the organization, optionally filtered by project name/ID. Omission does **not** restrict results to organization-scoped feeds. `Feed.project` preserves the returned association rather than inferring scope from the query. |
| `list_packages(feed=..., name_query=None, page_size=100, scope="organization", project=None)` | Lazy iterator over visible Universal Packages. `feed` is a name/ID; `name_query` is an optional nonempty **substring** query, not an exact identity. `page_size` must be a positive int32, not a boolean. Arguments are checked at call time; requests begin on iteration. |
| `list_package_versions(feed=..., name=..., include_deleted=False, scope="organization", project=None)` | Tuple of exact-name package versions, including prereleases, in service order. `include_deleted=True` includes both states by omitting `isDeleted`; default requests live versions. No version sorting or stable-only filtering is applied. An established missing package raises `PackageNotFoundError`. |
| `package_version_exists(feed=..., name=..., version=..., scope="organization", project=None)` | Boolean about a visible, nondeleted **exact** version, including prereleases. Wildcards are not accepted. False requires successful catalog reads establishing absence; failures, including ambiguous HTTP 404s, propagate. |

Feed-specific methods follow `download()` scope rules: `scope="project"` requires
a project name/ID, while organization scope requires omitting `project`.
Advancing an unexhausted package iterator after closing its client raises
`RuntimeError`, including when entries remain buffered.

### Catalog models

These are frozen summaries, not full SDK model parity. GUIDs are validated and
canonicalized; optional missing/null data remains `None`, not a fabricated value.
Descriptions can be empty, and dates are timezone-aware UTC datetimes.

| Model | Fields |
| --- | --- |
| `ProjectReference` | `id`: project GUID; optional `name` and `visibility`: service project values. |
| `Feed` | `id`: feed GUID; `name`: display name; optional `project`: associated `ProjectReference`; `description`: feed description; `deleted_date`: deletion timestamp. |
| `Package` | `id`: package GUID; `name`: display name; optional `normalized_name`: package identity; `protocol_type`: service protocol; `versions`: tuple of only the summaries supplied in the listing. `versions=None` means omitted, **not** no versions; use `list_package_versions()` to enumerate. |
| `PackageVersion` | `version`: display version; optional `normalized_version`: version identity; `id`: version GUID; `is_deleted` / `is_latest`: service flags; `publish_date` / `deleted_date`: timestamps; `description` / `package_description`: distinct SDK version/package descriptions. |

Exact lookup compares normalized identities when supplied, falling back to valid
display names/versions only when normalization is absent. Malformed supplied
fields raise `ProtocolError` instead of being skipped.

### Completeness and absence limits

Package listing traverses `$top`/`$skip` pages until a short/empty final page,
without accumulating the catalog. Exact-name lookup scans all candidate pages.
Two page-ID signatures detect immediately repeated and cyclic responses using
bounded memory; longer cycles can yield repeated entries before detection.
The int32 offset bound is explicit: exceeding it raises `ProtocolError`, never
silent truncation. Feed and version listing have no documented paging arguments
and use one bounded response. Unsupported continuation, partial responses,
oversized package pages, and inconsistent page counts fail explicitly.

This is **not a transactional snapshot**. Concurrent catalog changes can cause
duplicates, omissions, or inconsistencies; callers needing a stable traversal
must coordinate catalog changes themselves. Only service locations are cached,
not catalog results or negative existence checks.

**Absence does not guarantee publishability.** Deleted versions remain reserved,
visibility depends on permissions, and another publisher can race with a check.
An inaccessible/missing feed, authentication/permission failure, transport failure,
or malformed response is an error, not `False`. Registration remains authoritative;
there is no `can_publish()` API.

Catalog requests use Feed API `7.1` on `feeds.dev.azure.com`, resolved through the
shared Feed resource area (by ID/name) or its known organization fallback. Transfer
metadata uses the separate `pkgs.dev.azure.com` service. No NuGet-only `isListed`
or `isRelease` filters are sent for Universal Packages. These paths follow the
[Feed REST API](https://learn.microsoft.com/en-us/rest/api/azure/devops/artifacts/feed-management/get-feeds?view=azure-devops-rest-7.1)
and pinned SDK; **live catalog compatibility remains unverified**.

## Read package metadata

The metadata methods use the same explicit credentials and cached service
discovery as downloads, but do not retrieve manifests/blobs, write files, or
require an advertised Dedup service. All arguments are keyword-only. `feed`,
`name`, `scope`, and `project` follow the same rules as `download()`.

```python
import os

from az_artifacts import UniversalPackageClient

with UniversalPackageClient(
    "https://dev.azure.com/org",
    credential=os.environ["AZURE_DEVOPS_EXT_PAT"],
) as client:
    metadata = client.get_package_metadata(feed="feed", name="package", version="1.2.3-rc.1")
    versions = client.get_package_versions_metadata(feed="feed", name="package")

print(metadata.version, metadata.description, metadata.package_size)
print(versions.count)  # Server count, not a computed length or pagination total.
for entry in versions.value:
    print(entry.version, entry.description)
```

`get_package_metadata()` returns the same frozen `PackageMetadata` used in
`DownloadResult.metadata`: version, manifest ID, super-root ID, whole-package size
in bytes, and optional description. It requires an **exact version**, including
prereleases; wildcards are only supported by `download()`. Its optional `intent`
argument defaults to `None` (no query key) and otherwise sends the supplied
nonempty string unchanged. Downloads continue to send `intent="Download"`.
A response identifying a different version raises `ProtocolError`.

`get_package_versions_metadata()` returns a frozen
`LimitedPackageMetadataListResponse` with the unmodified server `count` and an
immutable `value` tuple of `LimitedPackageMetadata(version, description)` entries.
Service order and prereleases are preserved. This is limited UPack metadata, not
the richer generic package-version catalog. Count is not assumed to equal tuple
length or prove completeness; explicit unsupported continuation or partial-response
signals raise `ProtocolError`. Both methods propagate service/protocol failures,
including 404s, rather than treating them as an empty result.

Descriptions preserve empty strings; missing/null descriptions are `None`.
The exported frozen `PackagePushMetadata(manifest_id, super_root_id, proof_nodes,
description=None)` and `PackageVersionDeletionState(name, version,
deleted_date=None)` are **models only**, not publishing/deletion APIs.
Proof nodes are an immutable tuple of opaque strings; deletion dates are optional
timezone-aware UTC datetimes. There is no `add_package()` or deletion method.

### Experimental metadata routing

The pinned Python SDK uses location `4cdb2ced-0758-4651-8032-010f070dd7e5` and API
`7.1-preview.1` for [both metadata GETs](https://github.com/microsoft/azure-devops-python-api/blob/86c9a559fc4ab309df21e674b236a542f9e77f89/azure-devops/azure/devops/v7_1/upack_packaging/upack_packaging_client.py#L53-L101).
Its [route substitution](https://github.com/microsoft/azure-devops-python-api/blob/86c9a559fc4ab309df21e674b236a542f9e77f89/azure-devops/azure/devops/client.py#L117-L157)
removes omitted placeholder segments, but retains literal segments. Based on
that shared location and this library's existing exact-version route, the new
versionless GET uses
`/{project?}/_packaging/{feed}/upack/packages/{name}/versions`, without a final
version segment. This is an **inferred route**, not a live-discovered/verified
route template. No alternative endpoint is tried on failure. Live Azure DevOps
compatibility, including collection completeness, remains unverified.

## Compare a local file before uploading

Use `compare_file()` to check whether a local file's content is already represented
at a path in an **exact** package version, without downloading that file's payload.
This is a read-only pre-upload check, **not** publishing, registration, or permission
to reuse a version. A path has no independent version, and one matching file does
not imply that an entire candidate package matches.

```python
import os
from pathlib import Path

from az_artifacts import UniversalPackageClient

with UniversalPackageClient(
    "https://dev.azure.com/org",
    credential=os.environ["AZURE_DEVOPS_EXT_PAT"],
) as client:
    comparison = client.compare_file(
        feed="feed",
        name="package",
        version="1.2.3-rc.1",
        relative_path="config/settings.json",
        local_path=Path("build/config/settings.json"),
    )

if comparison.status == "match":
    print("This file's content is already represented in that version.")
elif comparison.status == "different":
    print("Local content differs from the manifest; choose an appropriate new version.")
else:
    print(comparison.status)  # version_missing or path_missing, not permission to publish.
```

All arguments are keyword-only:
`compare_file(*, feed, name, version, relative_path, local_path, scope="organization", project=None)`.
The feed/name/scope rules match `file_exists()`. Exact prereleases are supported;
wildcards are not. `relative_path` uses the same literal, case-sensitive, portable
string/`PurePosixPath` rules described below, not host paths or glob matching.
`local_path` instead requires a nonempty string or concrete `pathlib.Path`.
Requires an open client.

The exported frozen `FileComparison` is **not a boolean**:

| Field | Meaning |
| --- | --- |
| `status: Literal["version_missing", "path_missing", "match", "different"]` | Established catalog package/version absence, missing exact manifest path, represented content agreement, or content disagreement, respectively. |
| `metadata: PackageMetadata \| None` | Exact-version metadata when obtained; `None` only for `version_missing`. |
| `file: PackageFile \| None` | Exact manifest entry for `match` or `different`; `None` for either missing status. |

Arguments are validated first, then the local source is opened **read-only before
any remote request**, even if the version or path is missing. A missing/unreadable
local path therefore raises rather than returning a successful-shaped missing
result. Only regular files are accepted (`ValueError` for directories/special
files). Symlinks, including parent links, are followed to their regular-file target.
Special files are rejected before open; POSIX opens additionally use nonblocking
mode to avoid blocking if the path is replaced by a FIFO during opening.
No files are created or written. Local `OSError` subclasses and all remote errors
propagate; there is no unknown/equality/absence fallback or negative cache.

### Comparison algorithm and limits

Network work is metadata-only: visible-live catalog lookup, exact metadata
(`intent` omitted), one bounded validated manifest read, and only the file's
traversed dedup **node** blobs. Manifest chunks can themselves be `01` blobs and
are read; **file payload leaf URLs are never resolved or downloaded**. There is
no payload-download fallback or publishing chunker.

- A legitimate local/manifest logical-size difference returns `different` early.
  Equal names or sizes alone never establish a match.
- For a `01` chunk root, the local bytes are hashed incrementally with ordinary
  SHA-512, taking its **first 32 digest bytes**, not the distinct SHA-512/256 algorithm.
- A `02` root identifies a serialized node, not a flat file digest. Its hash-checked
  tree supplies ordered remote leaf sizes/IDs. Local bytes are read at those
  boundaries and hashed per leaf, consuming repeated references repeatedly.
  Node formats, aggregate child sizes, leaf size limits, depth/cycles, and exact
  EOF are checked. Empty files are supported.
- Local read buffers are at most 64 KiB; traversal retains bounded node data per
  depth, never all leaf references or the whole local file. Existing manifest,
  node-wire, 16 MiB-minus-1 chunk and depth-64 limits apply. Signed node URL
  refresh and separation of Azure credentials from signed blob requests remain intact.

Do **not mutate, replace, or retarget the source concurrently**. Descriptor/path
identity, type, size, and modification/change timestamps are checked before every
returned status. Observed changes, disappearance after opening, and unexpected
short/extra reads raise `LocalFileChangedError`. These checks are **not an atomic
filesystem snapshot**: changes hidden by filesystem timestamp granularity or
restored metadata can escape detection, as can changes after the final check.

A mismatch may stop before further nodes are visited: `different` means
disagreement with the manifest, **not a complete remote health audit**.
Traversed malformed/missing/corrupt metadata raises, never `match` or absence.
`match` verifies represented content, not current payload availability or
historical upload provenance. Catalog absence does not ensure publishability:
deleted versions stay reserved and concurrent publishers can race.
The algorithm follows the existing decoder and mock fixtures; **live Azure
single-chunk and multi-level package interoperability remains unverified**.
Full uploading/publishing and `add_package()` registration are still unavailable.

## Inspect package files and path history

These read-only methods retrieve exact metadata and the bounded, hash-validated
manifest (raw or chunked), **not file payloads or their dedup nodes/URLs**. They
take no destination and perform **no local filesystem reads or writes**. A
manifest read requires an advertised Dedup service. Metadata requests omit
`intent`, unlike `download()` which sends `"Download"`.

```python
import os
from pathlib import PurePosixPath

from az_artifacts import UniversalPackageClient

with UniversalPackageClient(
    "https://dev.azure.com/org",
    credential=os.environ["AZURE_DEVOPS_EXT_PAT"],
) as client:
    files = client.list_files(
        feed="feed",
        name="package",
        version="1.2.3",
        file_filter=["**/*.{json,txt}", "!private/**"],
    )
    for file in files:
        print(file.path.as_posix(), file.size, file.content_id)

    exists = client.file_exists(
        feed="feed",
        name="package",
        version="1.2.3",
        relative_path=PurePosixPath("config/settings.json"),
    )

    # Iteration must finish before the client closes.
    for entry in client.list_file_versions(
        feed="feed",
        name="package",
        relative_path="config/settings.json",
        versions=["1.2.3", "2.0.0-rc.1"],
    ):
        print(entry.version, entry.file.size, entry.file.content_id)
```

All arguments are keyword-only. `feed`, `name`, `scope="organization"`, and
`project=None` have the same meaning as for downloads. Project scope requires
`project`; organization scope requires its omission.

| Method | Result and arguments |
| --- | --- |
| `list_files(*, feed, name, version, file_filter=None, scope="organization", project=None)` | `tuple[PackageFile, ...]` in manifest order. Requires an exact version, including prereleases. Uses the same ordered, case-sensitive glob engine as downloads; no matches returns `()`, not `NoMatchingFilesError`. |
| `file_exists(*, feed, name, version, relative_path, scope="organization", project=None)` | `bool` for an exact version and exact logical file path; the path is **not a glob**. First uses the catalog to establish visible, nondeleted version presence. |
| `list_file_versions(*, feed, name, relative_path, versions=None, scope="organization", project=None)` | Lazy `Iterator[FileVersion]` for this path in **this package only**. `None` enumerates visible, nondeleted catalog versions, including prereleases, in service order. An explicit sequence of exact version strings retains caller order and duplicates, bypasses catalog enumeration, and bounds the scan. An empty sequence is a zero-request empty iterator. |

The exported models are frozen dataclasses:

| Model field | Meaning |
| --- | --- |
| `PackageFile.path: PurePosixPath` | Case-sensitive, package-relative POSIX path, never a local destination. |
| `PackageFile.size: int` | Advertised logical file size in bytes. |
| `PackageFile.content_id: str` | Uppercase typed dedup ID, including its `01` chunk or `02` node suffix. A node ID hashes a serialized dedup node, **not the whole file**; do not treat this field as a flat file digest. |
| `FileVersion.version: str` | Exact package version containing the requested path. |
| `FileVersion.file: PackageFile` | That version's manifest entry for the path. |

### Logical paths versus local destinations

`relative_path` accepts a nonempty **string or `PurePosixPath`**, not `Path`,
`PureWindowsPath`, or another local filesystem object. Use `/` separators and
omit any leading slash or drive prefix (`C:...`). Strings must not contain
backslashes, control characters, empty components (including a trailing slash),
`.` or `..` components. A `PurePosixPath` is validated using its existing
normalized value: Python has already collapsed repeated separators and `.`
components before the method receives it. No current-directory inference, path
resolution, case folding, or Unicode normalization is performed.

Manifest entries still accept the protocol's optional **single leading slash**;
returned paths never include it. Duplicate logical entries, file/parent overlap,
traversal, and invalid logical paths fail even if a filter would exclude them.
All manifest entries are files; parents are implicit, and empty directories are
not invented or preserved.

Inspection is portable and case-sensitive on Windows too: `A.txt` and `a.txt`
are distinct, and otherwise valid names such as `CON`, `name?`, or
`dir/name:stream` can be inspected. This does **not** promise that they can be
downloaded to the current host. Downloads retain host-reserved-name, case and
Unicode collision checks for **all entries before filtering**, along with their
existing colon, symlink, atomic-write, and no-overwrite protections.

### Absence, errors, and history limits

`file_exists()` returns `False` only after successful catalog reads establish
package/version absence, or a valid manifest lacks the exact path. It does not
cache negative results. An inaccessible/wrong feed, ambiguous HTTP 404,
authentication/permission failure, transport failure, or malformed metadata,
manifest, or manifest blob **raises**, never becomes `False`. A metadata 404
after the catalog reported a version also raises. A positive result describes
the manifest, **not current file-payload availability** or historical upload
provenance. Absence still does not imply publishability; deleted versions remain
reserved and concurrent publishers can race.

`list_files()` and explicit history use exact metadata reads: a nonexistent
version is an error, not an empty result. Automatic history raises
`PackageNotFoundError` for catalog-established missing packages; a present
package with no visible versions yields nothing. Errors or disappearing versions
mid-scan propagate, even after earlier records have been yielded.

History validates caller arguments immediately but starts requests only when
iterated. A bare version string or generator is not a version sequence; use a
list or tuple. Listing filters accept a nonempty string or nonempty sequence of
nonempty strings; malformed shapes, bare `!`, and excessive glob expansion fail
before requests. An unexhausted history iterator requires an open client even
when the version catalog is buffered.

History retains the explicit version sequence or bounded catalog response, and
one manifest at a time, governed by `max_manifest_bytes`; it does not accumulate
all manifests or results internally. Scanning may require metadata and manifest
requests for **every selected version**, including those without the path. It
never searches every feed/package, infers renames, detects content changes, or
provides a snapshot across concurrent catalog changes. Files are not independently
versioned: history associates the same relative path with package versions.
Use explicit versions to bound the work. **Live inspection interoperability is
unverified**, including service-produced raw/chunked manifests and inaccessible
or deleted resources; fixture tests are not compatibility evidence.

## Azure CLI comparison and unsupported behavior

This is a Python API, **not** a full reimplementation of
[`az artifacts universal download`](https://learn.microsoft.com/en-us/cli/azure/artifacts/universal#az-artifacts-universal-download).
The Azure CLI command can detect organization/project context from Git
configuration and use defaults from `az devops configure -d organization=... project=...`.
This library deliberately does neither. Pass organization, credentials, scope,
and project explicitly.

The complete download-command flag mapping is:

| Azure CLI flag | Python equivalent / limitation |
| --- | --- |
| `--feed` | `download(feed=...)` |
| `--name`, `-n` | `download(name=...)` |
| `--path` | `download(path=...)` |
| `--version`, `-v` | `download(version=...)` |
| `--org`, `--organization` | First `UniversalPackageClient(...)` argument |
| `--project`, `-p` | `download(project=...)`, with project scope |
| `--scope {organization,project}` | `download(scope=...)`, default `"organization"` |
| `--file-filter` | `download(file_filter=...)` |
| `--detect {false,true}` | Unsupported; no CLI/Git autodetection |
| `--acquire-policy-token`, `--change-reference` | Azure CLI global policy integration; unsupported |
| `--debug`, `--verbose`, `--only-show-errors` | No CLI logging/output flags |
| `--output`, `-o`, `--query` | No CLI formatting or JMESPath layer; inspect the typed result in Python |
| `--help`, `-h` | No command-line entry point; use this documentation and Python docstrings |
| `--subscription` | No Azure subscription selection; configure your identity and organization explicitly |

There is no `publish()` method, upload CLI, `--description` publishing option,
Azure CLI login/configuration integration, or Azure DevOps Server support.

## Development and verification

```bash
uv sync --locked --group dev
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv build
```

Normal tests use local fixtures and mocked HTTP, without live credentials.
[CI](.github/workflows/ci.yml) targets Python 3.11-3.14 on Linux and Python 3.14
on Windows/macOS. Lint, formatting, type checking, and distribution building run
once, on Linux/Python 3.14. CI builds a wheel and source distribution, installs
each into a separate clean environment, and checks imports without relying on
the source checkout. Actions are pinned to reviewed commits; `ghr` v0.8.0 installs
uv 0.12.15 with release verification enabled.

### Optional live download smoke check

Live service verification is **opt-in**, against your **own existing package**
using your own authorized read credentials. It is not run by normal tests or CI.
No package/feed is created and nothing is published.

In a trusted shell, provide `AZURE_DEVOPS_EXT_PAT`, `AZ_ARTIFACTS_ORGANIZATION`,
`AZ_ARTIFACTS_FEED`, `AZ_ARTIFACTS_PACKAGE`, and an exact
`AZ_ARTIFACTS_VERSION`. Optionally provide `AZ_ARTIFACTS_PROJECT` for a
project-scoped feed. Use a new, empty `./live-download` destination:

```bash
AZ_ARTIFACTS_LIVE_SMOKE=1 uv run --locked python - <<'PY'
import os

from az_artifacts import UniversalPackageClient

if os.environ.get("AZ_ARTIFACTS_LIVE_SMOKE") != "1":
    raise SystemExit("Explicit live-download opt-in is required")

project = os.environ.get("AZ_ARTIFACTS_PROJECT") or None
with UniversalPackageClient(
    os.environ["AZ_ARTIFACTS_ORGANIZATION"],
    credential=os.environ["AZURE_DEVOPS_EXT_PAT"],
) as client:
    result = client.download(
        feed=os.environ["AZ_ARTIFACTS_FEED"],
        name=os.environ["AZ_ARTIFACTS_PACKAGE"],
        version=os.environ["AZ_ARTIFACTS_VERSION"],
        path="./live-download",
        scope="project" if project else "organization",
        project=project,
        overwrite=False,
    )
print(result.metadata.version, len(result.files), result.bytes_downloaded)
PY
```

Compare the downloaded files with your known package contents. A successful
mock test or build does not substitute for this interoperability check.
For read-only inspection verification, use the same authorized package and scope
with `list_files()`, compare paths/sizes to the known contents, check an existing
and a missing path with `file_exists()`, and bound `list_file_versions()` to known
exact versions (including a prerelease when available). Repeat against authorized
organization- and project-scoped fixtures, with raw and chunked manifests.
Those inspection calls create no local files and do not prove payload availability.

## Releasing the Python distribution to PyPI

[The release workflow](.github/workflows/pypi.yml) runs only on pushed `v*` tags.
It checks out the exact triggering commit, reads the static version from
`pyproject.toml` with `tomllib`, and requires an exact match such as
`v0.1.0` -> `0.1.0`. It runs tests, lint, formatting, and type checking before
building and smoke-installing the wheel and source distribution. A separate
publish job downloads those build artifacts and uses OIDC Trusted Publishing
inside the `pypi` environment. There are no PyPI password/API-token secrets.

**Maintainer setup is required before the first release:**

1. Verify the `az-artifacts` PyPI name is available and configure a PyPI project
   or pending Trusted Publisher.
2. Configure the GitHub Trusted Publisher on PyPI with owner `cataggar`,
   repository `az-artifacts`, workflow filename **`pypi.yml`**, and environment
   **`pypi`**. Adjust owner/repository if maintaining a fork.
3. Create the GitHub **`pypi`** environment and restrict deployment to release
   tags (`v*`). This repository publishes automatically for matching tags,
   without required reviewers. Add reviewer approval if your release policy
   requires it. Merely naming an environment in YAML does not configure these
   protection rules. Protect release-tag creation through repository rules
   as appropriate.
4. Commit the intended version, lockfile, and tested changes, then create/push
   the matching `v<version>` tag when ready to release. If required reviewers
   are configured, approve the publish job only after reviewing its source
   and build results. Build artifacts are retained for 14 days, so any required
   approvals must occur before they expire.

PyPI publishing here distributes the **Python library**; it does not add Universal Package
publishing support.

## License and provenance

MIT licensed. The native download implementation is based on the Microsoft
MIT-licensed Rust code in
`../azure-devops-rust-api/azure_devops_rust_api/src/artifacts_download`, including
its Universal Package metadata, deduplication, and decompression protocol work.
The typed node format and content hashes also follow Microsoft's
[BuildXL hashing implementation](https://github.com/microsoft/BuildXL/tree/main/Public/Src/Cache/ContentStore/Hashing).
The exact upstream Microsoft license and copyright notice are retained in the
root [LICENSE](LICENSE); preserve that notice when redistributing derived code.
This Python implementation is not an official Microsoft SDK.
