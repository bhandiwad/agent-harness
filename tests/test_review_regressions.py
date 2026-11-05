"""Behavioral regressions from the standalone security and performance review."""

import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pandita_agent.agent.contracts import CreateSession, Turn, Settings
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.sse import events
from pandita_agent.agent.main import work
from .test_runner import Model
from .test_code import project


async def test_sse_transport_chunk_can_contain_many_bounded_events():
    class Response:
        async def aiter_bytes(self):
            yield (b'data: {"text":"' + b"a" * 1000 + b'"}\n\n') * 301

    result = [event async for event in events(Response())]
    assert len(result) == 301


async def test_worker_survives_one_session_failure():
    success = asyncio.Event()
    pending = ["broken", "healthy"]

    class Fake:
        settings = SimpleNamespace(max_active_sessions=10)
        store = SimpleNamespace(list=lambda **kw: [{"id": s} for s in pending])

        async def tick(self, ident):
            pending.remove(ident)
            if ident == "broken":
                raise RuntimeError("sensitive backend diagnostic")
            await asyncio.sleep(0.05)
            success.set()

    task = asyncio.create_task(work(Fake()))
    try:
        await asyncio.wait_for(success.wait(), 2)
        await asyncio.sleep(0.1)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_waiting_sessions_do_not_starve_runnable_work(store):
    store.settings = store.settings.model_copy(update={"max_active_sessions": 500})
    from sqlalchemy import text
    from pandita_agent.contracts import canonical

    with store.transaction() as conn:
        for i in range(120):
            state = {
                "status": "waiting_input",
                "run": {"pending": {"expires_at": time.time() + 3600}},
            }
            conn.execute(
                text(
                    "INSERT INTO agent.sessions VALUES (:tenant,:workspace,:id,'alice',1,CAST(:state AS jsonb),now()-interval '1 hour')"
                ),
                {**store.scope, "id": uuid4(), "state": canonical(state)},
            )
    runner = Runner(store, Model("done"), authorize=lambda *a: None)
    row = runner.create(
        CreateSession(command_id=uuid4(), agent_id="assistant"), "alice"
    )
    runner.turn(
        row["id"],
        Turn(command_id=uuid4(), expected_revision=row["revision"], text="Run now"),
        "alice",
    )
    assert row["id"] in {r["id"] for r in store.list(pending=True)}


def test_generated_team_ids_cannot_shadow_installed_agent(settings):
    data = settings.model_dump(mode="json")
    data["agents"][0]["team"] = [{"name": "Specialist", "instructions": "Work"}]
    data["agents"][1]["id"] = "assistant_s0"
    data["agents"][0]["delegates"] = []
    with pytest.raises(ValueError):
        Settings.model_validate_json(__import__("json").dumps(data))


def test_team_tools_must_be_configured(settings):
    data = settings.model_dump(mode="json")
    data["agents"][0]["team"] = [
        {"name": "Specialist", "instructions": "Work", "tools": ["unknown_tool"]}
    ]
    with pytest.raises(ValueError):
        Settings.model_validate_json(__import__("json").dumps(data))


async def test_code_answer_cannot_bypass_output_schema(store, monkeypatch):
    base = store.settings.agents[0].model_copy(
        update={
            "code": project(),
            "output_schema": {
                "type": "object",
                "required": ["total"],
                "properties": {"total": {"type": "number"}},
            },
        }
    )
    store.settings = store.settings.model_copy(
        update={"agents": (base, *store.settings.agents[1:])}
    )

    async def execute(*a):
        return {"done": True, "answer": "not json"}

    monkeypatch.setattr("pandita_agent.agent.code_runner.execute", execute)
    runner = Runner(store, Model(), authorize=lambda *a: None)
    row = runner.create(
        CreateSession(command_id=uuid4(), agent_id="assistant"), "alice"
    )
    runner.turn(
        row["id"],
        Turn(command_id=uuid4(), expected_revision=row["revision"], text="Calculate"),
        "alice",
    )
    await runner.tick(row["id"])
    state = store.get(row["id"])["state"]
    assert state["status"] == "failed"
    assert state["run"]["completion"]["status"] == "rejected"


async def test_code_delegate_returns_to_parent(store, monkeypatch):
    from pandita_agent.agent.contracts import Call

    specialist = store.settings.agents[1].model_copy(update={"code": project()})
    store.settings = store.settings.model_copy(
        update={"agents": (store.settings.agents[0], specialist)}
    )

    async def execute(*a):
        return {"done": True, "answer": "child result"}

    monkeypatch.setattr("pandita_agent.agent.code_runner.execute", execute)
    model = Model(
        Call(
            name="harness_delegate",
            arguments={"agent": "specialist", "task": "Compute"},
        ),
        "parent final",
    )
    runner = Runner(store, model, authorize=lambda *a: None)
    row = runner.create(
        CreateSession(command_id=uuid4(), agent_id="assistant"), "alice"
    )
    runner.turn(
        row["id"],
        Turn(command_id=uuid4(), expected_revision=row["revision"], text="Delegate"),
        "alice",
    )
    await runner.tick(row["id"])
    state = store.get(row["id"])["state"]
    assert state["status"] == "completed"
    assert state["history"][-1]["messages"][-1]["content"] == "parent final"
    assert len(model.requests) == 2


async def test_api_session_cannot_bypass_workspace_read(store, identity):
    import httpx2
    from pandita_agent.agent.api import create_app
    from pandita_agent.agent.contracts import Upload

    runner = Runner(store, Model(), authorize=lambda *args: None)
    row = runner.create(
        CreateSession(command_id=uuid4(), agent_id="assistant"), "alice"
    )
    runner.upload(
        row["id"],
        Upload(
            command_id=uuid4(),
            expected_revision=row["revision"],
            path="private.txt",
            content="private file bytes",
        ),
        "alice",
    )
    runner.authorize = (
        runner._authorize
    )  # fixture user can read sessions but has no workspace:read
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(runner)),
        base_url="http://localhost",
        headers={"Authorization": "Bearer " + identity("alice")},
    ) as client:
        data = (await client.get("/v1/sessions/" + str(row["id"]))).json()
        assert "content" not in data["state"]["files"]["private.txt"]
        assert (
            await client.get("/v1/sessions/" + str(row["id"]) + "/files/private.txt")
        ).status_code == 403


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_sdk_stops_reading_oversized_response(kind):
    import httpx2
    from pandita_agent.agent.client import Client, AsyncClient

    consumed = []

    class Sync(httpx2.SyncByteStream):
        def __iter__(self):
            for i in range(10):
                consumed.append(i)
                yield b"x" * 1048576

    class Async(httpx2.AsyncByteStream):
        async def __aiter__(self):
            for i in range(10):
                consumed.append(i)
                yield b"x" * 1048576

    if kind == "sync":
        with Client(
            "http://localhost",
            "fixture",
            transport=httpx2.MockTransport(
                lambda r: httpx2.Response(200, stream=Sync())
            ),
        ) as client:
            with pytest.raises(ValueError, match="bound"):
                client.get("/v1/agents")
    else:
        async with AsyncClient(
            "http://localhost",
            "fixture",
            transport=httpx2.MockTransport(
                lambda r: httpx2.Response(200, stream=Async())
            ),
        ) as client:
            with pytest.raises(ValueError, match="bound"):
                await client.get("/v1/agents")
    assert len(consumed) == 5


async def test_missing_code_completion_check_blocks_execution(store, monkeypatch):
    base = store.settings.agents[0].model_copy(
        update={"code": project(), "completion_check": "uninstalled"}
    )
    store.settings = store.settings.model_copy(
        update={"agents": (base, *store.settings.agents[1:])}
    )
    calls = []

    async def execute(*a):
        calls.append(a)
        return {"done": True, "answer": "unsafe success"}

    monkeypatch.setattr("pandita_agent.agent.code_runner.execute", execute)
    runner = Runner(store, Model(), authorize=lambda *a: None)
    row = runner.create(
        CreateSession(command_id=uuid4(), agent_id="assistant"), "alice"
    )
    runner.turn(
        row["id"],
        Turn(command_id=uuid4(), expected_revision=row["revision"], text="Run"),
        "alice",
    )
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "failed"
    assert calls == []


async def test_code_execution_obeys_remaining_run_deadline(store, monkeypatch):
    base = store.settings.agents[0].model_copy(
        update={"code": project(), "max_seconds": 1}
    )
    store.settings = store.settings.model_copy(
        update={"agents": (base, *store.settings.agents[1:])}
    )
    cancelled = []

    async def execute(*a):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    monkeypatch.setattr("pandita_agent.agent.code_runner.execute", execute)
    runner = Runner(store, Model(), authorize=lambda *a: None)
    row = runner.create(
        CreateSession(command_id=uuid4(), agent_id="assistant"), "alice"
    )
    runner.turn(
        row["id"],
        Turn(command_id=uuid4(), expected_revision=row["revision"], text="Run"),
        "alice",
    )
    started = time.monotonic()
    await runner.tick(row["id"])
    assert time.monotonic() - started < 3
    assert cancelled and store.get(row["id"])["state"]["status"] == "failed"


async def test_code_broker_rejects_body_before_reading_remaining_chunks(
    settings, tmp_path
):
    import httpx2
    from pandita_agent.agent.code_broker import create_app
    from .test_sandbox import broker_settings

    key = tmp_path / "transport-key"
    key.write_text("x" * 32)
    consumed = []

    async def body():
        for i in range(10):
            consumed.append(i)
            yield b"x" * 8192

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(
            app=create_app(broker_settings(settings, tmp_path), key)
        ),
        base_url="http://localhost",
    ) as client:
        response = await client.post(
            "/v1/environments",
            content=body(),
            headers={"Authorization": "Bearer " + "x" * 32},
        )
    assert response.status_code == 413
    assert consumed == [0, 1]


async def test_code_broker_does_not_log_exception_contents(
    settings, tmp_path, monkeypatch, caplog
):
    import httpx2
    from pandita_agent.agent.code_broker import create_app
    from .test_sandbox import broker_settings, job

    key = tmp_path / "transport-key"
    key.write_text("x" * 32)

    async def failed(*a):
        raise RuntimeError("PRIVATE-CREDENTIAL-LIKE-CONTENT")

    monkeypatch.setattr(
        "pandita_agent.agent.code_broker.DockerExecutor.execute", failed
    )
    scope = job(settings).scope.model_dump(mode="json")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(
            app=create_app(broker_settings(settings, tmp_path), key)
        ),
        base_url="http://localhost",
    ) as client:
        response = await client.post(
            "/v1/code",
            json={
                "project": project().model_dump(mode="json"),
                "scope": scope,
                "input": "Hello",
                "replay": [],
                "operation_id": str(uuid4()),
            },
            headers={"Authorization": "Bearer " + "x" * 32},
        )
    assert response.status_code == 200 and response.json()["status"] == "unknown"
    assert "PRIVATE-CREDENTIAL-LIKE-CONTENT" not in caplog.text
