"""Errors raised by the Universal Package client."""


class ArtifactsError(Exception):
    """Base class for library errors."""


class ServiceError(ArtifactsError):
    """An Azure DevOps or blob service returned an unsuccessful response."""

    def __init__(self, message: str, *, status_code: int, request_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class AuthenticationError(ServiceError):
    """The credential was not accepted."""


class PermissionDeniedError(ServiceError):
    """The credential does not have access to the requested resource."""


class NotFoundError(ServiceError):
    """The requested resource does not exist."""


class ConflictError(ServiceError):
    """The service confirmed HTTP 409; registration did not overwrite the version."""


class RegistrationOutcomeUnknownError(ArtifactsError):
    """Registration was not synchronously acknowledged; it may have committed.

    Never automatically replay this operation or treat a later conflict as success.
    Reconciliation requires explicitly checking the intended metadata. Optional
    ``status_code`` and sanitized ``request_id`` retain available response context;
    neither is available for transport or response-decoding failures.
    """

    def __init__(
        self, message: str, *, status_code: int | None = None, request_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id


class TransportError(ArtifactsError):
    """A request could not be completed."""


class ProtocolError(ArtifactsError):
    """A service response or blob does not match the supported protocol."""


class IntegrityError(ProtocolError):
    """Downloaded content does not match its advertised hash or size."""


class LocalFileChangedError(ArtifactsError):
    """The local source observably changed, was replaced, or disappeared during comparison.

    Checks are best-effort, not an atomic snapshot or protection against concurrent
    writers restoring file metadata. Other local I/O errors remain OSError.
    """


class UnsafePathError(ArtifactsError):
    """A package path or filesystem entry is unsafe for the requested operation."""


class NoMatchingFilesError(ArtifactsError):
    """The file filter did not match any manifest entries."""


class VersionNotFoundError(ArtifactsError):
    """No released version matches the requested version pattern."""


class PackageNotFoundError(ArtifactsError):
    """Successful catalog enumeration found no visible package with the exact name."""


class PackageConflictError(ConflictError):
    """The immutable package version already exists; it was not overwritten."""


class IncompleteUploadError(ArtifactsError):
    """Content or retention was not completed; registration was not attempted."""


class AmbiguousPublishError(ArtifactsError):
    """Registration was attempted, but its outcome could not be confirmed."""
