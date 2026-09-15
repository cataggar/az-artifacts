from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from az_artifacts._http import Http
from az_artifacts.auth import ADO_SCOPE, BearerToken
from az_artifacts.errors import (
    AuthenticationError,
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
