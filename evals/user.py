"""A simulated participant who keeps talking until the goal is met or the turn cap."""

from __future__ import annotations

from dataclasses import dataclass, field

from evals.llm import complete_text
from evals.workspace import ModelTarget

STOP = "STOP"


@dataclass
class SimulatedUser:
    target: ModelTarget
    goal: str
    locale: str
    max_turns: int
    history: list[tuple[str, str]] = field(default_factory=list)
    turns: int = 0

    async def next_message(self, her_reply: str) -> str | None:
        if her_reply:
            self.history.append(("her", her_reply))
        if self.turns >= self.max_turns:
            return None
        language = "Chinese" if self.locale.startswith("zh") else "English"
        transcript = "\n".join(f"{who}: {text}" for who, text in self.history[-8:])
        prompt = (
            f"You are a person talking to an assistant in {language}. "
            f"Your goal: {self.goal}\n"
            "Write only the next message you would send. "
            f"If the goal is already met, reply with exactly {STOP}.\n\n"
            f"Conversation so far:\n{transcript or '(you start)'}\n"
        )
        text = (await complete_text(self.target, prompt)).strip()
        self.turns += 1
        if not text or text.upper().startswith(STOP):
            return None
        self.history.append(("you", text))
        return text
