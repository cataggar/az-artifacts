import tomllib
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from az_artifacts._http import Http
from az_artifacts.auth import ADO_SCOPE, BearerToken
from az_artifacts.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ProtocolError,
    ServiceError,
    TransportError,
)

API = "https://dev.azure.com/org/_apis/ResourceAreas"
BLOB = "https://blob.example/content?sig=do-not-print"


def make_http(handler, credential="secret", retries=0):
    return Http(credential, timeout=1, retries=retries, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("authenticated", [True, False])
def test_user_agent_matches_project_version(authenticated):
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    version = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"]

    def handler(request):
        assert request.headers["user-agent"] == f"az-artifacts/{version}"
        return httpx.Response(200)

    http = make_http(handler)
    try:
        http.request("GET", API if authenticated else BLOB, authenticated=authenticated)
    finally:
        http.close()


def test_credentials_and_cookies_not_sent_to_signed_urls():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, content=b"ok", headers={"set-cookie": "session=private; Path=/; Secure"}
        )

    http = make_http(handler)
    try:
        http.request("GET", API)
        http.request("GET", API, authenticated=False)
    finally:
        http.close()
    assert requests[0].headers["authorization"] == "Basic OnNlY3JldA=="
    assert "authorization" not in requests[1].headers
    assert "cookie" not in requests[1].headers


def test_bearer_token():
    def handler(request):
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200)

    http = make_http(handler, BearerToken("token"))
    try:
        http.request("GET", API)
    finally:
        http.close()
    assert "token" not in repr(BearerToken("token"))


def test_token_credential_refreshes_for_each_request():
    scopes = []

    class Credential:
        def get_token(self, *values):
            scopes.append(values)
            return BearerToken("token")

    http = make_http(lambda _: httpx.Response(200), Credential())
    try:
        http.request("GET", API)
        http.request("GET", API)
    finally:
        http.close()
    assert scopes == [(ADO_SCOPE,), (ADO_SCOPE,)]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (400, ServiceError),
    ],
)
def test_service_errors_are_structured_and_redacted(status, error):
    http = make_http(
        lambda _: httpx.Response(
            status, content=b"do-not-print", headers={"x-vss-e2eid": "request"}
        )
    )
    try:
        with pytest.raises(error) as raised:
            http.request("GET", BLOB, authenticated=False)
    finally:
        http.close()
    assert raised.value.status_code == status
    assert raised.value.request_id == "request"
    assert "do-not-print" not in str(raised.value)


def test_retry_after(monkeypatch):
    waits = []
    monkeypatch.setattr("az_artifacts._http.time.sleep", waits.append)
    responses = iter([httpx.Response(429, headers={"retry-after": "2"}), httpx.Response(200)])
    http = make_http(lambda _: next(responses), retries=1)
    try:
        assert http.request("GET", API).status == 200
    finally:
        http.close()
    assert waits == [2]


def test_retry_after_http_date(monkeypatch):
    waits = []
    monkeypatch.setattr("az_artifacts._http.time.sleep", waits.append)
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=10))
    responses = iter([httpx.Response(503, headers={"retry-after": date}), httpx.Response(200)])
    http = make_http(lambda _: next(responses), retries=1)
    try:
        http.request("GET", API)
    finally:
        http.close()
    assert len(waits) == 1
    assert 0 < waits[0] <= 10


def test_long_retry_after_surfaces_error_without_retrying(monkeypatch):
    monkeypatch.setattr("az_artifacts._http.time.sleep", lambda _: pytest.fail("unexpected retry"))
    http = make_http(lambda _: httpx.Response(429, headers={"retry-after": "120"}), retries=2)
    try:
        with pytest.raises(ServiceError):
            http.request("GET", API)
    finally:
        http.close()


def test_transport_errors_are_redacted(monkeypatch):
    waits = []
    monkeypatch.setattr("az_artifacts._http.time.sleep", waits.append)

    def handler(request):
        raise httpx.ConnectError(str(request.url), request=request)

    http = make_http(handler, retries=1)
    try:
        with pytest.raises(TransportError) as raised:
            http.request("GET", BLOB, authenticated=False)
    finally:
        http.close()
    assert "do-not-print" not in str(raised.value)
    assert waits == [1]


def test_signed_redirects():
    def handler(request):
        assert "authorization" not in request.headers
        if request.url.host == "blob.example":
            return httpx.Response(307, headers={"location": "https://cdn.example/file"})
        return httpx.Response(200, content=b"content")

    http = make_http(handler)
    try:
        assert http.request("GET", BLOB, authenticated=False).body == b"content"
    finally:
        http.close()


@pytest.mark.parametrize(
    "location",
    ["http://cdn.example/file", "https://user:secret@cdn.example/file"],
)
def test_invalid_redirect_is_rejected(location):
    http = make_http(lambda _: httpx.Response(302, headers={"location": location}))
    try:
        with pytest.raises(ProtocolError):
            http.request("GET", BLOB, authenticated=False)
    finally:
        http.close()


def test_authenticated_redirect_is_rejected():
    http = make_http(lambda _: httpx.Response(302, headers={"location": BLOB}))
    try:
        with pytest.raises(ProtocolError):
            http.request("GET", API)
    finally:
        http.close()


def test_response_size_limit():
    http = make_http(lambda _: httpx.Response(200, content=b"abcd"))
    try:
        with pytest.raises(ProtocolError, match="size limit"):
            http.request("GET", BLOB, authenticated=False, max_bytes=3)
    finally:
        http.close()


def test_malformed_http_content_encoding():
    http = make_http(
        lambda _: httpx.Response(200, content=b"not-gzip", headers={"content-encoding": "gzip"})
    )
    try:
        with pytest.raises(ProtocolError, match="content encoding"):
            http.request("GET", BLOB, authenticated=False)
    finally:
        http.close()


def test_url_control_characters():
    http = make_http(lambda _: pytest.fail("request must not be sent"))
    try:
        with pytest.raises(ProtocolError, match="control characters"):
            http.request("GET", "https://blob.example/\nfile", authenticated=False)
    finally:
        http.close()


def test_untrusted_authenticated_host():
    http = make_http(lambda _: pytest.fail("request must not be sent"))
    try:
        with pytest.raises(ProtocolError):
            http.request("GET", "https://example.com/data")
    finally:
        http.close()


@pytest.mark.parametrize("retry", [None, 0, 1, "false", [], {}])
def test_retry_option_requires_boolean(retry):
    http = make_http(lambda _: pytest.fail("request must not be sent"))
    try:
        with pytest.raises(TypeError, match="retry"):
            http.request("PUT", API, retry=retry)
    finally:
        http.close()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
@pytest.mark.parametrize("failure", [429, 500, 502, 503, 504, "transport"])
def test_operation_can_disable_all_retries(monkeypatch, method, failure):
    requests = []
    monkeypatch.setattr("az_artifacts._http.time.sleep", lambda _: pytest.fail("unexpected retry"))

    def handler(request):
        requests.append(request)
        if failure == "transport":
            raise httpx.ReadTimeout("private URL", request=request)
        return httpx.Response(failure, headers={"retry-after": "0"})

    http = make_http(handler, retries=4)
    try:
        with pytest.raises(TransportError if failure == "transport" else ServiceError):
            http.request(method, API, retry=False)
    finally:
        http.close()
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", API, None),
        ("POST", "https://vsblob.dev.azure.com/org/_apis/dedup/urls", ["ab" * 32 + "01"]),
    ],
)
@pytest.mark.parametrize("failure", [429, 500, 502, 503, 504, "transport"])
def test_default_read_retries_including_url_resolution(monkeypatch, method, url, body, failure):
    waits = []
    requests = []
    monkeypatch.setattr("az_artifacts._http.time.sleep", waits.append)

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            if failure == "transport":
                raise httpx.ReadError("private URL", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json={})

    http = make_http(handler, retries=1)
    try:
        assert http.request(method, url, json_body=body).status == 200
    finally:
        http.close()
    assert waits == [1]
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert all(request.method == method for request in requests)


@pytest.mark.parametrize("value", ["private token", "https://private/?sig=secret", "x" * 129, "é"])
def test_request_id_rejects_unsafe_or_unbounded_values(value):
    http = make_http(lambda _: httpx.Response(409, headers={"x-vss-e2eid": value.encode("utf-8")}))
    try:
        with pytest.raises(ConflictError) as caught:
            http.request("PUT", API)
    finally:
        http.close()
    assert caught.value.request_id is None


def test_request_id_uses_valid_secondary_header():
    http = make_http(
        lambda _: httpx.Response(
            409, headers={"x-vss-e2eid": "unsafe value", "x-ms-request-id": "safe-id"}
        )
    )
    try:
        with pytest.raises(ConflictError) as caught:
            http.request("PUT", API)
    finally:
        http.close()
    assert caught.value.request_id == "safe-id"


def test_no_replay_is_per_operation_not_a_persistent_setting(monkeypatch):
    waits = []
    monkeypatch.setattr("az_artifacts._http.time.sleep", waits.append)
    responses = iter([httpx.Response(503), httpx.Response(503), httpx.Response(200)])
    http = make_http(lambda _: next(responses), retries=1)
    try:
        with pytest.raises(ServiceError):
            http.request("PUT", API, retry=False)
        assert waits == []
        assert http.request("GET", API).status == 200
    finally:
        http.close()
    assert waits == [1]
