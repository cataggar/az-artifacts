# az-artifacts

Native Python **downloads and read-only metadata** for Azure DevOps Universal
Packages, without ArtifactTool or an Azure CLI runtime dependency.

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
lifetime. Downloads and metadata methods perform this discovery automatically.

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
`NoMatchingFilesError`. Full Azure CLI/ArtifactTool glob parity is not claimed.

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
`PermissionDeniedError`, `NotFoundError`, `VersionNotFoundError`,
`NoMatchingFilesError`, `TransportError`, `ProtocolError`, `IntegrityError`, and
`UnsafePathError`. Invalid arguments can raise `ValueError`/`TypeError`, and local
filesystem failures can raise ordinary `OSError` subclasses.

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
