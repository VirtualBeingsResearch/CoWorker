"""In-memory extra communication tokens for the OpenAI channel."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from coworker.core.communication_tokens import validate_token_name
from coworker.relay.crypto import generate_communication_token

if TYPE_CHECKING:
    pass

PersistFn = Callable[[dict[str, str]], Awaitable[None]]


class ExtraTokenStore:
    """Short-name → secret map for OpenAI-channel extras (not the primary token)."""

    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self._tokens = dict(tokens or {})
        self._persist: PersistFn | None = None

    def set_persist(self, persist: PersistFn | None) -> None:
        self._persist = persist

    def snapshot(self) -> dict[str, str]:
        return dict(self._tokens)

    def names(self) -> list[str]:
        return sorted(self._tokens)

    def replace(self, tokens: dict[str, str]) -> None:
        cleaned: dict[str, str] = {}
        for name, secret in tokens.items():
            cleaned[validate_token_name(name)] = str(secret).strip()
        self._tokens = {name: secret for name, secret in cleaned.items() if secret}

    def has(self, name: str) -> bool:
        return name in self._tokens

    async def issue(self, name: str) -> str:
        cleaned = validate_token_name(name)
        if cleaned in self._tokens:
            raise ValueError(cleaned)
        secret = generate_communication_token()
        self._tokens[cleaned] = secret
        await self._flush()
        return secret

    async def rotate(self, name: str) -> str:
        cleaned = validate_token_name(name)
        if cleaned not in self._tokens:
            raise KeyError(cleaned)
        secret = generate_communication_token()
        self._tokens[cleaned] = secret
        await self._flush()
        return secret

    async def revoke(self, name: str) -> None:
        cleaned = validate_token_name(name)
        if cleaned not in self._tokens:
            raise KeyError(cleaned)
        del self._tokens[cleaned]
        await self._flush()

    async def _flush(self) -> None:
        if self._persist is not None:
            await self._persist(self.snapshot())
