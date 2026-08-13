from __future__ import annotations

import pytest
from pydantic import ValidationError

from coworker.core.config import AgentConfig
from coworker.i18n import locale_context
from coworker.prompts.template import (
    DEFAULT_SYSTEM_PROMPT_TEMPLATE,
    MAX_SYSTEM_PROMPT_TEMPLATE_CHARS,
    SYSTEM_PROMPT_VARIABLES,
    SystemPromptTemplateError,
    render_system_prompt_template,
    resolve_system_prompt_template,
    validate_system_prompt_template,
)


def test_default_template_lists_every_variable_once() -> None:
    assert DEFAULT_SYSTEM_PROMPT_TEMPLATE == "\n\n".join(
        f"{{{{{name}}}}}" for name in SYSTEM_PROMPT_VARIABLES
    )
    assert resolve_system_prompt_template("") == DEFAULT_SYSTEM_PROMPT_TEMPLATE
    assert resolve_system_prompt_template("  \n") == DEFAULT_SYSTEM_PROMPT_TEMPLATE


@pytest.mark.parametrize(
    ("template", "code", "variable"),
    [
        ("{{UNKNOWN}}", "unknown_variable", "UNKNOWN"),
        ("{{IDENTITY}}\n{{IDENTITY}}", "duplicate_variable", "IDENTITY"),
        ("prefix {{IDENTITY}}", "standalone_variable", "IDENTITY"),
    ],
)
def test_template_validation_rejects_invalid_variables(
    template: str,
    code: str,
    variable: str,
) -> None:
    with pytest.raises(SystemPromptTemplateError) as captured:
        validate_system_prompt_template(template)

    assert captured.value.code == code
    assert captured.value.variable == variable


def test_escaped_placeholder_is_literal_text() -> None:
    template = "[EXAMPLE]\n" + r"\{{UNKNOWN}}"

    assert validate_system_prompt_template(template) == template
    assert render_system_prompt_template(template, {}) == "[EXAMPLE]\n{{UNKNOWN}}\n"


def test_agent_config_rejects_too_long_template_in_both_locales() -> None:
    template = "x" * (MAX_SYSTEM_PROMPT_TEMPLATE_CHARS + 1)

    with locale_context("zh-CN"), pytest.raises(ValidationError, match="不能超过"):
        AgentConfig(system_prompt_template=template, _env_file=None)
    with locale_context("en"), pytest.raises(ValidationError, match="must not exceed"):
        AgentConfig(system_prompt_template=template, _env_file=None)


def test_agent_config_reads_template_from_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "AGENT__SYSTEM_PROMPT_TEMPLATE",
        "{{IDENTITY}}\n\n[CUSTOM]\nEnvironment rule",
    )

    config = AgentConfig(_env_file=None)

    assert config.system_prompt_template.endswith("Environment rule")
