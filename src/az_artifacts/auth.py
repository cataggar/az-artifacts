"""Explicit credentials; no dependency on Azure CLI configuration."""

from base64 import b64encode
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias

ADO_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"


class AccessToken(Protocol):
    @property
    def token(self) -> str: ...


class TokenCredential(Protocol):
    def get_token(self, *scopes: str) -> AccessToken: ...


@dataclass(frozen=True)
class BearerToken:
    """An already-acquired OAuth token, for example a pipeline's System.AccessToken."""

    token: str = field(repr=False)

    def __post_init__(self) -> None:
        _validate_token(self.token)


Credential: TypeAlias = str | BearerToken | TokenCredential


def _validate_token(token: str) -> None:
    if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
        raise ValueError("A nonempty credential without line breaks is required")


def authorization(credential: Credential) -> str:
    if isinstance(credential, str):
        _validate_token(credential)
        return "Basic " + b64encode(f":{credential}".encode()).decode("ascii")
    token = (
        credential.token
        if isinstance(credential, BearerToken)
        else credential.get_token(ADO_SCOPE).token
    )
    _validate_token(token)
    return "Bearer " + token
