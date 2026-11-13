"""Small deterministic output checks. Not a claim of complete DLP coverage."""

import re

from pydantic import Field

from pandita_agent.contracts import Contract


class SafetyPolicy(Contract):
    instructions: str = Field(default="", max_length=8192)
    blocked_text: tuple[str, ...] = Field(default=(), max_length=100)
    block_email_addresses: bool = False


SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|\b(?:sk-proj-|sk-ant-api\d+-)[A-Za-z0-9_-]{20,}|\bBearer\s+[A-Za-z0-9._~-]{24,}"
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def issues(value, policy):
    if SECRET.search(value):
        return [
            "Output contains a credential pattern. Remove credentials before returning or saving it."
        ]
    if policy.block_email_addresses and EMAIL.search(value):
        return ["Workspace policy does not permit email addresses in outputs."]
    if any(
        item and item.casefold() in value.casefold() for item in policy.blocked_text
    ):
        return [
            "Output contains text restricted by workspace policy. Remove it before returning or saving it."
        ]
    return []
