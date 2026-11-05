"""Pinned MCP Streamable HTTP tools; no token passthrough, subprocesses or discovery grants."""

import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx2
from pydantic import Field, model_validator

from pandita_agent.agent.sse import events
from pandita_agent.bindings import ModelBinding
from pandita_agent.contracts import Contract, content_digest
from pandita_agent.governance.tools import ToolBlocked


class McpBinding(Contract):
    endpoint: str
    tool_name: str = Field(min_length=1, max_length=128)
    protocol_version: str = Field(default="2025-11-25", pattern=r"^2025-(06-18|11-25)$")
    receipt_tool: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def destination(self):
        ModelBinding.explicit_endpoint(self.endpoint)
        return self


class McpAdapter:
    def __init__(self, binding, tool, credential, *, transport=None):
        self.binding, self.tool, self.credential, self.transport = (
            binding,
            tool,
            credential,
            transport,
        )

    @asynccontextmanager
    async def session(self, context):
        token = await self.credential(context)
        if not token or "\n" in token or "\r" in token:
            raise ToolBlocked("MCP delegated credentials unavailable.")
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        }
        async with httpx2.AsyncClient(
            timeout=self.tool.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:

            async def rpc(method, params, *, notification=False):
                request_id = str(uuid4())
                body = {"jsonrpc": "2.0", "method": method, "params": params}
                if not notification:
                    body["id"] = request_id
                async with client.stream(
                    "POST", self.binding.endpoint, headers=headers, json=body
                ) as response:
                    if notification:
                        if response.status_code != 202:
                            raise ToolBlocked("MCP notification was not accepted.")
                        return None
                    if response.status_code != 200:
                        raise ToolBlocked("MCP request did not complete; it was not retried.")
                    if method == "initialize" and "mcp-session-id" in response.headers:
                        session_id = response.headers["mcp-session-id"]
                        if not 1 <= len(session_id) <= 256 or any(
                            not 33 <= ord(c) <= 126 for c in session_id
                        ):
                            raise ToolBlocked("Invalid MCP session identifier.")
                        headers["Mcp-Session-Id"] = session_id
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    if content_type == "text/event-stream":
                        result = None
                        async for _, item in events(
                            response, limit=self.tool.max_result_bytes + 65536
                        ):
                            if item is None:
                                continue
                            if item.get("id") == request_id and "method" not in item:
                                result = item
                                break
                            if "method" in item:
                                # No sampling, elicitation or tool-list changes can expand authority.
                                if (
                                    "id" in item
                                    or item["method"] == "notifications/tools/list_changed"
                                ):
                                    raise ToolBlocked(
                                        "MCP server requested an unsupported interaction."
                                    )
                        if result is None:
                            raise ToolBlocked("MCP response disconnected without a result.")
                    elif content_type == "application/json":
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > self.tool.max_result_bytes + 65536:
                                raise ToolBlocked("MCP response exceeds its limit.")
                        result = json.loads(raw)
                    else:
                        raise ToolBlocked("Unsupported MCP response encoding.")
                    if (
                        result.get("jsonrpc") != "2.0"
                        or result.get("id") != request_id
                        or "error" in result
                        or "result" not in result
                    ):
                        raise ToolBlocked("MCP response is invalid or unsuccessful.")
                    return result["result"]

            initialized = await rpc(
                "initialize",
                {
                    "protocolVersion": self.binding.protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "pandita-agent", "version": "0.4.0"},
                },
            )
            if initialized.get(
                "protocolVersion"
            ) != self.binding.protocol_version or "tools" not in initialized.get(
                "capabilities", {}
            ):
                raise ToolBlocked("MCP protocol or tools capability does not match configuration.")
            headers["MCP-Protocol-Version"] = self.binding.protocol_version
            await rpc("notifications/initialized", {}, notification=True)
            try:
                yield rpc
            finally:
                if "Mcp-Session-Id" in headers:
                    try:
                        await client.delete(self.binding.endpoint, headers=headers, timeout=2)
                    except httpx2.HTTPError:
                        pass

    async def discover(self, rpc):
        tools, cursor, seen = {}, None, set()
        for _ in range(8):
            page = await rpc("tools/list", {"cursor": cursor} if cursor else {})
            for item in page.get("tools", []):
                if item["name"] in tools or len(tools) >= 128:
                    raise ToolBlocked("Duplicate or oversized MCP catalog.")
                tools[item["name"]] = item
            cursor = page.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or len(cursor) > 1024 or cursor in seen:
                raise ToolBlocked("Invalid MCP catalog cursor.")
            seen.add(cursor)
        raise ToolBlocked("MCP catalog pagination limit reached.")

    def check_schema(self, tools, name, expected_input, expected_output):
        tool = tools.get(name)
        if tool is None or content_digest(tool.get("inputSchema")) != content_digest(
            expected_input
        ):
            raise ToolBlocked("MCP tool input schema changed; operator review required.")
        if "outputSchema" in tool and content_digest(tool["outputSchema"]) != content_digest(
            expected_output
        ):
            raise ToolBlocked("MCP output schema changed; operator review required.")

    async def invoke(self, operation, arguments, context):
        if operation != self.tool.name:
            raise ToolBlocked("MCP operation is not configured.")
        async with self.session(context) as rpc:
            tools = await self.discover(rpc)
            self.check_schema(
                tools, self.binding.tool_name, self.tool.input_schema, self.tool.output_schema
            )
            args = arguments["approved_arguments"] if self.tool.effect == "write" else arguments
            meta = {
                "operation_id": str(context.operation_id),
                "tenant_id": str(context.tenant_id),
                "workspace_id": str(context.workspace_id),
                "subject": context.subject,
                "task_id": str(context.task_id),
            }
            if self.tool.effect == "write":
                meta["approval_digest"] = arguments["approval_digest"]
            result = await rpc(
                "tools/call",
                {
                    "name": self.binding.tool_name,
                    "arguments": args,
                    "_meta": {"com.pandita/harness": meta},
                },
            )
            return self.result(result)

    async def reconcile(self, operation_id, context):
        if not self.binding.receipt_tool:
            return None
        async with self.session(context) as rpc:
            tools = await self.discover(rpc)
            schema = {
                "type": "object",
                "properties": {"operation_id": {"type": "string"}},
                "required": ["operation_id"],
                "additionalProperties": False,
            }
            self.check_schema(tools, self.binding.receipt_tool, schema, self.tool.output_schema)
            result = await rpc(
                "tools/call",
                {
                    "name": self.binding.receipt_tool,
                    "arguments": {"operation_id": str(operation_id)},
                    "_meta": {
                        "com.pandita/harness": {
                            "tenant_id": str(context.tenant_id),
                            "workspace_id": str(context.workspace_id),
                            "subject": context.subject,
                        }
                    },
                },
            )
            return self.result(result)

    @staticmethod
    def result(value):
        if value.get("isError"):
            raise ToolBlocked("MCP tool reported an error; no success receipt established.")
        return (
            value["structuredContent"]
            if "structuredContent" in value
            else {"content": value.get("content", [])}
        )
