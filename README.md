# az-artifacts

Native Python **discovery, metadata, file inspection/comparison, downloads,
registration, and publishing** for Azure DevOps Universal Packages, without
ArtifactTool or an Azure CLI runtime dependency.

The published **PyPI 0.1.0 is download-only**. The source version still reads
`0.1.0`, but discovery, metadata, inspection/comparison, registration, and publishing
described here require installation from a current checkout until a new release.

**Status: experimental; not production-ready.** Publishing
is implemented in this checkout, not yet released. A native publish of a new
100 MiB synthetic payload plus empty/zero-filled controls succeeded in a development feed,
and both native and Microsoft-tool downloads matched every approved hash. Two explicitly
approved Microsoft-tool references establish the protocol, including a **100 MiB
file with 1,364 chunks and deeper dedup trees**. Native preparation matches their
manifests, complete content trees, super-roots, and proofs byte-for-byte. Both
reference packages were also downloaded and byte-verified with this library and
Microsoft tooling. Native publication of Bicep 0.43.8, ShellCheck 0.10.0, and
shfmt 3.11.0 packages also completed, with both downloaders verifying the complete
file inventories and hashes. This is development-feed interoperability evidence,
not broad production validation; failure branches also have offline fault-injection coverage.

There is no command-line entry point, subprocess wrapper, or fallback to
ArtifactTool. Azure DevOps Server (on-premises) is unsupported; this targets
Azure DevOps Services. Do not treat the private transfer protocol as a
production-ready replacement for Microsoft's tooling.

## Publish a package

Use an identity with **Packaging: Read & write** and Feed Publisher/Contributor
access. Organization and authentication remain on the existing client:

```python
import os

from az_artifacts import PublishRequest, UniversalPackageClient

with UniversalPackageClient(
    "https://dev.azure.com/org",
    credential=os.environ["AZURE_DEVOPS_EXT_PAT"],
) as client:
    result = client.publish(
        PublishRequest(
            feed="feed",  # Name or ID
            name="my-tool",
            version="1.2.3",  # Exact, immutable Universal Package version
            path="./prepared-package",
            scope="project",
            project="project",  # Name or ID
            description="Prepared tool binaries",
        )
    )

print(result.metadata.version)
print(result.bytes_uploaded)
```

`PublishRequest` is a **frozen dataclass**: `feed`, `name`, and `version`
are `str`; `path` is `str | Path`; `scope: Scope = "organization"`,
`project: str | None = None`, and `description: str | None = None`.
For organization-scoped feeds omit `project` and use the default scope.
PAT strings, `BearerToken(token)`, and synchronous `TokenCredential` objects
are supported identically for publishing and downloading.

Package names are lowercase alphanumerics separated by nonconsecutive `-`, `_`,
or `.`. Publishing versions must be exact lowercase SemVer, without `+` build
metadata, wildcards, leading-zero numeric identifiers, or numeric components
larger than 2,147,483,647. Local validation never reserves a version.

`PublishResult` has `metadata: PackageMetadata`, absolute source `path: Path`,
package-relative `files: tuple[Path, ...]`, and `bytes_uploaded: int`.
The byte counter includes dedup request bodies (including node negotiation,
manifest chunks and node bodies), but excludes HTTP headers and registration JSON.
Existing content can make this much smaller than the source size.
`metadata.package_size` includes logical manifest bytes.

### Publishing guarantees and limitations

- An existing version raises **`PackageConflictError(ServiceError)`**.
  There is no overwrite, delete, version bump, or automatic replacement.
- **`IncompleteUploadError(ArtifactsError)`** means content/retention did not
  complete and registration was not attempted. Authentication and authorization
  errors retain their existing types. Filesystem failures raise `OSError`;
  unsafe entries raise `UnsafePathError`; invalid requests/limits raise `ValueError`.
- **`AmbiguousPublishError(ArtifactsError)`** means registration was attempted
  but readback could not confirm it. **Do not blindly retry.** Inspect the exact
  immutable version first. A lost registration response is reconciled with a
  read-only GET and exact version, content IDs, size, and description comparison.
  Ordinary credential-provider failures during this confirmation also raise
  `AmbiguousPublishError`, preserving the original exception as the cause.
- Only GET/HEAD/OPTIONS and the read-only blob-URL resolver retry transient
  failures. Upload transport failures and registration are never automatically
  retried. A node may be resubmitted after missing children or new retention
  proofs have been supplied; unsuccessful negotiation is bounded.
- Chunking uses a 1 MiB lookahead buffer and compatible 32–128 KiB Dedup64K
  chunks. Uploads use at most 64 chunks (8 MiB) per batch and
  `min(max_workers, 16)` simultaneous batches. Preparation records and the
  manifest have conservative budgets based on `max_manifest_bytes`; Python
  object/receipt overhead adds to these budgets. The whole package is never
  loaded into memory, and publishing creates **no local staging files**.
- Keep the source directory quiescent. All regular files, including dotfiles,
  are included. Before registration the inventory, file identities, metadata,
  and every original chunk hash are checked again. This detects ordinary
  changes, but is **not an atomic filesystem snapshot**.
- Symlinks, junctions, other Windows reparse points, special files, nonportable
  names, case/Unicode-normalization collisions, and paths deeper than 256
  components are rejected. Empty files work. Empty directories are omitted;
  a source with no regular files is rejected.
- Only paths and bytes are stored: not permissions, executable bits, timestamps,
  ACLs, alternate streams, sparse allocation, or hard-link relationships.
- Signed retention receipts remain in process memory only. No credentials or
  authentication caches are persisted. Failed uploads may leave unregistered
  dedup data until service retention expires; the library does not delete it.

### Protocol evidence and live-publish gate

The [evidence record](tests/fixtures/publishing/protocol_evidence.json) links
**normalized captured fixtures**, not raw live records or reusable approvals.
Organization/project/feed identities, instance IDs, package names, versions, and
dates have been replaced with synthetic fixture values. Original live-only records
remain outside the repository. Public protocol resource IDs, synthetic payload
hashes, and wire structures are retained; opaque signatures and capabilities are
not. Retention regression tests use wholly synthetic IDs and signatures matching
the response shape observed with Bicep.

The two Microsoft-tool reference registrations each returned
204 exactly once. The larger reference contains a 100 MiB deterministic payload,
an empty file, and 128 KiB of zeros; its full manifest is 427 bytes.

The [large capture](tests/fixtures/publishing/next-approved-publish.jsonl) establishes
node PUT/409 negotiation (`Missing`, `InsufficientKeepUntil`, `Receipts`), batched
chunk PUTs, signed retention acknowledgments, deterministic file ordering, packed
512-child trees, and registration proof bytes. Dedup 409 is **not** a package conflict.
An existing node's HTTP 200 acknowledgment can include its own receipt plus
receipts for unique immediate children. A Bicep retention capture returned 511
entries for a node with 512 child references (510 unique children). These optional
child receipts are accepted only for known immediate children; a valid, unexpired
receipt for the requested node remains mandatory. Shared-child receipt updates
never replace a stronger existing retention proof with an older one.
Publishing resolves the organization `instanceId` with a read-only `connectionData`
request and uses the canonical `A{instanceId}` dedup account path. This matches
all 39 captured dedup writes, rather than assuming the download root alias accepts writes.
The super-root contains the file-collection root followed by the manifest root.
Registration uses the [public SDK's][upack-push-client] four
[fields][upack-push-model]; proofs are base64 serialized nodes, not content ID strings.

Conservatively omitted header values were recovered using the installed SDK with
a **terminal in-memory HTTP handler and synthetic inputs**, not another remote write:
chunk headers are `length/false` for uncompressed bytes; `X-MS-KeepUntils` is an
ordered comma-separated UTC timestamp list; `X-MS-Signature` is base64 SHA256 of
concatenated child signatures in node order. The initial header capture mistook
some `length/false` strings for opaque base64; those omissions were not retroactively
filled in. [Wire vectors](tests/fixtures/publishing/sdk-wire-vectors.json) and
[30 chunker vectors](tests/fixtures/publishing/sdk-chunk-vectors.json) identify this
separate local evidence. The chunker is derived from MIT-licensed [BuildXL][buildxl-hashing].

Reference-only instrumentation is under `tests/interop`. The bounded diagnostic
hook does not intercept TLS, modify trust/proxy settings, or retain credentials,
signed URLs, opaque receipts, or raw tool logs. Gzip responses are decoded with
a 16 MiB limit. Microsoft tooling is never imported or invoked by the library.

The approved native cold-source smoke completed with exactly one registration
PUT/204, 23 successful chunk requests, and 1,368 uploaded chunks that had been
reported missing. Chunk bodies totaled 104,858,027 bytes (100 MiB plus the
427-byte manifest); node request bodies added 99,000 bytes. Both downloaders
verified all three files. See [the result](tests/fixtures/publishing/native-publish-result.json)
and [sanitized native exchanges](tests/fixtures/publishing/native-python-http-1.jsonl).

The real-tool packages include upstream binaries, licenses, and deterministic
provenance; ShellCheck also includes its matching source archive. Both downloaders
verified every package file, and the Microsoft-downloaded executables ran on
Linux x64. Their registration followed the shared-child receipt correction
described above; the failed initial Bicep attempt stopped before registration.

Completed immutable experiments must never be republished. To inspect the
normalized native fixture without any network or write:

```bash
uv run python tests/interop/native_publish.py
```

For a future experiment, create and review a **separate local JSON proposal**
(recommended: `.interop-local/proposal.json`, which is ignored). Set an explicitly
authorized `organization`, `scope`, `project` (for project scope), `feed`, `name`,
unused immutable `version`, `description`, absolute `source_directory`,
`artifacttool_path`, and absolute `evidence_directory` under `.interop-local/`.
Include `files` entries with package-relative `path`, `size`, and `sha256`, plus
`publisher: "native-python"` and `approved: true` only after that exact proposal
is approved. Do not copy completion flags or treat a fixture as approval.

With the Microsoft download reference and locally built capture hook ready, pass
`--proposal .interop-local/proposal.json --execute-approved`. Credentials come
only from the process-local `AZ_ARTIFACTS_REFERENCE_TOKEN`. The optional pytest
live test additionally requires `AZ_ARTIFACTS_RUN_APPROVED_NATIVE_INTEROP=1` and
`AZ_ARTIFACTS_NATIVE_PROPOSAL` pointing to this local file. Public fixture paths
and objects marked `fixture_only` are rejected. An exclusive attempt marker
prevents blind retries; all new evidence stays ignored, never overwriting public
fixtures. Native publishing is followed by both downloaders and exact hash checks.

`reference_tool.py` likewise requires `--proposal`, and Microsoft reference writes
additionally require `--execute-approved` with `publisher: "microsoft-artifacttool"`.
Its preflight uses the proposal organization's discovered package service, not a
hardcoded destination. `bicep_retention.py` requires its own proposal with
`publisher: "native-retention-only"` and `--execute-approved`; dedup writes are
still writes even when registration is disabled. Never run these against production.

For Microsoft **download-only** verification of another approved project-feed
package, `tests/interop/microsoft_download.py` accepts `--organization`, `--project`,
`--feed`, `--name`, `--version`, explicit `--tool`, and an empty `--path`. It reads
`AZ_ARTIFACTS_REFERENCE_TOKEN` only from the process environment, fixes the command
to `universal download`, and withholds raw tool stdout/stderr. Optional
`--capture-path` enables the sanitized hook outside the download destination.
Always compare downloaded files with the reviewed package hashes.

[upack-push-client]: https://github.com/microsoft/azure-devops-python-api/blob/86c9a559fc4ab309df21e674b236a542f9e77f89/azure-devops/azure/devops/v7_1/upack_packaging/upack_packaging_client.py
[upack-push-model]: https://github.com/microsoft/azure-devops-python-api/blob/86c9a559fc4ab309df21e674b236a542f9e77f89/azure-devops/azure/devops/v7_1/upack_packaging/models.py
[buildxl-hashing]: https://github.com/microsoft/BuildXL/tree/16e96dc02e86c23afdc6114b0126b4d8549a41e1/Public/Src/Cache/ContentStore/Hashing
[buildxl-upload]: https://github.com/microsoft/BuildXL/blob/16e96dc02e86c23afdc6114b0126b4d8549a41e1/Public/Src/Cache/ContentStore/Vsts/DedupContentSession.cs
[pipeline-manifest-publish]: https://github.com/microsoft/azure-pipelines-agent/blob/8853a22f5bc48094641eb284c731f3670235df29/src/Agent.Plugins/Artifact/PipelineArtifactServer.cs

## Installation

Requires Python **3.11 or newer**. The runtime dependencies are `httpx` and `wcmatch`.

For the released **download-only 0.1.0**, install from
[PyPI](https://pypi.org/project/az-artifacts/):

```bash
uv add az-artifacts
```

For the new APIs in this README, work from a current checkout instead:

```bash
uv sync --locked --group dev
uv run --locked python
```

Or install that checkout into another uv project (replace the path):

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
lifetime. Downloads, catalog, metadata, inspection/comparison, registration, and
publishing share this discovery.
Only locations used by this client are checked against its supported URL policy;
unrelated resource areas can advertise other ports without blocking downloads.
UPack resource-area identity takes precedence, followed by `UPackPackaging`,
`PackagingApi`, and the legacy `Packaging` name. Feed-management discovery remains
separate.

| Client argument | Default / meaning |
| --- | --- |
| `organization` | Required organization name, `https://dev.azure.com/org`, or legacy `https://org.visualstudio.com` URL. No automatic organization detection. |
| `credential` | Required explicit PAT, `BearerToken`, or synchronous `TokenCredential`. |
| `timeout` | `60.0` seconds; positive, finite HTTP timeout. |
| `retries` | `3`; bounded retries for read requests, including read-only Dedup URL-resolution POSTs. Use `0` to disable. Registration PUTs always disable retries independently of this setting. |
| `max_workers` | `4`; bounds file download workers and publishing batches (publishing additionally caps at 16). |
| `max_manifest_bytes` | `64 * 1024 * 1024`; decoded manifest limit and publishing preparation-record budgets. |
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

Exact Universal Package SemVer versions, including lowercase prereleases such as
`1.2.3-rc.1`, are accepted; build metadata (`+build`) is not supported.
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
| `metadata.package_size` | Advertised whole-package size, before filtering; can include manifest bytes. |
| `metadata.description` | Optional package description; missing/null is `None`, and an empty string is preserved. |
| `path` | Resolved destination `pathlib.Path`. |
| `files` | Tuple of relative `pathlib.Path` objects for downloaded files; combine with `result.path` to locate them. |
| `bytes_downloaded` | Logical file bytes written for the selected files, **not** network bytes, compressed transfer size, or bytes spent on metadata/retries. |

Downloads validate content hashes and sizes, bound manifest/decompression work,
use bounded workers and retries, and reject unsafe manifest/destination paths.
One shared chunk-fetch pool serves all file workers, with at most
`min(max_workers, 4)` pending chunks per file. This keeps large single-file
verification parallel without creating a thread pool for every file.
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

Library errors derive from `ArtifactsError`, including `ServiceError`, `AuthenticationError`,
`PermissionDeniedError`, `NotFoundError`, `PackageNotFoundError`, `VersionNotFoundError`,
`NoMatchingFilesError`, `TransportError`, `ProtocolError`, `IntegrityError`,
`LocalFileChangedError`, `UnsafePathError`, `ConflictError`, and
`RegistrationOutcomeUnknownError`. `AuthenticationError` (401),
`PermissionDeniedError` (403), `NotFoundError` (404), and `ConflictError` (409) are
`ServiceError` subclasses retaining `status_code` and an optional `request_id`.
Request IDs are accepted only as bounded ASCII identifier tokens; unsafe values
are discarded. Error messages omit response bodies, request URLs, credentials,
and proof data. Registration uncertainty is separate from `ServiceError` and is
**not** permission to retry; see its outcome contract below. Invalid arguments can raise
`ValueError`/`TypeError`, and local filesystem failures can raise ordinary `OSError`
subclasses.

## Discover feeds, packages, and versions

The hierarchy is **organization → feed → package → version → files**.
Project-scoped feeds additionally belong to a project. A file is part of a package
version, not independently versioned. Manifest-only inspection, path history, and
content comparison are available below. The low-level `add_package()` registration
primitive is separate from these read-only checks.

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
metadata and registration use the separate `pkgs.dev.azure.com` service. No NuGet-only `isListed`
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
`PackagePushMetadata` supplies the registration input described below.
The exported frozen `PackageVersionDeletionState(name, version, deleted_date=None)`
remains **data only**, with an optional timezone-aware UTC deletion timestamp;
there is no deletion/restore API.

### SDK capability mapping and experimental routing

The pinned [`v7_1.upack_packaging` SDK](https://github.com/microsoft/azure-devops-python-api/tree/86c9a559fc4ab309df21e674b236a542f9e77f89/azure-devops/azure/devops/v7_1/upack_packaging)
contains three operations and five models. This checkout covers that narrow
surface without an `azure-devops` or `msrest` runtime dependency:

| SDK operation | Native API / result |
| --- | --- |
| `add_package(metadata, feed_id, package_name, package_version, project=None)` | Keyword-only `add_package(feed=..., name=..., version=..., metadata=..., scope=..., project=...)` → `None` on acknowledged registration, not file upload. |
| `get_package_metadata(...)` | `get_package_metadata(...)` → `PackageMetadata`, with optional `intent`. |
| `get_package_versions_metadata(...)` | `get_package_versions_metadata(...)` → `LimitedPackageMetadataListResponse`; distinct from the richer catalog `list_package_versions()`. |

| SDK model | Exported frozen model |
| --- | --- |
| `UPackPackageMetadata` | `PackageMetadata(version, manifest_id, super_root_id, package_size, description=None)` |
| `UPackLimitedPackageMetadata` | `LimitedPackageMetadata(version, description=None)` |
| `UPackLimitedPackageMetadataListResponse` | `LimitedPackageMetadataListResponse(count, value)` |
| `UPackPackagePushMetadata` | `PackagePushMetadata(manifest_id, super_root_id, proof_nodes, description=None)` |
| `UPackPackageVersionDeletionState` | `PackageVersionDeletionState(name, version, deleted_date=None)`; no deletion operation. |

The catalog and file APIs are additional native capabilities, not full
FeedClient/UPackApiClient parity. Native methods require explicit supported
identities and immutable tuples where applicable, rather than SDK model coercion.

The pinned Python SDK uses location `4cdb2ced-0758-4651-8032-010f070dd7e5` and API
`7.1-preview.1` for [registration PUT and both metadata GETs](https://github.com/microsoft/azure-devops-python-api/blob/86c9a559fc4ab309df21e674b236a542f9e77f89/azure-devops/azure/devops/v7_1/upack_packaging/upack_packaging_client.py#L28-L101).
Its [route substitution](https://github.com/microsoft/azure-devops-python-api/blob/86c9a559fc4ab309df21e674b236a542f9e77f89/azure-devops/azure/devops/client.py#L117-L157)
removes omitted placeholder segments, but retains literal segments. Based on
that shared location and this library's existing exact-version route, the new
versionless GET uses
`/{project?}/_packaging/{feed}/upack/packages/{name}/versions`, without a final
version segment. The registration PUT uses that same route **with** the exact
version segment, as the metadata GET does. These are **inferred routes**, not
live-discovered/verified location templates. No alternative endpoint is tried on
failure. Live Azure DevOps compatibility, including collection completeness and
registration success/conflict semantics, remains an explicit experimental gate.

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
Full uploading/publishing remains unavailable; registration of already-uploaded
references is described below.

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

## Register already-uploaded content

`add_package()` is the final metadata-registration step, **not** a substitute for
uploading a directory. All content, its manifest/super-root, and suitable proof
strings must already exist through a separately authorized upload workflow.
The method performs no content upload, blob lookup, local file access, chunking,
proof generation, retention, overwrite, or automatic reconciliation. It requires
an open client, but **does not require an advertised Dedup service**.

Read-only APIs need **Packaging: Read** and feed access. Registration needs a
credential authorized for **Packaging: Read & write** and an appropriate feed
publisher role (Feed Publisher/Contributor); read visibility alone is insufficient.
PATs, `BearerToken`, and synchronous `TokenCredential` use the same explicit
authentication conventions as downloads.

All arguments are keyword-only:

| Argument | Meaning |
| --- | --- |
| `feed` | Required feed name or ID. |
| `name` | Required exact lowercase Universal Package name; nonconsecutive `-`, `_`, or `.` separators are allowed. |
| `version` | Required exact Universal Package SemVer, including prereleases; no wildcards or build metadata. |
| `metadata` | Required `PackagePushMetadata`, not a dictionary or another model. |
| `scope` | `"organization"` (default) or `"project"`. |
| `project` | Project name/ID required for project scope; omit for organization scope. |

`metadata.manifest_id` and `metadata.super_root_id` must be supported dedup IDs:
64 hexadecimal digits plus a `01` chunk or `02` node suffix. They are serialized
in uppercase without mutating the model. `proof_nodes` must be a **tuple of
strings**, not a list or a bare string. Proofs are opaque: order, duplicates, empty
strings, and an empty tuple are preserved without asserting that the service will
accept them as valid proofs. `description=None` **omits** the field; `description=""`
sends an empty string. This matches the SDK model's
[msrest serialization behavior](https://github.com/Azure/msrest-for-python/blob/master/msrest/serialization.py):
None attributes are omitted, while empty strings/arrays are retained.
The new body has camelCase `manifestId`, `superRootId`, and `proofNodes` keys, plus
`description` when supplied. Invalid caller types/fields raise `TypeError` or
`ValueError` before any network request, including resource discovery.

This illustrative integration function accepts **pre-existing** references; it
does not obtain them or upload files. Do not substitute invented IDs/proofs:

```python
from az_artifacts import PackagePushMetadata, UniversalPackageClient


def register_uploaded_version(
    client: UniversalPackageClient,
    *,
    existing_manifest_id: str,
    existing_super_root_id: str,
    existing_proof_nodes: tuple[str, ...],
) -> None:
    client.add_package(
        feed="your-feed",
        name="your-package",
        version="1.2.3-rc.1",  # Choose an explicitly authorized, unreserved version.
        metadata=PackagePushMetadata(
            manifest_id=existing_manifest_id,
            super_root_id=existing_super_root_id,
            proof_nodes=existing_proof_nodes,
            description=None,
        ),
        scope="project",
        project="your-project",
    )
```

### Acknowledgment, no replay, and uncertain outcomes

The registration PUT makes **one attempt**, regardless of client `retries`.
Transport errors, 429, and 5xx never cause automatic registration replay.
Resource discovery is still a read and retains its normal retries; a discovery
failure propagates before registration is attempted. Other reads, including the
Dedup URL-resolution POST used by downloads, retain their retry behavior.
Callers and custom transports must not independently replay the registration PUT.

| Registration outcome | Result |
| --- | --- |
| HTTP **200, 201, or 204**, with no async/partial headers | Returns `None`. No response schema is deserialized; an empty body is valid, and a bounded nonempty body is ignored as in the SDK. A normal `Location` header on 201 is allowed. |
| HTTP **409** | `ConflictError(ServiceError)` with status/request ID. Never treated as success or permission to overwrite. |
| HTTP **401 / 403 / 404** | `AuthenticationError` / `PermissionDeniedError` / `NotFoundError`, not absence or success. |
| Other **4xx except 408**, including **429** | `ServiceError`, no retry. These request rejections do not prove that a version is absent or reusable. |
| HTTP **408**, **5xx**, or other nonacknowledging statuses (including **202**, **206**, **207**, and redirects) | `RegistrationOutcomeUnknownError`; no replay or alternate-path attempt. |
| Async/partial signals even on 200/201/204 | `RegistrationOutcomeUnknownError` for nonempty `Azure-AsyncOperation`, `Operation-Location`, `Content-Range`, or `x-ms-continuationtoken` headers. No polling. |
| Transport or response-protocol failure during the PUT, including timeout/disconnection, malformed encoding, or an oversized response | `RegistrationOutcomeUnknownError`, conservatively even for connection failures. |

`RegistrationOutcomeUnknownError(ArtifactsError)` means **completion is not
established and the version may already have committed**. It retains optional
`status_code` and sanitized `request_id` when available; transport/protocol errors
may have neither. Sanitized underlying library errors are retained as causes,
without raw responses, URLs, credentials, or proofs.

Versions are **immutable and reserved even after deletion**. Existence/comparison
checks cannot authorize reuse or avoid races. After an uncertain outcome, stop
automatic processing and explicitly inspect the intended package's metadata
before deciding how to reconcile. Do not turn a subsequent 409 into success or
automatically delete/recreate a version. Neither a catalog miss nor one matching
file proves a registration succeeded. The PUT route and live response behavior
remain experimental; fixture success is not service interoperability evidence.

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

`add_package()` accepts a metadata description but does not upload content.
For `az artifacts universal publish`, the analogous Python call is
`client.publish(PublishRequest(feed, name, version, path, scope=..., project=...,
description=...))`. There is no upload CLI, CLI login/configuration integration,
or Azure DevOps Server support.

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
For catalog/metadata checks, compare accessible feed/project associations,
version lists (including prereleases/deletion states), optional intent behavior,
and exact metadata with Microsoft's tooling or known service fixtures. Verify
collection completeness and inaccessible/deleted-resource errors explicitly.
For comparison, use `compare_file()` with known unchanged local fixture files:
exercise a match, same-size different bytes, and a different size against both
single-chunk and multi-level dedup files. Keep the source stable during each call;
compare represented content, not current payload availability.

### Separate registration interoperability gate

Live registration requires **separate explicit authorization** for a disposable,
unreserved package version and valid pre-uploaded manifest/root/proof references,
coordinated with [issue #2](https://github.com/cataggar/az-artifacts/issues/2).
Do not extend the read-only smoke above to upload, register, retry, or delete
anything automatically. Verify the service location template and actual
acknowledgment/conflict/error responses before claiming compatibility.
No live registration or read-only smoke evidence is claimed here; the complete
local implementation and fixture coverage do not remove these release gates.

## Releasing the Python distribution to PyPI

[The release workflow](.github/workflows/pypi.yml) runs only on pushed `v*` tags.
It checks out the exact triggering commit, reads the static version from
`pyproject.toml` with `tomllib`, and requires an exact match such as
`v0.1.0` -> `0.1.0`. It runs tests, lint, formatting, and type checking before
building and smoke-installing the wheel and source distribution. A separate
publish job downloads those build artifacts and uses OIDC Trusted Publishing
inside the `pypi` environment. There are no PyPI password/API-token secrets.

**Maintainer checklist for subsequent releases or a fork:**

1. PyPI `az-artifacts` 0.1.0 is already released (download-only). Choose a new
   version for any later release; do not reuse `v0.1.0`. Forks need their own
   available distribution name and PyPI project or pending Trusted Publisher.
2. Verify the GitHub Trusted Publisher on PyPI with owner `cataggar`,
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

PyPI publishing here distributes the **Python library**; it does not itself
publish any Universal Package to an Azure Artifacts feed.

## License and provenance

MIT licensed. The native download implementation is based on the Microsoft
MIT-licensed Rust code in
`../azure-devops-rust-api/azure_devops_rust_api/src/artifacts_download`, including
its Universal Package metadata, deduplication, and decompression protocol work.
The native chunker and packed-tree builder are adapted from MIT-licensed BuildXL;
the typed node format and content hashes also follow Microsoft's
[BuildXL hashing implementation](https://github.com/microsoft/BuildXL/tree/main/Public/Src/Cache/ContentStore/Hashing).
The exact upstream Microsoft license and copyright notice are retained in the
root [LICENSE](LICENSE); preserve that notice when redistributing derived code.
This Python implementation is not an official Microsoft SDK.
