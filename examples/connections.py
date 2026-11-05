"""Print inert configuration examples. Review endpoints, schemas and permissions before use."""

import json

from pandita_agent.agent.sandbox_contracts import BROWSER_SCHEMA, CODE_SCHEMA, RESULT_SCHEMA

connections = [
    {
        "tool": {
            "name": "context_lookup",
            "version": "v1",
            "owner": "operator",
            "transport": "mcp",
            "effect": "read",
            "permission": "context:read",
            "approval_required": False,
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "output_schema": {"type": "object"},
        },
        "binding": {
            "endpoint": "https://context.example.invalid/mcp",
            "tool_name": "REPLACE_WITH_REVIEWED_TOOL",
        },
        "subject_token_files": {"REPLACE_WITH_IDP_SUBJECT": "/run/secrets/context-subject.token"},
    }
]
for kind in ("shell", "python", "browser"):
    connections.append(
        {
            "tool": {
                "name": "sandbox_" + kind,
                "version": "v1",
                "owner": "operator",
                "transport": "sandbox",
                "effect": "read",
                "permission": "execute:" + kind,
                "approval_required": False,
                "timeout_seconds": 60,
                "max_result_bytes": 131072,
                "input_schema": BROWSER_SCHEMA if kind == "browser" else CODE_SCHEMA,
                "output_schema": RESULT_SCHEMA,
            },
            "binding": {"endpoint": "https://sandbox.example.invalid", "kind": kind},
            "subject_token_files": {
                "REPLACE_WITH_IDP_SUBJECT": "/run/secrets/sandbox-subject.token"
            },
        }
    )
print(json.dumps(connections, indent=2))
