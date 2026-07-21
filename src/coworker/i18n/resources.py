from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from coworker.i18n.runtime import SupportedLocale, get_locale, normalize_locale, tr

_FORMAT_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}|(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})"
)


@dataclass(frozen=True, slots=True)
class LocalizedMarkdown:
    fields: Mapping[str, str]
    body: str
    source: Path | None = None
    warning: str | None = None


def companion_candidates(
    original: str | Path,
    locale: str | SupportedLocale | None = None,
) -> tuple[Path, ...]:
    """Return exact locale, base language, then original, without duplicates."""

    path = Path(original)
    selected = get_locale() if locale is None else normalize_locale(locale)
    suffix = "".join(path.suffixes)
    stem = path.name[: -len(suffix)] if suffix else path.name
    tags = (selected.value, selected.value.split("-", 1)[0])
    candidates: list[Path] = []
    for tag in tags:
        candidate = path.with_name(f"{stem}.{tag}{suffix}")
        if candidate not in candidates:
            candidates.append(candidate)
    candidates.append(path)
    return tuple(candidates)


def resolve_localized_path(
    original: str | Path,
    locale: str | SupportedLocale | None = None,
) -> Path:
    for candidate in companion_candidates(original, locale):
        if candidate.is_file():
            return candidate
    return Path(original)


def read_localized_text(
    original: str | Path,
    locale: str | SupportedLocale | None = None,
) -> str:
    path = resolve_localized_path(original, locale)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _format_placeholders(text: str) -> frozenset[str]:
    return frozenset(first or second for first, second in _FORMAT_PLACEHOLDER_RE.findall(text))


def _warning(path: Path, reason: str) -> str:
    return tr("assets.companion_invalid", path=path, reason=reason)


def load_markdown_companion(
    original: str | Path,
    *,
    base_fields: Mapping[str, object],
    base_body: str,
    localizable_fields: Iterable[str],
    locale: str | SupportedLocale | None = None,
) -> LocalizedMarkdown:
    """Overlay localized prose while keeping operational metadata from the base file."""

    base_path = Path(original)
    selected = get_locale() if locale is None else normalize_locale(locale)
    companion = next(
        (
            candidate
            for candidate in companion_candidates(base_path, selected)[:-1]
            if candidate.is_file()
        ),
        None,
    )
    empty_fields = {field: str(base_fields.get(field, "") or "") for field in localizable_fields}
    if companion is None:
        return LocalizedMarkdown(fields=empty_fields, body=base_body)

    try:
        text = companion.read_text(encoding="utf-8")
        if not text.startswith("---"):
            raise ValueError(tr("assets.frontmatter_missing"))
        parts = text.split("---", 2)
        if len(parts) < 3:
            raise ValueError(tr("assets.frontmatter_incomplete"))
        parsed = yaml.safe_load(parts[1]) or {}
        if not isinstance(parsed, dict):
            raise ValueError(tr("assets.frontmatter_not_mapping"))
        localized_body = parts[2].strip()
        if _format_placeholders(localized_body) != _format_placeholders(base_body):
            raise ValueError(tr("assets.placeholder_mismatch"))
        fields: dict[str, str] = {}
        for field in localizable_fields:
            value = parsed.get(field, base_fields.get(field, ""))
            if not isinstance(value, str):
                raise ValueError(tr("assets.field_not_string", field=field))
            if _format_placeholders(value) != _format_placeholders(
                str(base_fields.get(field, "") or "")
            ):
                raise ValueError(tr("assets.field_placeholder_mismatch", field=field))
            fields[field] = value
        return LocalizedMarkdown(fields=fields, body=localized_body, source=companion)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        return LocalizedMarkdown(
            fields=empty_fields,
            body=base_body,
            warning=_warning(companion, str(exc)),
        )
