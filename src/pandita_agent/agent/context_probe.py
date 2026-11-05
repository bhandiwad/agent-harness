"""Explicit, read-only connector qualification; no model, database or write dispatch."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from pandita_agent.agent.contracts import load_settings
from pandita_agent.agent.mcp import McpAdapter
from pandita_agent.contracts import content_digest
from pandita_agent.files import read_bounded
from pandita_agent.governance.tools import Invocation, Registry, RestAdapter, ToolBlocked
from pandita_agent.runtime.auth import Access, Unauthorized


async def probe(settings, token, *, agent_id, tool_name, arguments, transport=None):
    access = Access(settings)
    principal = access.authenticate(token)
    if principal.draft_digest is not None:
        raise Unauthorized()
    access.require(principal, "session:create")
    access.require(principal, "agent:" + agent_id)
    agent = next((a for a in settings.agents if a.id == agent_id), None)
    connection = next((c for c in settings.connections if c.tool.name == tool_name), None)
    if agent is None or tool_name not in agent.tools or connection is None:
        raise ToolBlocked("Choose a configured tool enabled for this agent.")
    tool = connection.tool
    if tool.effect != "read" or tool.transport not in {"rest", "mcp"}:
        raise ToolBlocked("Context probes accept read-only REST/MCP tools only.")
    access.require(principal, tool.permission)

    async def credential(context):
        path = connection.subject_token_files.get(principal.subject)
        if path is None:
            raise ToolBlocked("Delegated context credentials unavailable.")
        return read_bounded(path, 16384).decode().strip()

    adapter = (
        McpAdapter(connection.binding, tool, credential, transport=transport)
        if tool.transport == "mcp"
        else RestAdapter(
            connection.binding,
            credential,
            limit=tool.max_result_bytes,
            timeout=tool.timeout_seconds,
            transport=transport,
        )
    )

    def authorize(context, permission):
        fresh = Access(settings)
        fresh.require(fresh.authenticate(token), permission)

    registry = Registry((tool,), {tool.name: adapter}, authorize)
    context = Invocation(
        tenant_id=settings.tenant_id,
        workspace_id=settings.workspace_id,
        subject=principal.subject,
        task_id=uuid4(),
        plan_revision=1,
        operation_id=uuid4(),
    )
    result = await registry.read(tool.name, tool.digest, arguments, context)
    authorize(context, tool.permission)
    sources = result.get("sources")
    sources = sources if isinstance(sources, list) else None
    return {
        "status": "contract_passed",
        "tool": tool.name,
        "version": tool.version,
        "operation_id": str(context.operation_id),
        "result_digest": content_digest(result),
        "source_count": len(sources) if sources is not None else None,
        "empty_sources": not sources if sources is not None else None,
        "verified": "identity, delegated transport and configured response schema",
        "not_verified": [
            "answer quality",
            "source authorization inside the context service",
            "production readiness",
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Issue one authorized read-only context probe; emit metadata, not source text."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--arguments-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(
            probe(
                load_settings(args.config),
                read_bounded(args.token_file, 16384).decode().strip(),
                agent_id=args.agent,
                tool_name=args.tool,
                arguments=json.loads(read_bounded(args.arguments_file, 32768)),
            )
        )
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "Authentication, authorization, connection or schema check failed; no successful evidence claim.",
                }
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
