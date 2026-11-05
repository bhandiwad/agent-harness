import asyncio
import copy
import json
from uuid import uuid4

import pytest

from pandita_agent.agent.contracts import (
    Call,
    Command,
    CreateSession,
    Decision,
    Memory,
    ModelResult,
    Turn,
)
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.storage import Conflict, NotFound, Store, check_role
from pandita_agent.runtime.auth import Forbidden


class Model:
    def __init__(self, *steps):
        self.steps, self.requests = list(steps), []

    async def generate(self, model, instructions, messages, tools, max_tokens, request_timeout):
        self.requests.append(
            copy.deepcopy({"instructions": instructions, "messages": messages, "tools": tools})
        )
        value = self.steps.pop(0) if self.steps else "Done."
        return ModelResult(
            text=value if isinstance(value, str) else "",
            call=value if isinstance(value, Call) else None,
            input_tokens=100,
            output_tokens=30,
        )


class Adapter:
    def __init__(self, uncertain=False):
        self.calls, self.receipts, self.uncertain = [], [], uncertain

    async def invoke(self, name, arguments, context):
        self.calls.append((name, arguments, context))
        if name == "create_case":
            if self.uncertain:
                raise TimeoutError("Simulated lost receipt")
            return {
                "operation_id": str(context.operation_id),
                "status": "confirmed",
                "result": {"case": "CASE-17"},
            }
        return {
            "sources": [
                {
                    "reference": "fixture://assets/A17",
                    "version": "1",
                    "text": "Asset A17 has 8 pumps.",
                }
            ]
        }

    async def reconcile(self, operation_id, context):
        self.receipts.append(operation_id)
        return {
            "operation_id": str(operation_id),
            "status": "confirmed",
            "result": {"case": "CASE-17"},
        }


def call(name, /, **arguments):
    return Call(name=name, arguments=arguments)


def new(runner):
    return runner.create(CreateSession(command_id=uuid4(), agent_id="assistant"), "alice")


def send(runner, row, text="Help with asset A17"):
    return runner.turn(
        row["id"], Turn(command_id=uuid4(), expected_revision=row["revision"], text=text), "alice"
    )


def approve(runner, row, actor="carol", **changes):
    request = Decision(
        command_id=uuid4(),
        expected_revision=row["revision"],
        action_digest=row["state"]["run"]["pending"]["digest"],
        approve=True,
        reason="Reviewed exact action",
    )
    return runner.decide(row["id"], request.model_copy(update=changes), actor)


async def test_persistent_conversation_more_than_five_turns_and_restart(store):
    model = Model()
    runner = Runner(store, model)
    row = new(runner)
    for number in range(8):
        send(runner, row, f"Question number {number}")
        await runner.tick(row["id"])
        row = store.get(row["id"])
        assert row["state"]["status"] == "completed"
        runner = Runner(Store(store.engine, store.settings), model)
    assert len(row["state"]["history"]) == 8
    assert model.requests[-1]["messages"][0]["content"] == "Question number 0"
    assert len(model.requests) == 8
    check_role(store.engine)


async def test_context_tool_then_followup_is_not_a_report(store):
    adapter = Adapter()
    model = Model(
        call("context_lookup", query="asset A17"),
        "A17 has 8 pumps [fixture://assets/A17].",
        "The previous result was 8 pumps.",
    )
    runner = Runner(store, model, adapters={"context_lookup": adapter})
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["status"] == "completed"
    assert len(adapter.calls) == 1
    assert "approval.requested" not in [event["kind"] for event in store.events(row["id"])]
    send(runner, row, "How many did you say?")
    await runner.tick(row["id"])
    assert any(
        "fixture://assets/A17" in message.get("content", "")
        for message in model.requests[-1]["messages"]
    )
    assert "report" not in row["state"]


async def test_clarification_wait_survives_restart(store):
    model = Model(call("harness_ask_user", question="Which asset?"), "I will use A17.")
    runner = Runner(store, model)
    row = new(runner)
    send(runner, row, "Look up the asset")
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["status"] == "waiting_input"
    run_id = row["state"]["run"]["id"]
    runner = Runner(Store(store.engine, store.settings), model)
    send(runner, row, "A17")
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["run"]["id"] == run_id
    assert row["state"]["status"] == "completed"
    assert json.loads(model.requests[-1]["messages"][-1]["content"]) == {"answer": "A17"}


async def test_write_requires_independent_exact_approval_and_rechecks_permission(store):
    adapter = Adapter()
    runner = Runner(
        store,
        Model(call("create_case", query="A17 issue"), "Created CASE-17."),
        adapters={"create_case": adapter},
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["status"] == "waiting_approval" and adapter.calls == []
    with pytest.raises(Conflict):
        approve(runner, row, "alice")
    with pytest.raises(Conflict):
        approve(runner, row, action_digest="0" * 64)
    approved = approve(runner, row)
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "completed"
    assert len(adapter.calls) == 1
    assert adapter.calls[0][1]["approval_digest"] == row["state"]["run"]["pending"]["digest"]
    assert approved["revision"] > row["revision"]


async def test_uncertain_write_is_never_repeated_and_reconciles(store):
    adapter = Adapter(uncertain=True)
    runner = Runner(
        store,
        Model(call("create_case", query="A17"), "Created CASE-17."),
        adapters={"create_case": adapter},
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    approve(runner, store.get(row["id"]))
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["status"] == "outcome_unknown"
    await runner.tick(row["id"])
    with pytest.raises(Conflict):
        send(runner, row)
    runner.reconcile(
        row["id"], Command(command_id=uuid4(), expected_revision=row["revision"]), "carol"
    )
    await runner.tick(row["id"])
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "completed"
    assert len(adapter.calls) == len(adapter.receipts) == 1


async def test_crash_reservation_stops_ambiguous_model_replay(store):
    model = Model("Should not execute")
    runner = Runner(store, model)
    row = new(runner)
    send(runner, row)
    runner.commit(row["id"], "fixture.crash", lambda s: s["run"].update(inflight={"kind": "model"}))
    await Runner(Store(store.engine, store.settings), model).tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "interrupted"
    assert model.requests == []


async def test_cancel_during_model_call_discards_late_answer(store):
    entered, release = asyncio.Event(), asyncio.Event()

    class Waiting(Model):
        async def generate(self, *args):
            entered.set()
            await release.wait()
            return await super().generate(*args)

    runner = Runner(store, Waiting("Late answer"))
    row = new(runner)
    send(runner, row)
    running = asyncio.create_task(runner.tick(row["id"]))
    await entered.wait()
    row = store.get(row["id"])
    runner.cancel(
        row["id"], Command(command_id=uuid4(), expected_revision=row["revision"]), "alice"
    )
    release.set()
    await running
    assert store.get(row["id"])["state"]["status"] == "cancelled"


async def test_cancel_before_dispatch_cannot_be_overridden(store):
    adapter = Adapter()
    row = None

    def cancel(tool, args, context):
        current = store.get(row["id"])
        runner.cancel(
            row["id"], Command(command_id=uuid4(), expected_revision=current["revision"]), "alice"
        )

    runner = Runner(
        store,
        Model(call("context_lookup", query="A17")),
        adapters={"context_lookup": adapter},
        before_tool=cancel,
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    assert adapter.calls == []
    assert store.get(row["id"])["state"]["status"] == "cancelled"


async def test_delegation_uses_configured_specialist_and_shared_budget(store):
    adapter = Adapter()
    model = Model(
        call("harness_delegate", agent="specialist", task="Look up A17"),
        call("context_lookup", query="A17"),
        "8 pumps",
        "The specialist found 8 pumps.",
    )
    runner = Runner(store, model, adapters={"context_lookup": adapter})
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["status"] == "completed"
    assert row["state"]["run"]["steps"] == 4
    assert "Answer the assigned subtask" in model.requests[1]["instructions"]


def test_commands_are_idempotent_and_stale_changes_are_rejected(store):
    runner = Runner(store, Model())
    request = CreateSession(command_id=uuid4(), agent_id="assistant")
    row = runner.create(request, "alice")
    assert runner.create(request, "alice")["id"] == row["id"]
    command = Turn(command_id=uuid4(), expected_revision=row["revision"], text="Hello")
    accepted = runner.turn(row["id"], command, "alice")
    assert runner.turn(row["id"], command, "alice")["revision"] == accepted["revision"]
    with pytest.raises(Conflict):
        runner.turn(row["id"], command.model_copy(update={"text": "Changed"}), "alice")
    with pytest.raises(Conflict):
        runner.cancel(
            row["id"], Command(command_id=uuid4(), expected_revision=row["revision"]), "alice"
        )


async def test_permission_revoked_before_tool_exec_fails_closed(store):
    adapter = Adapter()
    model = Model(call("context_lookup", query="A17"))
    runner = Runner(store, model, adapters={"context_lookup": adapter})
    row = new(runner)
    send(runner, row)
    current = store.get(row["id"])
    await runner.model_step(current, runner.agents["assistant"], runner.agents["assistant"])
    policy = store.settings.policy_file
    policy.write_text(policy.read_text().replace("tool:read", "tool:revoked"))
    await runner.tick(row["id"])
    assert adapter.calls == [] and store.get(row["id"])["state"]["status"] == "failed"


def test_tenant_isolation_and_owner_check(store):
    runner = Runner(store, Model())
    row = new(runner)
    other = Store(store.engine, store.settings.model_copy(update={"tenant_id": uuid4()}))
    with pytest.raises(NotFound):
        other.get(row["id"])
    with pytest.raises(Forbidden):
        runner.turn(
            row["id"],
            Turn(command_id=uuid4(), expected_revision=row["revision"], text="Foreign turn"),
            "nobody",
        )


async def test_compaction_preserves_pinned_memory_and_archived_history(store):
    agent = store.settings.agents[0]
    agent = agent.model_copy(
        update={
            "capacity": agent.capacity.model_copy(update={"context_tokens": 12000}),
            "max_tokens": 64000,
        }
    )
    settings = store.settings.model_copy(update={"agents": (agent, store.settings.agents[1])})
    local = Store(store.engine, settings)
    model = Model(*([" x" * 1500] * 6))
    runner = Runner(local, model)
    row = new(runner)
    row = runner.memory(
        row["id"],
        Memory(
            command_id=uuid4(), expected_revision=row["revision"], facts={"customer": "A17 only"}
        ),
        "alice",
    )
    for number in range(6):
        send(runner, row, "Question " + str(number))
        await runner.tick(row["id"])
        row = local.get(row["id"])
        assert row["state"]["status"] == "completed"
    assert len(row["state"]["history"]) == 6
    assert "A17 only" in model.requests[-1]["instructions"]
    assert "Archived turn IDs" in model.requests[-1]["instructions"]


async def test_model_step_budget_stops_loop(store):
    agent = store.settings.agents[0].model_copy(update={"max_steps": 2})
    settings = store.settings.model_copy(update={"agents": (agent, store.settings.agents[1])})
    model = Model(
        call("harness_plan", steps=["Read data"]),
        call("harness_plan", steps=["Still planning"]),
        "Should never run",
    )
    runner = Runner(Store(store.engine, settings), model)
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    assert len(model.requests) == 2 and store.get(row["id"])["state"]["status"] == "failed"
