"""OpenConnector wire contract; no live credentials or provider calls."""

import json
from uuid import uuid4

import httpx2
import pytest
from pydantic import ValidationError

from pandita_agent.agent.contracts import Connection
from pandita_agent.agent.openconnector import OpenConnectorAdapter, OpenConnectorBinding
from pandita_agent.governance.tools import Invocation, ToolBlocked, ToolSpec, ToolUncertain


def fixture(effect="read", handler=None):
    tool = ToolSpec(
        name="jira_search",
        version="v1",
        owner="internal",
        transport="openconnector",
        effect=effect,
        permission="jira:read",
        approval_required=effect == "write",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "channelId": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
    )
    binding = OpenConnectorBinding(
        endpoint="https://gateway.example.test",
        action_id="jira.search_issues",
        subject_connections={"alice": "account-a", "bob": "account-b"},
        fixed_input={"channelId": "approved-channel"},
    )
    context = Invocation(
        tenant_id=uuid4(),
        workspace_id=uuid4(),
        subject="alice",
        task_id=uuid4(),
        plan_revision=1,
        operation_id=uuid4(),
    )

    async def credentials(ctx):
        return "scoped-" + ctx.subject

    calls = []

    def handle(request):
        calls.append(request)
        return (
            handler(request)
            if handler
            else httpx2.Response(
                200,
                json={"success": True, "data": {"issues": []}, "meta": {"executionId": "fixture"}},
            )
        )

    return (
        OpenConnectorAdapter(binding, tool, credentials, transport=httpx2.MockTransport(handle)),
        context,
        calls,
    )


@pytest.mark.asyncio
async def test_subject_scoping_fixed_inputs_and_stable_operation_identity():
    adapter, ctx, calls = fixture()
    for user, connection in [("alice", "account-a"), ("bob", "account-b")]:
        assert await adapter.invoke(
            "jira_search", {"query": "project = DEMO"}, ctx.model_copy(update={"subject": user})
        ) == {"issues": []}
        req = calls[-1]
        assert req.headers["authorization"] == "Bearer scoped-" + user
        assert req.headers["x-oo-connector-app-id"] == connection
        assert req.headers["idempotency-key"] == str(ctx.operation_id)
        assert req.url.path == "/v1/actions/jira.search_issues"
        assert json.loads(req.content) == {
            "input": {"query": "project = DEMO", "channelId": "approved-channel"}
        }
    for args in (
        {"query": "x", "channelId": "another-channel"},
        {"query": "x", "connectionName": "another-account"},
    ):
        with pytest.raises(ToolBlocked):
            await adapter.invoke("jira_search", args, ctx)
    with pytest.raises(ToolBlocked):
        await adapter.invoke(
            "jira_search", {"query": "x"}, ctx.model_copy(update={"subject": "eve"})
        )
    with pytest.raises(ToolBlocked):
        await adapter.invoke("arbitrary.action", {"query": "x"}, ctx)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_write_requires_approval_envelope_and_returns_harness_receipt():
    adapter, ctx, calls = fixture("write")
    with pytest.raises(ToolBlocked):
        await adapter.invoke("jira_search", {"query": "x"}, ctx)
    result = await adapter.invoke(
        "jira_search", {"approved_arguments": {"query": "x"}, "approval_digest": "a" * 64}, ctx
    )
    assert result == {
        "operation_id": str(ctx.operation_id),
        "status": "confirmed",
        "result": {"issues": []},
    }
    assert json.loads(calls[0].content)["input"] == {"query": "x", "channelId": "approved-channel"}
    assert await adapter.reconcile(ctx.operation_id, ctx) is None
    assert len(calls) == 1  # Reconciliation cannot replay an action after its 24h cache expires.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload",
    [
        (302, {}),
        (429, {}),
        (409, {}),
        (500, {}),
        (200, {"success": False, "data": {}}),
        (200, {"success": True, "data": "invalid"}),
    ],
)
async def test_nonconfirmation_is_unknown_and_never_retried(status, payload):
    adapter, ctx, calls = fixture("write", lambda _: httpx2.Response(status, json=payload))
    with pytest.raises(ToolUncertain):
        await adapter.invoke(
            "jira_search", {"approved_arguments": {"query": "x"}, "approval_digest": "a" * 64}, ctx
        )
    assert await adapter.reconcile(ctx.operation_id, ctx) is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_oversized_response_and_transport_loss_are_not_success():
    adapter, ctx, calls = fixture(handler=lambda _: httpx2.Response(200, content=b"x" * 100000))
    with pytest.raises(ToolUncertain):
        await adapter.invoke("jira_search", {"query": "x"}, ctx)
    assert len(calls) == 1


def test_binding_contract_rejects_unsafe_endpoints_and_header_values():
    adapter, _, _ = fixture()
    assert (
        Connection(tool=adapter.tool, binding=adapter.binding).binding.action_id
        == "jira.search_issues"
    )
    for patch in (
        {"endpoint": "http://remote.example"},
        {"endpoint": "https://user:pass@example.test"},
        {"action_id": "../proxy"},
        {"subject_connections": {"alice": "x\r\nAuthorization: bad"}},
    ):
        with pytest.raises(ValidationError):
            OpenConnectorBinding.model_validate({**adapter.binding.model_dump(), **patch})


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [False, True])
async def test_runner_write_approval_and_unknown_outcome_never_replay(store, lost_response):
    from pandita_agent.agent.contracts import Command
    from pandita_agent.agent.runner import Runner

    from .test_runner import Model, approve, call, new, send

    tool = store.settings.connections[1].tool.model_copy(update={"transport": "openconnector"})
    binding = OpenConnectorBinding(
        endpoint="https://gateway.example.test",
        action_id="slack.post_message",
        subject_connections={"alice": "sales-slack"},
        fixed_input={"channelId": "internal-digest"},
    )
    calls = []

    def handle(request):
        calls.append(request)
        if lost_response:
            raise httpx2.ReadTimeout("synthetic lost response")
        return httpx2.Response(
            200, json={"success": True, "data": {"ts": "1.2", "channelId": "internal-digest"}}
        )

    async def credentials(context):
        return "scoped-alice"

    adapter = OpenConnectorAdapter(
        binding, tool, credentials, transport=httpx2.MockTransport(handle)
    )
    runner = Runner(
        store,
        Model(call("create_case", query="A source-backed digest"), "Sent the approved digest."),
        adapters={"create_case": adapter},
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["status"] == "waiting_approval" and not calls
    approve(runner, row)
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["status"] == ("outcome_unknown" if lost_response else "completed")
    assert len(calls) == 1
    if lost_response:
        runner.reconcile(
            row["id"], Command(command_id=uuid4(), expected_revision=row["revision"]), "carol"
        )
        await runner.tick(row["id"])
        assert store.get(row["id"])["state"]["status"] == "outcome_unknown"
        assert len(calls) == 1


def test_runner_registers_openconnector_without_custom_injection(settings):
    from types import SimpleNamespace

    from pandita_agent.agent.runner import Runner

    existing = settings.connections[0]
    connection = Connection(
        tool=existing.tool.model_copy(update={"transport": "openconnector"}),
        binding=OpenConnectorBinding(
            endpoint="https://gateway.example.test",
            action_id="jira.search_issues",
            subject_connections={"alice": "account-a"},
        ),
    )
    changed = settings.model_copy(update={"connections": (connection, *settings.connections[1:])})
    runner = Runner(SimpleNamespace(settings=changed), None, authorize=lambda *_: None)
    assert isinstance(runner.adapters[existing.tool.name], OpenConnectorAdapter)
