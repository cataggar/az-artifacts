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
    """A logical package path is invalid or cannot safely be written locally."""


class NoMatchingFilesError(ArtifactsError):
    """The file filter did not match any manifest entries."""


class VersionNotFoundError(ArtifactsError):
    """No released version matches the requested version pattern."""


class PackageNotFoundError(ArtifactsError):
    """Successful catalog enumeration found no visible package with the exact name."""
