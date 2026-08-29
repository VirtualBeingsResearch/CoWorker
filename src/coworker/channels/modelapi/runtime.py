"""Mutable model API runtime state shared by the channel, module, and endpoints."""

from __future__ import annotations

from pathlib import Path

from coworker.channels.modelapi.scenarios import ScenarioStore
from coworker.channels.modelapi.sessions import ModelApiTokenDirectory, SessionMatcher
from coworker.channels.modelapi.turns import TurnRegistry
from coworker.core.config import ModelApiConfig


class ModelApiRuntime:
    """Owns the live model API state and applies admin hot-config changes.

    The channel registration itself is static for the process lifetime; this
    object is what changes when an administrator edits ``model_api`` settings,
    so enabling the API, rotating tokens, or tuning the lifecycle never
    requires a restart.
    """

    name = "model-api"

    def __init__(self, config: ModelApiConfig, sessions_path: str | Path) -> None:
        self.sessions = SessionMatcher(sessions_path)
        self.turns = TurnRegistry(
            nudge_seconds=float(config.nudge_seconds),
            timeout_seconds=float(config.timeout_seconds),
        )
        self.directory = ModelApiTokenDirectory(config.tokens)
        self.enabled = config.enabled
        self.scenarios = ScenarioStore(Path(sessions_path).parent / "scenarios")

    @property
    def available(self) -> bool:
        """True when requests should be served."""
        return self.enabled and len(self.directory) > 0

    def reconfigure(self, config: ModelApiConfig) -> None:
        self.turns.nudge_seconds = float(config.nudge_seconds)
        self.turns.timeout_seconds = float(config.timeout_seconds)
        self.directory.reconfigure(config.tokens)
        self.enabled = config.enabled
