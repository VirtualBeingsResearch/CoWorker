from __future__ import annotations

from collections.abc import Iterable


class RegistrationError(ValueError):
    """All configuration issues found while registering one component."""

    def __init__(self, subject: str, issues: Iterable[str]) -> None:
        self.subject = subject
        self.issues = tuple(issues)
        details = "\n".join(f"  - {issue}" for issue in self.issues)
        super().__init__(
            f"{subject} registration failed with {len(self.issues)} "
            f"{'issue' if len(self.issues) == 1 else 'issues'}:\n{details}"
        )
