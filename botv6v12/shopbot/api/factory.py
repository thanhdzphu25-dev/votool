"""Factory helpers for external API providers.

The Telegram handler should resolve a product's provider name through this layer
instead of hard-coding provider-specific HTTP calls.
"""

from __future__ import annotations

from typing import Any


class UnsupportedApiProvider(ValueError):
    """Raised when a product references an unknown API provider."""


def get_api_provider(provider: str, providers: dict[str, Any]) -> Any:
    key = str(provider or "").upper()
    if key in providers:
        return providers[key]
    raise UnsupportedApiProvider("Unsupported API provider")
