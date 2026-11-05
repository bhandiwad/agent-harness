import json
from uuid import uuid4

import httpx2
import pytest

from pandita_agent.agent.contracts import Command, Connection
from pandita_agent.agent.mcp import McpAdapter, McpBinding
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.storage import Store
from pandita_agent.governance.tools import Invocation, ToolBlocked

from .test_runner import Model, approve, call, new, send


class Server:
    def __init__(self, tool, mode="json"):
        self.tool, self.mode, self.calls, self.headers = tool, mode, [], []

    def handle(self, request):
        self.headers.append(dict(request.headers))
        assert request.headers["authorization"] == "Bearer delegated-alice"
        if request.method == "DELETE":
            return httpx2.Response(204)
        body = json.loads(request.content)
        method = body["method"]
        headers = {}
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
            headers["Mcp-Session-Id"] = "fixture-session"
        elif method == "notifications/initialized":
            assert "id" not in body
            return httpx2.Response(202)
        elif method == "tools/list":
            assert request.headers["Mcp-Session-Id"] == "fixture-session"
            assert request.headers["MCP-Protocol-Version"] == "2025-11-25"
            schema = self.tool.input_schema if self.mode != "drift" else {"type": "object"}
            receipt_schema = {
                "type": "object",
                "properties": {"operation_id": {"type": "string"}},
                "required": ["operation_id"],
                "additionalProperties": False,
            }
            result = {
                "tools": [
                    {
                        "name": "remote",
                        "inputSchema": schema,
                        "outputSchema": self.tool.output_schema,
                        "annotations": {"readOnlyHint": True},
                    },
                    {
                        "name": "receipt",
                        "inputSchema": receipt_schema,
                        "outputSchema": self.tool.output_schema,
                    },
                ]
            }
        else:
            assert method == "tools/call"
            self.calls.append(body["params"])
            if self.mode == "disconnect":
                return httpx2.Response(
                    200, headers={"content-type": "text/event-stream"}, content=": heartbeat\n\n"
                )
            if body["params"]["name"] == "receipt":
                value = {
                    "status": "confirmed",
                    "operation_id": body["params"]["arguments"]["operation_id"],
                }
            elif self.tool.effect == "write":
                value = {
                    "status": "confirmed",
                    "operation_id": body["params"]["_meta"]["com.pandita/harness"]["operation_id"],
                }
            else:
                value = {"answer": "8 pumps"}
            result = {"structuredContent": value, "content": [], "isError": self.mode == "error"}
        value = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if method == "tools/call" and self.mode in {"sse", "sampling"}:
            prefix = ""
            if self.mode == "sampling":
                prefix = (
                    "data: "
                    + json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "host-request",
                            "method": "sampling/createMessage",
                            "params": {},
                        }
                    )
                    + "\n\n"
                )
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=prefix + "event: message\ndata: " + json.dumps(value) + "\n\n",
            )
        return httpx2.Response(200, json=value, headers=headers)


async def credential(context):
    assert context.subject == "alice"
    return "delegated-alice"


def invocation(settings):
    return Invocation(
        tenant_id=settings.tenant_id,
        workspace_id=settings.workspace_id,
        subject="alice",
        task_id=uuid4(),
        plan_revision=1,
        operation_id=uuid4(),
    )


@pytest.mark.parametrize("mode", ["json", "sse"])
async def test_mcp_initialization_schema_pinning_and_structured_results(settings, mode):
    tool = settings.connections[0].tool
    server = Server(tool, mode)
    adapter = McpAdapter(
        McpBinding(endpoint="https://fixture.invalid/mcp", tool_name="remote"),
        tool,
        credential,
        transport=httpx2.MockTransport(server.handle),
    )
    context = invocation(settings)
    assert await adapter.invoke(tool.name, {"query": "A17"}, context) == {"answer": "8 pumps"}
    assert server.calls[0]["_meta"]["com.pandita/harness"]["operation_id"] == str(
        context.operation_id
    )
    assert server.calls[0]["arguments"] == {"query": "A17"}


@pytest.mark.parametrize("mode", ["drift", "error", "disconnect", "sampling"])
async def test_mcp_rejects_changed_schema_errors_disconnect_and_server_requests(settings, mode):
    tool = settings.connections[0].tool
    server = Server(tool, mode)
    adapter = McpAdapter(
        McpBinding(endpoint="https://fixture.invalid/mcp", tool_name="remote"),
        tool,
        credential,
        transport=httpx2.MockTransport(server.handle),
    )
    with pytest.raises(ToolBlocked):
        await adapter.invoke(tool.name, {"query": "A17"}, invocation(settings))
    assert len(server.calls) == (0 if mode == "drift" else 1)


async def test_mcp_write_approval_lost_receipt_reconciles_without_repeating(store):
    tool = store.settings.connections[1].tool.model_copy(update={"transport": "mcp"})
    binding = McpBinding(
        endpoint="https://fixture.invalid/mcp", tool_name="remote", receipt_tool="receipt"
    )
    connection = Connection(tool=tool, binding=binding)
    settings = store.settings.model_copy(
        update={"connections": (store.settings.connections[0], connection)}
    )
    local = Store(store.engine, settings)
    server = Server(tool, "disconnect")
    adapter = McpAdapter(binding, tool, credential, transport=httpx2.MockTransport(server.handle))
    runner = Runner(
        local,
        Model(call("create_case", query="A17"), "Created."),
        adapters={"create_case": adapter},
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    assert local.get(row["id"])["state"]["status"] == "waiting_approval"
    assert server.calls == []
    approve(runner, local.get(row["id"]))
    await runner.tick(row["id"])
    assert local.get(row["id"])["state"]["status"] == "outcome_unknown"
    assert len(server.calls) == 1
    assert "approval_digest" in server.calls[0]["_meta"]["com.pandita/harness"]
    server.mode = "json"
    runner.reconcile(
        row["id"],
        Command(command_id=uuid4(), expected_revision=local.get(row["id"])["revision"]),
        "carol",
    )
    await runner.tick(row["id"])
    assert local.get(row["id"])["state"]["status"] == "queued"
    await runner.tick(row["id"])
    assert local.get(row["id"])["state"]["status"] == "completed"
    assert [item["name"] for item in server.calls] == ["remote", "receipt"]
