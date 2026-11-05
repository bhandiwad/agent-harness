"""Capacity preflight and deterministic compaction with protected authoritative state."""

from typing import Annotated, Literal

from pydantic import Field

from pandita_agent.contracts import Contract, Digest, Identifier, canonical, content_digest


class Capacity(Contract):
    profile: Identifier
    context_tokens: Annotated[int, Field(ge=1024, le=2000000)]
    output_tokens: Annotated[int, Field(ge=1)]
    safety_tokens: Annotated[int, Field(ge=128)] = 1024


class ContextItem(Contract):
    id: Identifier
    kind: Literal["message", "evidence", "tool_pair"]
    content: str = Field(max_length=131072)
    evidence_ref: str | None = Field(default=None, max_length=1024)
    completed: bool = False


class ContextSnapshot(Contract):
    version: Annotated[int, Field(ge=1)]
    plan_digest: Digest
    protected_digest: Digest
    source_digest: Digest
    profile: Identifier
    policy_digest: Digest
    items: tuple[ContextItem, ...]
    omitted: tuple[Identifier, ...]
    estimated_tokens: int


def token_upper_bound(value: object) -> int:
    # Deliberately conservative UTF-8 byte bound plus framing. Never claim exact tokenization.
    return len(canonical(value).encode("utf-8")) + 64


def preflight(
    capacity: Capacity,
    *,
    instructions: str,
    protected: dict,
    skills: tuple[str, ...],
    tool_schemas: tuple[dict, ...],
    items: tuple[ContextItem, ...],
):
    total = token_upper_bound(
        {
            "instructions": instructions,
            "protected": protected,
            "skills": skills,
            "tools": tool_schemas,
            "items": [item.model_dump(mode="json") for item in items],
        }
    )
    if total + capacity.output_tokens + capacity.safety_tokens > capacity.context_tokens:
        raise ValueError(
            "Context exceeds the configured serving capacity; reduce or split the task."
        )
    return total


def compact(
    capacity: Capacity,
    *,
    plan_digest: str,
    policy_digest: str,
    protected: dict,
    instructions: str,
    skills: tuple[str, ...],
    tool_schemas: tuple[dict, ...],
    items: tuple[ContextItem, ...],
    version: int,
) -> ContextSnapshot:
    """No model summary can alter protected state or split a tool-call/result pair.

    The caller persists source items separately and atomically activates the returned snapshot.
    Failed compaction returns no candidate, so the previous snapshot remains active.
    """
    if len({item.id for item in items}) != len(items):
        raise ValueError("Context item IDs must be unique.")
    source_digest = content_digest([item.model_dump(mode="json") for item in items])
    reduced, omitted, seen = [], [], set()
    for item in items:
        digest = content_digest((item.kind, item.content, item.evidence_ref))
        if item.completed and item.kind != "tool_pair" and digest in seen:
            omitted.append(item.id)
            continue
        seen.add(digest)
        if item.completed and item.evidence_ref and item.kind != "tool_pair":
            item = item.model_copy(update={"content": "Evidence retained at " + item.evidence_ref})
        reduced.append(item)
    while True:
        try:
            estimate = preflight(
                capacity,
                instructions=instructions,
                protected=protected,
                skills=skills,
                tool_schemas=tool_schemas,
                items=tuple(reduced),
            )
            break
        except ValueError:
            index = next((i for i, item in enumerate(reduced) if item.completed), None)
            if index is None:
                raise
            omitted.append(reduced.pop(index).id)
    return ContextSnapshot(
        version=version,
        plan_digest=plan_digest,
        protected_digest=content_digest(protected),
        source_digest=source_digest,
        profile=capacity.profile,
        policy_digest=policy_digest,
        items=tuple(reduced),
        omitted=tuple(omitted),
        estimated_tokens=estimate,
    )
