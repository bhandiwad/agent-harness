"""Pinned native specialists in an explicit ordered workflow."""

from pydantic import Field

from pandita_agent.agent.code_contracts import CodeProject
from pandita_agent.contracts import Contract


class TeamMember(Contract):
    model_ref: str = Field(default="", max_length=128, exclude_if=lambda v: not v)
    team: tuple["TeamMember", ...] = Field(
        default=(), max_length=6, exclude_if=lambda v: not v
    )
    name: str = Field(min_length=1, max_length=100)
    instructions: str = Field(min_length=1, max_length=4096)
    tools: tuple[str, ...] = Field(default=(), max_length=10)
    code: CodeProject | None = Field(default=None, exclude_if=lambda v: v is None)
    source_key: str = Field(default="", max_length=128, exclude_if=lambda v: not v)
    source_revision: int = Field(default=0, ge=0, exclude_if=lambda v: not v)
