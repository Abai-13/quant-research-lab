"""Protected, generation-fenced credentials for local provider integrations."""

from .service import (
    CredentialError,
    CredentialManager,
    CredentialTokenSource,
    close_process_credentials,
    credential_generation,
    resolve_credential,
)

__all__ = [
    "CredentialError",
    "CredentialManager",
    "CredentialTokenSource",
    "close_process_credentials",
    "credential_generation",
    "resolve_credential",
]
