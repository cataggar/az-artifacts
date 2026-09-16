# az-artifacts

Native Python access to Azure DevOps Universal Packages: publish and download
files, discover packages, read metadata, and inspect or compare package contents.
No Azure CLI or ArtifactTool runtime dependency.

## Installation

Requires Python 3.11 or newer.

```bash
uv add az-artifacts
```

PyPI 0.1.0 supports downloads. Publishing and the additional APIs require a
current checkout until the next release; see [source installation](doc/reference.md#installation).

## Quick start

Provide a PAT with Packaging: Read and access to the feed through your
environment or secret store, not source code or command-line arguments.

```python
import os

from az_artifacts import UniversalPackageClient

with UniversalPackageClient(
    "https://dev.azure.com/org",
    credential=os.environ["AZURE_DEVOPS_EXT_PAT"],
) as client:
    result = client.download(
        feed="feed",
        name="package",
        version="1.2.3",
        path="./download",
        scope="project",
        project="project",
    )

print(result.files)
```

## Documentation

- [Publish packages](doc/reference.md#publish-a-package)
- [Download options and authentication](doc/reference.md#download-a-package)
- [Discover feeds, packages, and versions](doc/reference.md#discover-feeds-packages-and-versions)
- [Read metadata](doc/reference.md#read-package-metadata)
- [Compare local files](doc/reference.md#compare-a-local-file-before-uploading)
- [Inspect files and path history](doc/reference.md#inspect-package-files-and-path-history)
- [Register already-uploaded content](doc/reference.md#register-already-uploaded-content)
- [Development and releases](doc/reference.md#development-and-verification)

## License

[MIT](LICENSE). See [provenance](doc/reference.md#license-and-provenance) for
upstream attribution. This is not an official Microsoft SDK.
