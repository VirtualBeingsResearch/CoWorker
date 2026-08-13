from __future__ import annotations

import re
from collections.abc import Mapping

SYSTEM_PROMPT_VARIABLES = (
    "IDENTITY",
    "ENVIRONMENT",
    "INSTINCTS",
    "GUIDELINES",
    "LANGUAGE_POLICY",
    "THINKING",
    "CHANNELS",
    "SKILLS",
    "PALACES",
)
MAX_SYSTEM_PROMPT_TEMPLATE_CHARS = 100_000
DEFAULT_SYSTEM_PROMPT_TEMPLATE = "\n\n".join(
    f"{{{{{name}}}}}" for name in SYSTEM_PROMPT_VARIABLES
)

_PLACEHOLDER_RE = re.compile(r"(?<!\\)\{\{([^{}\n]*)\}\}")
_STANDALONE_PLACEHOLDER_RE = re.compile(r"^\s*\{\{([^{}\n]*)\}\}\s*$")
_ESCAPED_PLACEHOLDER_RE = re.compile(r"\\(\{\{[A-Za-z][A-Za-z0-9_]*\}\})")


class SystemPromptTemplateError(ValueError):
    def __init__(self, code: str, *, variable: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.variable = variable


def validate_system_prompt_template(template: str) -> str:
    if len(template) > MAX_SYSTEM_PROMPT_TEMPLATE_CHARS:
        raise SystemPromptTemplateError("too_long")
    if not template.strip():
        return ""

    seen: set[str] = set()
    for match in _PLACEHOLDER_RE.finditer(template):
        variable = match.group(1)
        if variable not in SYSTEM_PROMPT_VARIABLES:
            raise SystemPromptTemplateError("unknown_variable", variable=variable)

        line_start = template.rfind("\n", 0, match.start()) + 1
        line_end = template.find("\n", match.end())
        if line_end < 0:
            line_end = len(template)
        if template[line_start:match.start()].strip() or template[match.end():line_end].strip():
            raise SystemPromptTemplateError("standalone_variable", variable=variable)
        if variable in seen:
            raise SystemPromptTemplateError("duplicate_variable", variable=variable)
        seen.add(variable)

    return template


def resolve_system_prompt_template(template: str) -> str:
    validated = validate_system_prompt_template(template)
    return validated or DEFAULT_SYSTEM_PROMPT_TEMPLATE


def render_system_prompt_template(
    template: str,
    sections: Mapping[str, str],
) -> str:
    resolved = resolve_system_prompt_template(template)
    output: list[str] = []
    suppress_next_blank = False

    for line in resolved.splitlines():
        placeholder = _STANDALONE_PLACEHOLDER_RE.fullmatch(line)
        if placeholder is not None:
            variable = placeholder.group(1)
            section = sections.get(variable, "").strip()
            if section:
                output.extend(section.splitlines())
                suppress_next_blank = False
            else:
                suppress_next_blank = True
            continue

        if suppress_next_blank and not line.strip():
            suppress_next_blank = False
            continue
        suppress_next_blank = False
        output.append(_ESCAPED_PLACEHOLDER_RE.sub(r"\1", line))

    while output and not output[0].strip():
        output.pop(0)
    while output and not output[-1].strip():
        output.pop()
    return "\n".join(output) + "\n"
