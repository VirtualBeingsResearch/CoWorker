from __future__ import annotations

from typing import Any


async def fetch_openai_model_ids(client: Any) -> list[str]:
    """Walk every page of an OpenAI-compatible ``GET /models`` response."""
    page = await client.models.list()
    ids: list[str] = []
    while True:
        for model in getattr(page, "data", []) or []:
            model_id = getattr(model, "id", None)
            if isinstance(model_id, str) and model_id:
                ids.append(model_id)
        has_next = getattr(page, "has_next_page", None)
        if not callable(has_next) or not has_next():
            break
        get_next = getattr(page, "get_next_page", None)
        if not callable(get_next):
            break
        page = await get_next()
    return sorted(set(ids))


async def fetch_anthropic_model_ids(client: Any) -> list[str]:
    """Walk every page of Anthropic's paginated ``GET /v1/models`` response."""
    page = await client.models.list(limit=1000)
    ids: list[str] = []
    while True:
        for model in getattr(page, "data", []) or []:
            model_id = getattr(model, "id", None)
            if isinstance(model_id, str) and model_id:
                ids.append(model_id)
        has_next = getattr(page, "has_next_page", None)
        if not callable(has_next) or not has_next():
            break
        get_next = getattr(page, "get_next_page", None)
        if not callable(get_next):
            break
        page = await get_next()
    return sorted(set(ids))
