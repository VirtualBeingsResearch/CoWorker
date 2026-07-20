from __future__ import annotations

import secrets

_DEFAULT_ENTROPY_BYTES = 9
_MIN_ENTROPY_BYTES = 6


def new_compact_id(prefix: str = "", *, entropy_bytes: int = _DEFAULT_ENTROPY_BYTES) -> str:
    """Return a URL-safe random ID intended for model-visible local references.

    Nine random bytes encode to exactly 12 base64url characters without padding.
    Callers with an explicit collision check may use six bytes (eight characters).
    """
    if entropy_bytes < _MIN_ENTROPY_BYTES:
        raise ValueError(f"entropy_bytes must be at least {_MIN_ENTROPY_BYTES}")
    return f"{prefix}{secrets.token_urlsafe(entropy_bytes)}"
