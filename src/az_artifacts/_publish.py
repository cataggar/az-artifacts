"""Immutable package registration, with read-only ambiguous-outcome reconciliation."""

import base64
from dataclasses import replace

from . import _json
from ._http import Http
from ._prepare import PreparedPackage
from ._upload import Uploader
from .errors import (
    AmbiguousPublishError,
    AuthenticationError,
    IncompleteUploadError,
    NotFoundError,
    PackageConflictError,
    PermissionDeniedError,
    ProtocolError,
    ServiceError,
    TransportError,
)
from .models import PackagePushMetadata, PublishRequest, PublishResult


def publish(
    http: Http,
    package_url: str,
    blob_url: str,
    request: PublishRequest,
    prepared: PreparedPackage,
    *,
    max_workers: int,
) -> PublishResult:
    try:
        http.request("GET", package_url, params={"intent": "FetchMetadataOnly"})
    except NotFoundError:
        pass
    else:
        raise PackageConflictError("The immutable package version already exists", status_code=409)
    try:
        uploaded = Uploader(http, blob_url, prepared, max_workers=max_workers).upload()
    except (AuthenticationError, PermissionDeniedError, IncompleteUploadError):
        raise
    except (ProtocolError, ServiceError, TransportError) as error:
        raise IncompleteUploadError(
            "Content upload or retention failed; registration was not attempted"
        ) from error
    prepared.verify_sources()
    description = request.description if request.description is not None else ""
    try:
        http.request(
            "PUT",
            package_url,
            params={"api-version": "7.1-preview.1"},
            json_body=_json.serialize_package_push_metadata(
                PackagePushMetadata(
                    manifest_id=prepared.metadata.manifest_id,
                    super_root_id=prepared.metadata.super_root_id,
                    description=description,
                    proof_nodes=tuple(
                        base64.b64encode(node).decode("ascii") for node in prepared.proofs
                    ),
                )
            ),
            retry=False,
        )
    except ServiceError as error:
        if error.status_code == 409:
            raise PackageConflictError(
                "The immutable package version already exists",
                status_code=409,
                request_id=error.request_id,
            ) from None
        if error.status_code not in (408, 429) and error.status_code < 500:
            raise
    except (TransportError, ProtocolError):
        pass
    try:
        response = http.request("GET", package_url, params={"intent": "FetchMetadataOnly"})
        obj = _json.as_object(response.json(), "registered package")
        metadata = _json.package_metadata(obj)
        if metadata.version != prepared.metadata.version:
            raise ProtocolError("Registration readback returned another package version")
        returned_description = metadata.description or ""
    except Exception as error:
        # Credential providers can fail outside the library's exception hierarchy.
        raise AmbiguousPublishError(
            "Registration was attempted but could not be confirmed; do not blindly retry"
        ) from error
    if (
        replace(metadata, description=None) != prepared.metadata
        or returned_description != description
    ):
        raise PackageConflictError(
            "The immutable version exists with different content or description", status_code=409
        )
    return PublishResult(
        metadata,
        prepared.path,
        tuple(source.path.relative_to(prepared.path) for source in prepared.sources),
        uploaded,
    )
