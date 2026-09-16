"""HTTP transport with separate authenticated and signed-URL request paths."""

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .auth import Credential, authorization
from .errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ProtocolError,
    ServiceError,
    TransportError,
)

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_API_ACCEPT = "application/json; api-version=7.1-preview.1"


def validate_url(url: str, *, authenticated: bool = False) -> None:
    if any(ord(char) < 32 for char in url):
        raise ProtocolError("Service URLs must not contain control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ProtocolError("Invalid service URL") from None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ProtocolError("Service URLs must use HTTPS without credentials or fragments")
    if authenticated and not (
        host == "dev.azure.com"
        or host.endswith(".dev.azure.com")
        or host.endswith(".visualstudio.com")
    ):
        raise ProtocolError("Authenticated service URL is not an Azure DevOps Services URL")


def endpoint(base: str, *segments: str) -> str:
    return base.rstrip("/") + "/" + "/".join(quote(segment, safe="") for segment in segments)


@dataclass(frozen=True)
class Response:
    body: bytes
    headers: httpx.Headers
    status: int

    @property
    def request_id(self) -> str | None:
        for name in ("x-vss-e2eid", "x-ms-request-id"):
            value = self.headers.get(name)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
                return value
        return None

    def json(self) -> object:
        try:
            result: object = json.loads(self.body)
        except (ValueError, UnicodeError):
            raise ProtocolError("Service returned invalid JSON") from None
        return result


class Http:
    def __init__(
        self,
        credential: Credential,
        *,
        timeout: float,
        retries: int,
        transport: httpx.BaseTransport | None,
    ) -> None:
        self._credential = credential
        self._retries = retries
        self._api_client = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": "az-artifacts/0.1.0", "Accept-Encoding": "identity"},
        )
        self._blob_client = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": "az-artifacts/0.1.0", "Accept-Encoding": "identity"},
        )

    def close(self) -> None:
        self._api_client.close()
        self._blob_client.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool = True,
        params: dict[str, str] | None = None,
        json_body: object = None,
        headers: dict[str, str] | None = None,
        max_bytes: int = 16 * 1024 * 1024,
        retry: bool = True,
    ) -> Response:
        """Make a bounded request; retry=False forbids automatic failure replay."""
        if not isinstance(retry, bool):
            raise TypeError("retry must be a boolean")
        validate_url(url, authenticated=authenticated)
        retries = self._retries if retry else 0
        for attempt in range(retries + 1):
            request_headers = dict(headers or {})
            if authenticated:
                request_headers["Authorization"] = authorization(self._credential)
                request_headers.setdefault("Accept", _API_ACCEPT)
                request_headers["X-TFS-FedAuthRedirect"] = "Suppress"
            try:
                response = self._request_once(
                    method,
                    url,
                    authenticated=authenticated,
                    params=params,
                    json_body=json_body,
                    headers=request_headers,
                    max_bytes=max_bytes,
                )
            except httpx.DecodingError:
                raise ProtocolError("Unable to decode the HTTP response content encoding") from None
            except httpx.TransportError:
                if attempt == retries:
                    # HTTPX errors can include signed URL query strings.
                    raise TransportError("Unable to complete Azure Artifacts request") from None
                time.sleep(min(2**attempt, 30))
                continue
            if 200 <= response.status < 300:
                return response
            if response.status in _RETRY_STATUSES and attempt < retries:
                delay = self._retry_delay(response, attempt)
                if delay <= 30:
                    time.sleep(delay)
                    continue
            self._raise_service_error(response)
        raise AssertionError("Request retry loop exhausted without a result")

    def _request_once(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool,
        params: dict[str, str] | None,
        json_body: object,
        headers: dict[str, str],
        max_bytes: int,
    ) -> Response:
        for redirect in range(6):
            validate_url(url, authenticated=authenticated)
            client = self._api_client if authenticated else self._blob_client
            with client.stream(
                method, url, params=params, json=json_body, headers=headers
            ) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    if authenticated:
                        raise ProtocolError(
                            "Authenticated service unexpectedly redirected a request"
                        )
                    location = response.headers.get("location")
                    if not location or redirect == 5:
                        raise ProtocolError("Invalid or excessive blob download redirects")
                    url = urljoin(str(response.url), location)
                    params = None
                    continue
                body = bytearray()
                if 200 <= response.status_code < 300:
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        if len(body) + len(chunk) > max_bytes:
                            raise ProtocolError(
                                "Service response exceeds the configured size limit"
                            )
                        body.extend(chunk)
                return Response(bytes(body), response.headers, response.status_code)
        raise AssertionError("Redirect loop exhausted without a result")

    @staticmethod
    def _retry_delay(response: Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            if retry_after.isdigit():
                return float(retry_after)
            try:
                date = parsedate_to_datetime(retry_after)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                return max(0, (date - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
        return float(min(2**attempt, 30))

    @staticmethod
    def _raise_service_error(response: Response) -> None:
        error = {
            401: AuthenticationError,
            403: PermissionDeniedError,
            404: NotFoundError,
            409: ConflictError,
        }.get(response.status, ServiceError)
        raise error(
            f"Azure Artifacts request failed (HTTP {response.status})",
            status_code=response.status,
            request_id=response.request_id,
        )
