"""Optional trusted worker callbacks. Checks never come from model or session code."""

from dataclasses import dataclass

from pydantic import Field

from pandita_agent.contracts import Contract


@dataclass(frozen=True)
class CompletionContext:
    """Detached snapshot; modifying these values cannot change the conversation."""

    answer: str
    requests: tuple[str, ...]
    facts: dict[str, str]
    files: dict[str, dict]


class CompletionResult(Contract):
    issues: tuple[str, ...] = Field(default=(), max_length=8)

    def bounded(self):
        if any(not issue.strip() or len(issue) > 512 for issue in self.issues):
            raise ValueError("Completion checks must return short actionable issues.")
        return self
