"""Private, bounded acceptance instrumentation (never imported by the library)."""

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePath
from urllib.parse import urljoin, urlsplit

import httpx

from az_artifacts import BearerToken
from az_artifacts.errors import (
    AuthenticationError,
    LocalFileChangedError,
    NotFoundError,
    PackageNotFoundError,
    PermissionDeniedError,
    ProtocolError,
    RegistrationOutcomeUnknownError,
    ServiceError,
    TransportError,
)

ROOT = Path(__file__).resolve().parents[2]
ID = re.compile(r"[0-9A-Fa-f]{64}0[12]\Z")
ENV = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
METHODS = (
    "list_feeds",
    "list_packages",
    "list_package_versions",
    "package_version_exists",
    "get_package_metadata",
    "get_package_versions_metadata",
    "list_files",
    "file_exists",
    "list_file_versions",
    "compare_file",
)


class Reason(StrEnum):
    OK = "ok"
    EXPECTED_ERROR = "expected-error"
    NOT_RUN = "not-run"
    COVERAGE_GAP = "coverage-gap"
    MISSING_FIXTURE = "missing-fixture"
    BUDGET = "budget-exhausted"
    REQUEST_BOUNDARY = "request-boundary"
    FILESYSTEM_BOUNDARY = "filesystem-boundary"
    BASELINE_MISMATCH = "baseline-mismatch"
    CONFIGURATION = "gate-or-configuration"
    AUTHENTICATION = "authentication-error"
    PERMISSION = "permission-error"
    NOT_FOUND = "not-found"
    SERVICE = "service-error"
    PROTOCOL = "protocol-error"
    TRANSPORT = "transport-error"
    LOCAL_SOURCE = "local-source-error"
    REGISTRATION_UNKNOWN = "registration-unknown"
    UNEXPECTED_ERROR = "unexpected-error"


class Incomplete(Exception):
    """A required fixture or configured budget is unavailable; never a pass."""

    def __init__(self, message, *, reason=Reason.MISSING_FIXTURE):
        super().__init__(message)
        self.reason = Reason(reason)


class BoundaryError(Exception):
    """A gate, privacy, write, or request boundary was violated."""

    def __init__(self, message, *, reason=Reason.CONFIGURATION):
        super().__init__(message)
        self.reason = Reason(reason)


def reason_code(error):
    """Map trusted types to fixed codes; never inspect exception text or causes."""
    if isinstance(error, (Incomplete, BoundaryError)):
        return error.reason if isinstance(error.reason, Reason) else Reason.UNEXPECTED_ERROR
    for types, reason in (
        ((FileNotFoundError, KeyError), Reason.MISSING_FIXTURE),
        ((AuthenticationError,), Reason.AUTHENTICATION),
        ((PermissionDeniedError,), Reason.PERMISSION),
        ((NotFoundError, PackageNotFoundError), Reason.NOT_FOUND),
        ((RegistrationOutcomeUnknownError,), Reason.REGISTRATION_UNKNOWN),
        ((ServiceError,), Reason.SERVICE),
        ((ProtocolError, httpx.DecodingError, json.JSONDecodeError), Reason.PROTOCOL),
        ((TransportError, httpx.TransportError), Reason.TRANSPORT),
        ((LocalFileChangedError, OSError), Reason.LOCAL_SOURCE),
        ((ValueError, TypeError), Reason.CONFIGURATION),
    ):
        if isinstance(error, types):
            return reason
    return Reason.UNEXPECTED_ERROR


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default prints unrecognized argument values verbatim.
        raise BoundaryError("Invalid acceptance arguments")


def require(condition, *, reason=Reason.CONFIGURATION):
    if not condition:
        raise BoundaryError("Acceptance boundary rejected", reason=reason)


def private_root(value):
    path = Path(value)
    require(path.is_absolute())
    path = path.resolve(strict=True)
    require(path.is_dir() and not path.is_relative_to(ROOT))
    # Reject any Git worktree, including nested repositories and .git files.
    require(not any((parent / ".git").exists() for parent in (path, *path.parents)))
    return path


def private_path(root, value, *, exists=True):
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve(strict=exists)
    require(path != root and path.is_relative_to(root))
    require(not any((parent / ".git").exists()
                    for parent in (path, *path.parents) if parent.is_relative_to(root)))
    return path


def clean_input(value):
    """Reject credentials/capabilities rather than trying to redact raw captures."""
    if isinstance(value, dict):
        for key, child in value.items():
            require(isinstance(key, str))
            require(key.lower() not in {
                "token", "password", "authorization", "headers", "receipts",
                "signature", "proofnodes", "proof_nodes", "signed_url",
                "credential", "credentials", "pat", "access_token", "accesstoken",
                "bearer_token", "client_secret", "secret", "capabilities",
            })
            clean_input(child)
    elif isinstance(value, list):
        for child in value:
            clean_input(child)
    elif isinstance(value, str) and "://" in value:
        parsed = urlsplit(value)
        require(not (parsed.query or parsed.fragment or parsed.username or parsed.password))


def read_json(path, *, max_bytes=16 * 1024 * 1024, expected_sha256=None):
    with path.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise Incomplete("Input budget exhausted", reason=Reason.BUDGET)
    if expected_sha256 is not None:
        require(hashlib.sha256(data).hexdigest() == expected_sha256)
    try:
        value = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise BoundaryError("Invalid acceptance JSON input") from None
    clean_input(value)
    return value


def load_config(root, path, kind, *, expected_sha256=None):
    config = read_json(private_path(root, path), expected_sha256=expected_sha256)
    require(config.get("schema") == 1 and config.get("kind") == kind)
    require(config.get("fixture_only") is False)
    require(isinstance(config.get("organization"), str) and config["organization"])
    require(isinstance(config.get("credential_env"), str))
    require(ENV.fullmatch(config["credential_env"]) is not None)
    require(config.get("credential_kind") in ("pat", "bearer"))
    require(isinstance(config.get("evidence_directory"), str))
    private_path(root, config["evidence_directory"], exists=False)
    return config


def credential(config):
    token = os.environ.get(config["credential_env"])
    require(isinstance(token, str) and bool(token))
    return BearerToken(token) if config["credential_kind"] == "bearer" else token


def normalized(value):
    if is_dataclass(value):
        return normalized(asdict(value))
    if isinstance(value, dict):
        return {key: normalized(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalized(child) for child in value]
    if isinstance(value, PurePath):
        return value.as_posix()
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def limits_from(config):
    limits = config["limits"]
    maxima = {
        "requests": 10000, "items": 250000, "seconds": 3600,
        "response_bytes": 64 * 1024 * 1024, "total_bytes": 256 * 1024 * 1024,
        "manifest_bytes": 64 * 1024 * 1024, "cases": 1000,
    }
    require(set(limits) == set(maxima))
    for key, maximum in maxima.items():
        require(type(limits[key]) is int and 0 < limits[key] <= maximum)
    return limits


class Budget:
    def __init__(self, limits):
        self.limits = limits
        self.deadline = time.monotonic() + limits["seconds"]
        self.requests = self.items = self.bytes = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise Incomplete("Deadline exhausted", reason=Reason.BUDGET)

    def request(self):
        self.check()
        if self.requests >= self.limits["requests"]:
            raise Incomplete("Request budget exhausted", reason=Reason.BUDGET)
        self.requests += 1

    def add_items(self, count):
        self.items += count
        if self.items > self.limits["items"]:
            raise Incomplete("Item budget exhausted", reason=Reason.BUDGET)
        self.check()

    def add_bytes(self, count):
        self.bytes += count
        if self.bytes > self.limits["total_bytes"]:
            raise Incomplete("Byte budget exhausted", reason=Reason.BUDGET)
        self.check()


def bounded_values(values, budget):
    result = []
    for value in values:
        budget.add_items(1)
        result.append(value)
    return result


def digest(data, kind="01"):
    return hashlib.sha512(data).digest()[:32].hex().upper() + kind


def node_children(data):
    """Independent decoder for the pinned SDK's serialized Dedup node layout."""
    require(len(data) >= 4 and data[:2] == b"\0\0")
    count = int.from_bytes(data[2:4], "little") + 1
    require(count <= 512)
    children, offset = [], 4
    for _ in range(count):
        require(offset < len(data) and data[offset] in (0, 1))
        node = data[offset] == 1
        width = 7 if node else 3
        end = offset + 1 + width + 32
        require(end <= len(data))
        children.append((
            data[offset + 1 + width:end].hex().upper() + ("02" if node else "01"),
            int.from_bytes(data[offset + 1:offset + 1 + width], "little"),
        ))
        offset = end
    require(offset == len(data))
    return children


class ManifestOracle:
    """Allow manifest chunks and tree nodes, never file-payload chunks.

    Inputs are independently captured *decoded* manifest/node bytes. No native
    parser, native metadata response, file payload, URL or retention proof is used.
    """

    def __init__(self, entry, budget):
        self.allowed = set()
        self.file_nodes = set()
        self.files = []
        self.features = set()
        self.blobs = {}
        for identifier, encoded in entry["blobs"].items():
            require(ID.fullmatch(identifier) is not None)
            data = base64.b64decode(encoded, validate=True)
            budget.add_bytes(len(data))
            require(digest(data, identifier[-2:]) == identifier.upper())
            self.blobs[identifier.upper()] = data
        self.root = entry["manifest_id"].upper()
        require(ID.fullmatch(self.root) is not None)
        manifest = self._manifest(self.root, budget, set())
        self.features.add("raw-manifest" if self.root.endswith("01") else "chunked-manifest")
        for item in json.loads(manifest)["items"]:
            budget.add_items(1)
            identifier = item["blob"]["id"].upper()
            size = item["blob"]["size"]
            require(ID.fullmatch(identifier) is not None and type(size) is int and size >= 0)
            path = item["path"]
            require(isinstance(path, str))
            path = path.removeprefix("/")
            require(path and "\\" not in path and all(p not in ("", ".", "..")
                                                     for p in path.split("/")))
            leaves, depth = self._tree(identifier, size, budget, set())
            feature = ("empty-file" if size == 0 else
                       "single-chunk" if identifier.endswith("01") else
                       "multilevel-node" if depth > 1 else "multichunk")
            if identifier.endswith("02") and leaves < 2:
                raise Incomplete("Required tree fixture is not multichunk")
            self.files.append({"path": path, "size": size, "content_id": identifier,
                               "feature": feature})
        require(len({file["path"] for file in self.files}) == len(self.files))
        # Superfluous blobs may conceal payload captures; do not accept them.
        require(set(self.blobs) <= self.allowed | self.file_nodes)
        self.allowed |= self.file_nodes

    def _data(self, identifier):
        if identifier not in self.blobs:
            raise Incomplete("Required manifest or node capture is unavailable")
        return self.blobs[identifier]

    def _manifest(self, identifier, budget, ancestors):
        budget.add_items(1)
        require(len(ancestors) < 64 and identifier not in ancestors)
        self.allowed.add(identifier)
        data = self._data(identifier)
        if identifier.endswith("01"):
            if len(data) > budget.limits["manifest_bytes"]:
                raise Incomplete("Manifest budget exhausted", reason=Reason.BUDGET)
            return data
        result = bytearray()
        for child, size in node_children(data):
            content = self._manifest(child, budget, ancestors | {identifier})
            require(len(content) == size)
            if len(result) + len(content) > budget.limits["manifest_bytes"]:
                raise Incomplete("Manifest budget exhausted", reason=Reason.BUDGET)
            result.extend(content)
        return bytes(result)

    def _tree(self, identifier, size, budget, ancestors):
        budget.add_items(1)
        require(len(ancestors) < 64 and identifier not in ancestors)
        if identifier.endswith("01"):
            if size == 0:
                require(identifier == digest(b""))
            return 1, 0
        self.file_nodes.add(identifier)
        children = node_children(self._data(identifier))
        require(sum(length for _, length in children) == size)
        results = [self._tree(child, length, budget, ancestors | {identifier})
                   for child, length in children]
        return sum(count for count, _ in results), 1 + max(depth for _, depth in results)


_audit_state = threading.local()


def _audit(event, args):
    state = getattr(_audit_state, "active", None)
    if state is None:
        return
    if event == "open":
        _, mode, flags = args
        write = (isinstance(mode, str) and any(flag in mode for flag in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT |
                                               os.O_TRUNC | os.O_APPEND)
        )
        if write:
            state["violation"] = True
            raise BoundaryError("Inspection filesystem write blocked",
                                reason=Reason.FILESYSTEM_BOUNDARY)
    elif event in {
        "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.link", "os.symlink",
        "os.truncate", "os.chmod", "os.chown", "os.utime", "shutil.copyfile",
        "subprocess.Popen", "os.system",
    }:
        state["violation"] = True
        raise BoundaryError("Inspection mutation blocked", reason=Reason.FILESYSTEM_BOUNDARY)


sys.addaudithook(_audit)


@contextmanager
def readonly_filesystem():
    # Client uses max_workers=1. The guard is scoped to the calling thread so
    # unrelated application threads are not prevented from writing their files.
    require(getattr(_audit_state, "active", None) is None)
    state = {"violation": False}
    _audit_state.active = state
    try:
        yield
    finally:
        _audit_state.active = None
        if state["violation"]:
            raise BoundaryError("Inspection mutation attempted", reason=Reason.FILESYSTEM_BOUNDARY)


@contextmanager
def quiet_http_logs():
    # HTTPX's normal INFO request line includes signed URLs. Never let a caller's
    # verbose logging turn metadata-only evidence into an accidental capability log.
    loggers = [logging.getLogger(name) for name in logging.Logger.manager.loggerDict
               if name == "httpx" or name.startswith(("httpx.", "httpcore"))]
    previous = [(logger, logger.disabled) for logger in loggers]
    for logger in loggers:
        logger.disabled = True
    try:
        yield
    finally:
        for logger, disabled in previous:
            logger.disabled = disabled


class GuardTransport(httpx.BaseTransport):
    """Trace every real request, including redirects, before sending any bytes."""

    def __init__(self, inner, budget):
        self.inner, self.budget = inner, budget
        self.records = []
        self.allowed = set()
        self.urls = {}
        self.case = "setup"
        self.routes = {}
        self.metadata_urls = set()
        self.metadata_roots = set()
        self.fetched = set()
        self.registration_url = None
        self.registration_body = None
        self.puts_remaining = 0
        self.puts = 0
        self.violated = False
        self.page_offsets = []
        self.intent = None
        self.name_query = None
        self.page_size = None

    def deny(self):
        self.violated = True
        raise BoundaryError("Network request boundary rejected", reason=Reason.REQUEST_BOUNDARY)

    def handle_request(self, request):
        self.budget.request()
        url = str(request.url)
        method = request.method
        identifiers = []
        operation = self.routes.get((method, urlsplit(url)._replace(query="").geturl()))
        if url in self.urls:
            if method != "GET" or "authorization" in request.headers:
                self.deny()
            operation = "manifest-or-node"
        elif method == "PUT":
            if (url != self.registration_url or self.puts_remaining != 1
                    or json.loads(request.content) != self.registration_body):
                self.deny()
            self.puts_remaining = 0
            self.puts += 1
            operation = "registration"
        elif operation == "resolver":
            identifiers = json.loads(request.content)
            if (not isinstance(identifiers, list) or not identifiers
                    or not all(isinstance(i, str) and i.upper() in self.allowed
                               for i in identifiers)):
                self.deny()
        elif operation is None or method != "GET":
            self.deny()
        if operation == "packages":
            query = request.url.params
            if (query.get("isRelease") is not None
                    or query.get("packageNameQuery") != self.name_query
                    or query.get("protocolType") != "upack"
                    or (self.page_size is not None and query.get("$top") != str(self.page_size))):
                self.deny()
            self.page_offsets.append(int(query.get("$skip", "0")))
        if operation == "limited-metadata" and request.url.query:
            self.deny()
        if operation == "metadata":
            expected_params = {"intent": self.intent} if self.intent is not None else {}
            if request.url.params != httpx.QueryParams(expected_params):
                self.deny()
            record_intent = self.intent is not None
        remaining = max(0.001, self.budget.deadline - time.monotonic())
        request.extensions["timeout"] = dict.fromkeys(
            ("connect", "read", "write", "pool"), min(15.0, remaining)
        )
        record = {"case": self.case, "operation": operation, "method": method}
        if operation == "metadata":
            record["intent_present"] = record_intent
        self.records.append(record)
        response = self.inner.handle_request(request)
        try:
            record["status"] = response.status_code
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                self.budget.add_bytes(len(chunk))
                if len(data) + len(chunk) > self.budget.limits["response_bytes"]:
                    raise Incomplete("Response budget exhausted", reason=Reason.BUDGET)
                data.extend(chunk)
            self.budget.check()
            record["bytes"] = len(data)
            if operation == "metadata" and response.status_code == 200:
                # Observation must not preempt the native parser's typed errors.
                # Invalid metadata is forwarded unchanged and earns no coverage.
                try:
                    body = json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = None
                if isinstance(body, dict):
                    identifier = body.get("manifestId")
                    if isinstance(identifier, str) and ID.fullmatch(identifier) is not None:
                        self.metadata_roots.add(identifier.upper())
            if operation == "manifest-or-node" and response.status_code == 200:
                self.fetched.add(self.urls[url])
            if operation == "resolver" and response.status_code == 200:
                urls = json.loads(data)
                require(isinstance(urls, dict), reason=Reason.PROTOCOL)
                for identifier, location in urls.items():
                    if identifier.upper() not in {i.upper() for i in identifiers}:
                        self.deny()
                    require(isinstance(location, str), reason=Reason.PROTOCOL)
                    self.urls[str(httpx.URL(location))] = identifier.upper()
            if (operation == "manifest-or-node"
                    and response.status_code in (301, 302, 303, 307, 308)):
                location = response.headers.get("location")
                require(bool(location), reason=Reason.PROTOCOL)
                self.urls[str(httpx.URL(urljoin(url, location)))] = self.urls[url]
            if operation in ("feeds", "packages", "versions", "limited-metadata"):
                if response.status_code == 200:
                    body = json.loads(data)
                    values = body.get("value", [])
                    require(isinstance(values, list), reason=Reason.PROTOCOL)
                    self.budget.add_items(len(values))
                    record["items"] = len(values)
                    if type(body.get("count")) is int:
                        record["count"] = body["count"]
            # iter_bytes() already decoded Content-Encoding. Preserve every other
            # header (including duplicates); HTTPX supplies the decoded length.
            headers = [(key, value) for key, value in response.headers.raw
                       if key.lower() not in (b"content-encoding", b"content-length")]
            return httpx.Response(response.status_code, headers=headers,
                                  content=bytes(data), request=request)
        finally:
            response.close()

    def close(self):
        self.inner.close()


def route(base, *parts):
    from urllib.parse import quote

    return base.rstrip("/") + "/" + "/".join(quote(str(part), safe="") for part in parts)


def configure_routes(guard, config, target, addressing, case):
    services = config["services"]
    # Independently approved service roots, not learned from native discovery.
    require(set(services) == {"feeds", "packaging", "dedup"})
    for value in services.values():
        require(isinstance(value, str) and urlsplit(value).scheme == "https")
    args = case["args"]
    project = [target["project"][addressing]] if target["scope"] == "project" else []
    feed = target["feed"][addressing]
    catalog = route(services["feeds"], *project, "_apis", "packaging", "Feeds")
    guard.routes = {
        ("GET", route(config["organization"], "_apis", "ResourceAreas")): "discovery",
    }
    if case["method"] == "list_feeds":
        guard.routes["GET", catalog] = "feeds"
    if case["method"] in ("list_packages", "list_package_versions", "package_version_exists",
                          "file_exists", "compare_file"):
        guard.routes["GET", route(catalog, feed, "packages")] = "packages"
        names = [args["name"]] if "name" in args else []
        for package_id in (target["package_ids"][name] for name in names
                           if name in target["package_ids"]):
            versions_url = route(catalog, feed, "packages", package_id, "versions")
            guard.routes["GET", versions_url] = "versions"
    if "name" in args and case["method"] not in ("list_package_versions", "package_version_exists"):
        metadata = route(services["packaging"], *project, "_packaging", feed,
                         "upack", "packages", args["name"], "versions")
        if case["method"] == "get_package_versions_metadata":
            guard.routes["GET", metadata] = "limited-metadata"
        versions = args.get("versions", [args["version"]] if "version" in args else [])
        for version in versions:
            guard.routes["GET", route(metadata, version)] = "metadata"
    if case["method"] in ("list_files", "file_exists", "list_file_versions", "compare_file"):
        guard.routes["POST", route(services["dedup"], "_apis", "dedup", "urls")] = "resolver"
    guard.allowed = set()
    guard.urls = {}
    guard.metadata_roots = set()
    guard.fetched = set()
    guard.page_offsets = []
    guard.intent = args.get("intent")
    guard.name_query = args.get("name_query", args.get("name"))
    guard.page_size = args.get("page_size")
    return {"scope": target["scope"], "feed": feed,
            **({"project": project[0]} if project else {})}


def write_report(output, report, guard):
    # Only fixed case IDs, enumerated operations/statuses, booleans and counts.
    with (output / "report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    with (output / "requests.json").open("x", encoding="utf-8") as stream:
        json.dump(guard.records, stream, indent=2)
    print(json.dumps({"status_counts": dict(Counter(row["status"] for row in report["cases"]))}))
