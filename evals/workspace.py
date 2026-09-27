"""Prepare an isolated working directory and environment for one sample."""

from __future__ import annotations

import os
import re
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path

from coworker.core.config import LLMConfig
from evals.scenario import Scenario

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Coworker reads these prefixes from the environment; the caller's own values must
# never leak into an experimental instance.
_COWORKER_ENV_PREFIXES = (
    "AGENT__",
    "LLM__",
    "MEMORY__",
    "API__",
    "I18N__",
    "RELAY__",
    "DESKTOP_UPDATES__",
    "ADMIN__",
    "WECOM__",
    "WEIXIN__",
    "TELEGRAM__",
    "CHANNEL_ACCESS",
)
_KEY_RE = re.compile(r"^LLM__[A-Z0-9_]+_API_KEY$")
_BASE_URL_RE = re.compile(r"^LLM__[A-Z0-9_]+_BASE_URL$")

# Quiet, offline-friendly defaults; scenario ``config`` overrides them.
BASE_ENV = {
    "AGENT__PASSIVE_MODE": "true",
    "AGENT__INBOX_POLL_INTERVAL": "0.5",
    "AGENT__SUBCONSCIOUS_THINKING": "false",
    "MEMORY__BACKEND": "file",
    "RELAY__ENABLED": "false",
    "DESKTOP_UPDATES__SYNC_ON_START": "false",
    "MEM0_TELEMETRY": "false",
    "ANONYMIZED_TELEMETRY": "false",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}


@dataclass(frozen=True)
class ModelTarget:
    provider: str
    model: str
    providers_file: Path | None = None
    api_keys: dict[str, str] = field(default_factory=dict, repr=False)
    base_urls: dict[str, str] = field(default_factory=dict)


def _read_env(pattern: re.Pattern[str], env_file: Path | None) -> dict[str, str]:
    values = {k: v for k, v in os.environ.items() if pattern.match(k) and v.strip()}
    if env_file is not None:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip().removeprefix("export ").strip()
            value = value.strip().strip("'\"")
            if pattern.match(name) and value:
                values[name] = value
    return values


def read_api_keys(env_file: Path | None) -> dict[str, str]:
    """Collect ``LLM__*_API_KEY`` values from the environment and an optional env file."""
    return _read_env(_KEY_RE, env_file)


def read_base_urls(env_file: Path | None) -> dict[str, str]:
    """Collect ``LLM__*_BASE_URL`` values from the environment and an optional env file."""
    return _read_env(_BASE_URL_RE, env_file)


def base_url_variable(provider: str) -> str | None:
    """The ``LLM__<PROVIDER>_BASE_URL`` variable of a built-in provider, if it has one."""
    field_name = f"{provider.replace('-', '_')}_base_url"
    if field_name not in LLMConfig.model_fields:
        return None
    return f"LLM__{field_name.upper()}"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def prepare(workspace: Path, scenario: Scenario, locale: str, target: ModelTarget) -> None:
    workspace.mkdir(parents=True, exist_ok=False)
    identity_src = FIXTURES / "identity" / locale
    shutil.copytree(identity_src, workspace / "data" / "identity")
    subconscious = REPO_ROOT / ".coworker" / "subconscious"
    if subconscious.is_dir():
        shutil.copytree(subconscious, workspace / ".coworker" / "subconscious")
    for relative, content in scenario.files_for(locale).items():
        path = (workspace / relative).resolve()
        if workspace.resolve() not in path.parents:
            raise ValueError(f"scenario file escapes the workspace: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if target.providers_file is not None:
        shutil.copyfile(target.providers_file, workspace / "providers.json")


def child_env(
    scenario: Scenario, locale: str, target: ModelTarget, port: int, token: str
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(_COWORKER_ENV_PREFIXES)
    }
    env.update(BASE_ENV)
    env.update(target.api_keys)
    env.update(target.base_urls)
    env.update(
        {
            "LLM__DEFAULT_PROVIDER": target.provider,
            "LLM__DEFAULT_MODEL": target.model,
            "LLM__PROVIDERS_FILE": "providers.json",
            "I18N__LOCALE": locale,
            "API__HOST": "127.0.0.1",
            "API__PORT": str(port),
        }
    )
    env.update(scenario.env_overrides())
    env["API__COMMUNICATION_TOKEN"] = token
    return env
