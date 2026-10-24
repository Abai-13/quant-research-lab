"""Local model configuration and guarded Chat Completions gateway."""

from .core import (
    GatewayError,
    GatewayService,
    get_public_config,
    request_completion,
    save_config,
    test_config,
)

__all__ = [
    "GatewayError",
    "GatewayService",
    "get_public_config",
    "save_config",
    "test_config",
    "request_completion",
]
