from __future__ import annotations

import re
import tomllib
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from importlib import resources
from types import MappingProxyType
from typing import Any


class SupportedLocale(StrEnum):
    ZH_CN = "zh-CN"
    EN = "en"


_ALIASES = {
    "zh": SupportedLocale.ZH_CN,
    "zh-cn": SupportedLocale.ZH_CN,
    "zh-hans": SupportedLocale.ZH_CN,
    "zh-hans-cn": SupportedLocale.ZH_CN,
    "cn": SupportedLocale.ZH_CN,
    "en": SupportedLocale.EN,
    "en-us": SupportedLocale.EN,
    "en-gb": SupportedLocale.EN,
}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_catalog_cache: dict[SupportedLocale, Mapping[str, str]] = {}
_default_locale = SupportedLocale.ZH_CN
_locale_context: ContextVar[SupportedLocale | None] = ContextVar(
    "coworker_locale",
    default=None,
)


def normalize_locale(value: str | SupportedLocale) -> SupportedLocale:
    """Normalize a supported locale alias or fail closed for unknown values."""

    if isinstance(value, SupportedLocale):
        return value
    normalized = str(value).strip().replace("_", "-").lower()
    try:
        return _ALIASES[normalized]
    except KeyError as exc:
        supported = ", ".join(locale.value for locale in SupportedLocale)
        raise ValueError(f"Unsupported locale {value!r}; expected one of: {supported}") from exc


def get_locale() -> SupportedLocale:
    return _locale_context.get() or _default_locale


def capture_locale() -> SupportedLocale:
    """Capture the locale that a newly created background task must retain."""

    return get_locale()


def bind_locale[T](
    operation: Callable[[], Awaitable[T]],
    locale: str | SupportedLocale | None = None,
) -> Coroutine[Any, Any, T]:
    """Wrap background work with the locale captured at task creation time."""

    captured = capture_locale() if locale is None else normalize_locale(locale)

    async def run() -> T:
        with locale_context(captured):
            return await operation()

    return run()


@contextmanager
def locale_context(locale: str | SupportedLocale) -> Iterator[SupportedLocale]:
    """Temporarily bind a locale using ContextVar (safe across async tasks)."""

    normalized = normalize_locale(locale)
    token = _locale_context.set(normalized)
    try:
        yield normalized
    finally:
        _locale_context.reset(token)


def configure_locale(locale: str | SupportedLocale) -> SupportedLocale:
    """Set the instance default after validating every built-in catalog."""

    global _default_locale
    normalized = normalize_locale(locale)
    validate_catalogs()
    _default_locale = normalized
    return normalized


def locale_language(locale: str | SupportedLocale | None = None) -> str:
    normalized = get_locale() if locale is None else normalize_locale(locale)
    return normalized.value.split("-", 1)[0]


def browser_locale(locale: str | SupportedLocale | None = None) -> str:
    normalized = get_locale() if locale is None else normalize_locale(locale)
    return "zh-CN" if normalized is SupportedLocale.ZH_CN else "en-US"


def _flatten_catalog(value: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    flattened: dict[str, str] = {}
    for name, item in value.items():
        key = f"{prefix}.{name}" if prefix else name
        if isinstance(item, dict):
            flattened.update(_flatten_catalog(item, key))
        elif isinstance(item, str):
            if not item.strip():
                raise ValueError(f"Catalog value {key!r} must not be empty")
            flattened[key] = item
        else:
            raise ValueError(f"Catalog value {key!r} must be a string")
    return flattened


def _load_catalog(locale: SupportedLocale) -> Mapping[str, str]:
    cached = _catalog_cache.get(locale)
    if cached is not None:
        return cached

    catalog_dir = resources.files("coworker.i18n.catalogs").joinpath(locale.value)
    merged: dict[str, str] = {}
    files = sorted(
        (entry for entry in catalog_dir.iterdir() if entry.name.endswith(".toml")),
        key=lambda entry: entry.name,
    )
    if not files:
        raise ValueError(f"No built-in catalog files found for locale {locale.value}")
    for file in files:
        try:
            parsed = tomllib.loads(file.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"Invalid catalog {locale.value}/{file.name}: {exc}") from exc
        for key, value in _flatten_catalog(parsed).items():
            if key in merged:
                raise ValueError(
                    f"Duplicate catalog key {key!r} in locale {locale.value} ({file.name})"
                )
            merged[key] = value
    frozen = MappingProxyType(merged)
    _catalog_cache[locale] = frozen
    return frozen


def catalog(locale: str | SupportedLocale | None = None) -> Mapping[str, str]:
    normalized = get_locale() if locale is None else normalize_locale(locale)
    return _load_catalog(normalized)


def placeholders(template: str) -> frozenset[str]:
    return frozenset(_PLACEHOLDER_RE.findall(template))


def validate_catalogs() -> None:
    """Validate key and placeholder parity for every built-in locale."""

    reference_locale = SupportedLocale.ZH_CN
    reference = _load_catalog(reference_locale)
    for locale in SupportedLocale:
        candidate = _load_catalog(locale)
        missing = sorted(reference.keys() - candidate.keys())
        extra = sorted(candidate.keys() - reference.keys())
        if missing or extra:
            raise ValueError(
                f"Catalog key mismatch for {locale.value}: missing={missing}, extra={extra}"
            )
        for key, reference_text in reference.items():
            expected = placeholders(reference_text)
            actual = placeholders(candidate[key])
            if expected != actual:
                raise ValueError(
                    f"Catalog placeholder mismatch for {locale.value}:{key}: "
                    f"expected={sorted(expected)}, actual={sorted(actual)}"
                )


def tr(key: str, /, **values: object) -> str:
    """Render a catalog entry without ever exposing a missing key to the model."""

    entries = _load_catalog(get_locale())
    try:
        template = entries[key]
    except KeyError as exc:
        raise KeyError(f"Missing i18n catalog entry: {key}") from exc
    required = placeholders(template)
    missing = required - values.keys()
    if missing:
        raise ValueError(f"Missing values for {key}: {sorted(missing)}")

    def replace(match: re.Match[str]) -> str:
        return str(values[match.group(1)])

    return _PLACEHOLDER_RE.sub(replace, template)
