"""LLM judge for open-ended ability items. Never scores the same vendor it judges."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from evals.llm import complete_text, vendor_of
from evals.workspace import ModelTarget

PASS_MARKERS = ("PASS", "通过")
FAIL_MARKERS = ("FAIL", "不通过")


@dataclass(frozen=True)
class JudgeVerdict:
    passed: bool
    reason: str
    raw: str


def ensure_different_vendor(subject: ModelTarget, judge: ModelTarget) -> None:
    if vendor_of(subject.provider) == vendor_of(judge.provider):
        raise ValueError(
            f"judge {judge.provider!r} is the same vendor as the subject "
            f"{subject.provider!r}; pick a judge from another firm"
        )


def _parse_verdict(text: str) -> JudgeVerdict:
    upper = text.upper()
    passed = any(marker in text or marker in upper for marker in PASS_MARKERS)
    failed = any(marker in text or marker in upper for marker in FAIL_MARKERS)
    if passed == failed:
        match = re.search(r"\b(PASS|FAIL|通过|不通过)\b", text)
        if match:
            passed = match.group(1) in PASS_MARKERS
            failed = not passed
    if passed == failed:
        return JudgeVerdict(False, "judge did not return a clear PASS/FAIL", text)
    return JudgeVerdict(passed, text.strip()[:400], text)


async def judge_text(
    judge: ModelTarget,
    *,
    rubric: str,
    evidence: str,
    locale: str,
) -> JudgeVerdict:
    language = "Chinese" if locale.startswith("zh") else "English"
    prompt = (
        f"You are checking whether an agent met a written rubric. Reply in {language}.\n"
        "First line must be exactly PASS or FAIL. Then one short reason.\n\n"
        f"Rubric:\n{rubric}\n\nEvidence:\n{evidence}\n"
    )
    raw = await complete_text(judge, prompt)
    return _parse_verdict(raw)


def format_evidence(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
