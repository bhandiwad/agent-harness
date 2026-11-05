"""Coded agents retain immutable source, existing tool authority and private outputs."""

from uuid import uuid4

import pytest

from pandita_agent.agent.code_contracts import CodeProject
from pandita_agent.agent.contracts import CreateSession, Turn
from pandita_agent.agent.runner import Runner
from pandita_agent.runtime.auth import Forbidden

from .test_runner import Model


def project(**changes):
    return CodeProject(
        language="python",
        entrypoint="agent.py",
        files={"agent.py": "def run(input, ctx): return input"},
        **changes,
    )


def test_source_validation_and_legacy_serialization(settings):
    assert "code" not in settings.agents[0].model_dump(mode="json")
    for name in ["../secret.py", "/secret.py", "a/../../secret.py", ".hidden.py", "agent.sh"]:
        with pytest.raises(ValueError):
            CodeProject(language="python", entrypoint=name, files={name: "content"})
    with pytest.raises(ValueError):
        project(test_input="x" * 8193)


@pytest.mark.asyncio
async def test_model_then_file_completion_and_private_ownership(store, monkeypatch):
    base = store.settings.agents[0].model_copy(
        update={"delegates": (), "code": project(), "native_tools": ("harness_write_file",)}
    )
    store.settings = store.settings.model_copy(update={"agents": (base,)})
    model = Model("Verified brief")
    calls = []

    async def execute(code, input, replay, scope, operation):
        calls.append(replay)
        return (
            {
                "done": True,
                "answer": replay[0]["result"],
                "files": {"brief.md": replay[0]["result"]},
                "logs": "Finished",
            }
            if replay
            else {"done": False, "request": {"kind": "model", "prompt": input}, "logs": "Started"}
        )

    monkeypatch.setattr("pandita_agent.agent.code_runner.execute", execute)
    allowed = []

    def authorize(actor, permission):
        if actor != "alice":
            raise Forbidden()
        allowed.append(permission)

    runner = Runner(store, authorize=authorize, provider=model)
    row = runner.create(CreateSession(command_id=uuid4(), agent_id="assistant"), "alice")
    runner.turn(
        row["id"],
        Turn(command_id=uuid4(), expected_revision=row["revision"], text="Facts"),
        "alice",
    )
    await runner.tick(row["id"])
    state = store.get(row["id"])["state"]
    assert state["status"] == "completed"
    assert state["files"]["brief.md"]["content"] == "Verified brief"
    assert len(model.requests) == 1 and len(calls) == 2
    assert "workspace:write" in allowed
    with pytest.raises(Forbidden):
        runner.turn(
            row["id"],
            Turn(
                command_id=uuid4(),
                expected_revision=store.get(row["id"])["revision"],
                text="Read private work",
            ),
            "bob",
        )


@pytest.mark.asyncio
async def test_code_error_and_denied_tool_do_not_call_provider(store, monkeypatch):
    base = store.settings.agents[0].model_copy(
        update={"delegates": (), "code": project(), "tools": ()}
    )
    store.settings = store.settings.model_copy(update={"agents": (base,)})
    model = Model()
    response = {"error": "ValueError: fix agent.py line 3", "logs": "Entered run"}

    async def execute(*args):
        return response

    monkeypatch.setattr("pandita_agent.agent.code_runner.execute", execute)
    runner = Runner(store, authorize=lambda *args: None, provider=model)
    for value in [
        response,
        {"done": False, "request": {"kind": "tool", "name": "create_case", "arguments": {}}},
    ]:
        response = value
        row = runner.create(CreateSession(command_id=uuid4(), agent_id="assistant"), "alice")
        runner.turn(
            row["id"],
            Turn(command_id=uuid4(), expected_revision=row["revision"], text="Facts"),
            "alice",
        )
        await runner.tick(row["id"])
        state = store.get(row["id"])["state"]
        assert state["status"] == "failed"
        assert not state.get("files")
    assert not model.requests
