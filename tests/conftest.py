import hashlib
import json
import struct
from collections import Counter
from urllib.parse import quote

import httpx
import pytest

from az_artifacts import UniversalPackageClient
from az_artifacts.models import BlobRef

PACKAGE_ID = "11111111-1111-4111-8111-111111111111"


def identifier(data, kind="01"):
    return hashlib.sha512(data).digest()[:32].hex().upper() + kind


def node_bytes(children):
    data = bytearray(struct.pack("<HH", 0, len(children) - 1))
    for child in children:
        is_node = child.id.endswith("02")
        data.append(int(is_node))
        data.extend(child.size.to_bytes(7 if is_node else 3, "little"))
        data.extend(bytes.fromhex(child.id[:-2]))
    return bytes(data)


class AzureService:
    def __init__(self):
        self.blobs = {}
        self.requests = []
        self.resolve_counts = Counter()
        self.expire = set()
        self.missing_urls = set()
        self.version = "1.2.3"
        self.metadata_intent = "Download"
        self.versions_metadata = {"count": 1, "value": [{"version": self.version}]}
        self.versions_metadata_headers = {}
        self.versions_metadata_status = 200
        self.package_pages = [[{"id": PACKAGE_ID, "name": "package"}]]
        self.versions = ["1.2.3"]
        self.services = [
            {"name": "Packaging", "locationUrl": "https://pkgs.dev.azure.com/org/"},
            {"name": "Dedup", "locationUrl": "https://vsblob.dev.azure.com/org/"},
        ]
        self.manifest({})

    def chunk(self, data, *, wire=None):
        ref = BlobRef(identifier(data), len(data))
        self.blobs[ref.id] = data if wire is None else wire
        return ref

    def node(self, children):
        data = node_bytes(children)
        ref = BlobRef(identifier(data, "02"), sum(child.size for child in children))
        self.blobs[ref.id] = data
        return ref

    def manifest(self, files, *, chunked=False):
        data = json.dumps(
            {
                "items": [
                    {"path": path, "blob": {"id": ref.id.lower(), "size": ref.size}}
                    for path, ref in files.items()
                ]
            }
        ).encode()
        if chunked:
            midpoint = len(data) // 2
            root = self.node([self.chunk(data[:midpoint]), self.chunk(data[midpoint:])])
        else:
            root = self.chunk(data)
        self.metadata = {
            "version": self.version,
            "manifestId": root.id.lower(),
            "superRootId": "ab" * 32 + "02",
            "packageSize": sum(ref.size for ref in files.values()),
        }
        return root

    def __call__(self, request):
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "blob.example":
            assert "authorization" not in request.headers
            key = path.rsplit("/", 1)[-1]
            if key in self.expire:
                self.expire.remove(key)
                return httpx.Response(403)
            if key not in self.blobs:
                return httpx.Response(404)
            return httpx.Response(200, content=self.blobs[key])
        assert request.headers["authorization"] == "Basic OnRlc3QtcGF0"
        if path.endswith("/_apis/ResourceAreas"):
            return httpx.Response(200, json={"value": self.services})
        if host == "pkgs.dev.azure.com" and "/_packaging/" in path:
            assert request.method == "GET"
            assert request.headers["accept"] == "application/json; api-version=7.1-preview.1"
            if path.endswith("/versions"):
                assert not request.url.params
                return httpx.Response(
                    self.versions_metadata_status,
                    json=self.versions_metadata,
                    headers=self.versions_metadata_headers,
                )
            if "/upack/packages/" in path and path.rsplit("/", 2)[-2] == "versions":
                expected = (
                    {"intent": self.metadata_intent} if self.metadata_intent is not None else {}
                )
                assert request.url.params == httpx.QueryParams(expected)
                return httpx.Response(200, json=self.metadata)
        if path.endswith("/_apis/dedup/urls"):
            assert request.method == "POST"
            assert request.url.params["allowEdge"] == "true"
            requested = json.loads(request.content)
            self.resolve_counts.update(requested)
            return httpx.Response(
                200,
                json={
                    key.lower(): f"https://blob.example/{quote(key)}?sig=redacted"
                    for key in requested
                    if key not in self.missing_urls
                },
            )
        if host == "feeds.dev.azure.com" and path.endswith("/packages"):
            assert request.url.params["protocolType"] == "upack"
            page = int(request.url.params["$skip"]) // 100
            values = self.package_pages[page] if page < len(self.package_pages) else []
            return httpx.Response(200, json={"value": values})
        if host == "feeds.dev.azure.com" and path.endswith(f"/{PACKAGE_ID}/versions"):
            assert request.url.params == httpx.QueryParams(
                {"api-version": "7.1", "isDeleted": "false"}
            )
            return httpx.Response(
                200,
                json={
                    "value": [
                        value if isinstance(value, dict) else {"version": value}
                        for value in self.versions
                    ]
                },
            )
        pytest.fail(f"Unexpected request: {request.method} {host}{path}")


@pytest.fixture
def service():
    return AzureService()


@pytest.fixture
def client(service):
    with UniversalPackageClient(
        "https://dev.azure.com/org",
        credential="test-pat",
        transport=httpx.MockTransport(service),
        retries=0,
        max_workers=1,
    ) as instance:
        yield instance
