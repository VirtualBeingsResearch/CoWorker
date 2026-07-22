"""Desktop inbound: build a typed envelope and render it to final text.

Replaces the old ``json.dumps`` (in ``routes.py``) -> ``json.loads`` (in the
dispatcher interceptor) round-trip: the caller builds the envelope dict and
passes it here, where it is validated into a :class:`DesktopEnvelope`
structurally and rendered via :meth:`DesktopDispatcher.route`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from coworker.channels.desktop.dispatcher import DesktopEnvelope

if TYPE_CHECKING:
    from coworker.channels.desktop.dispatcher import DesktopDispatcher


def render(
    envelope: dict[str, Any],
    participant_id: str,
    dispatcher: DesktopDispatcher,
) -> str | None:
    """Render a desktop envelope dict to final text, or ``None`` to consume.

    Malformed envelopes are consumed (``None``) so they never leak into the
    agent's inbox as raw JSON.
    """
    try:
        parsed = DesktopEnvelope.model_validate(envelope)
    except ValidationError:
        return None
    return dispatcher.route(parsed, participant_id)
