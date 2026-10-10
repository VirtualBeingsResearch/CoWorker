"""Search backends used by the search_web tool."""

from coworker.web_search.service import (
    SearchError,
    configured_providers,
    provider_choices,
    resolve_provider,
    run_search,
)

__all__ = [
    "SearchError",
    "configured_providers",
    "provider_choices",
    "resolve_provider",
    "run_search",
]
