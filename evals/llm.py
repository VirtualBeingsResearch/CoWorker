"""Call a built-in provider the way a raw model call would, without starting Coworker."""

from __future__ import annotations

from coworker.brain.factory import build_provider, resolve_base_url
from coworker.core.types import Message
from evals.workspace import ModelTarget

_VENDOR_ALIASES = {
    "opencode": "opencode",
    "opencode_go": "opencode",
    "openai": "openai",
    "openai_compatible": "openai",
}


def vendor_of(provider: str) -> str:
    """Coarse vendor id used to keep the judge off the same firm as the subject."""
    key = provider.strip().lower().replace("-", "_")
    if key in _VENDOR_ALIASES:
        return _VENDOR_ALIASES[key]
    return key.split("_")[0]


def credential_key(provider: str) -> str:
    return f"LLM__{provider.replace('-', '_').upper()}_API_KEY"


def base_url_key(provider: str) -> str:
    return f"LLM__{provider.replace('-', '_').upper()}_BASE_URL"


def api_key_for(target: ModelTarget) -> str:
    return target.api_keys.get(credential_key(target.provider), "")


def base_url_for(target: ModelTarget) -> str:
    return target.base_urls.get(base_url_key(target.provider), "")


async def complete_text(target: ModelTarget, prompt: str, *, system: str = "") -> str:
    key = api_key_for(target)
    if not key:
        raise RuntimeError(f"missing {credential_key(target.provider)}")
    provider = build_provider(
        target.provider,
        key,
        base_url=resolve_base_url(target.provider, base_url_for(target) or None),
        name=target.provider,
        default_model=target.model,
    )
    provider.set_model(target.model)
    response = await provider.complete(
        [Message(role="user", content=prompt)],
        system,
        [],
        thinking=False,
    )
    return str(response.content or "").strip()
