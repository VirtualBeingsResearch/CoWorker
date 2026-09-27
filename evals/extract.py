"""Pull a scorable answer out of a free-form reply."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Extraction:
    value: str
    failed: bool
    detail: str


_CHOICE = re.compile(r"\b([A-D])\b", re.IGNORECASE)
_FENCED = re.compile(r"```(?:[a-zA-Z0-9_+-]+)?\n(.*?)```", re.DOTALL)


def extract_choice(text: str) -> Extraction:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return Extraction("", True, "empty reply")
    for line in reversed(lines[-4:]):
        match = _CHOICE.search(line)
        if match:
            return Extraction(match.group(1).upper(), False, f"from {line!r}")
    match = _CHOICE.search(text)
    if match:
        return Extraction(match.group(1).upper(), False, "from body")
    return Extraction("", True, "no A-D choice found")


def extract_exact(text: str) -> Extraction:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return Extraction("", True, "empty reply")
    return Extraction(lines[-1], False, "last non-empty line")


def extract_code(text: str) -> Extraction:
    blocks = _FENCED.findall(text)
    if blocks:
        return Extraction(blocks[-1].strip(), False, "fenced block")
    if not text.strip():
        return Extraction("", True, "empty reply")
    return Extraction(text.strip(), False, "whole reply")
