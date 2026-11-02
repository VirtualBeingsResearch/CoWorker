from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRoutingConfig(BaseModel):
    """Limits apply to a logical generation, not to nested SDK retries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempts_per_target: int = Field(default=2, ge=1, le=5)
    max_attempts: int = Field(default=4, ge=1, le=20)
    attempt_timeout_seconds: float = Field(default=90, gt=0, le=3600)
    request_timeout_seconds: float = Field(default=180, gt=0, le=7200)
    retry_delay_seconds: float = Field(default=1, ge=0, le=30)
    failure_threshold: int = Field(default=3, ge=1, le=100)
    failure_window_seconds: float = Field(default=60, gt=0, le=3600)
    cooldown_seconds: float = Field(default=60, gt=0, le=86400)
    max_cooldown_seconds: float = Field(default=900, gt=0, le=86400)
    rate_limit_cooldown_seconds: float = Field(default=30, gt=0, le=86400)
    quota_cooldown_seconds: float = Field(default=1800, gt=0, le=2592000)
    recovery_successes: int = Field(default=2, ge=1, le=10)
