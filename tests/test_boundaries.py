import asyncio
import time
from uuid import uuid4

import httpx2
import pytest

from pandita_agent.agent.api import create_app
from pandita_agent.agent.contracts import Upload
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.storage import Conflict

from .test_runner import Adapter, Model, approve, call, new, send


async def test_rejected_write_returns_reason_without_dispatch(store):
    adapter = Adapter()
    model = Model(call("create_case", query="A17"), "Action rejected.")
    runner = Runner(store, model, adapters={"create_case": adapter})
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    approve(runner, store.get(row["id"]), approve=False, reason="Not authorized for this task")
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "completed"
    assert not adapter.calls
    assert "Not authorized" in model.requests[-1]["messages"][-1]["content"]


async def test_revoked_reviewer_permission_blocks_approved_dispatch(store):
    adapter = Adapter()
    runner = Runner(
        store, Model(call("create_case", query="A17")), adapters={"create_case": adapter}
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    approve(runner, store.get(row["id"]))
    path = store.settings.policy_file
    path.write_text(
        "\n".join(
            line for line in path.read_text().splitlines() if not line.startswith("g, carol,")
        )
    )
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "failed"
    assert not adapter.calls


async def test_expired_approval_is_not_executed(store, monkeypatch):
    adapter = Adapter()
    runner = Runner(
        store, Model(call("create_case", query="A17")), adapters={"create_case": adapter}
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    waiting = store.get(row["id"])
    future = waiting["state"]["run"]["pending"]["expires_at"] + 1
    monkeypatch.setattr(time, "time", lambda: future)
    with pytest.raises(Conflict):
        approve(runner, waiting)
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "expired"
    assert not adapter.calls


async def test_two_workers_do_not_duplicate_model_dispatch(store):
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(Model):
        async def generate(self, *args):
            entered.set()
            await release.wait()
            return await super().generate(*args)

    model = Slow("Only one reply.")
    runner = Runner(store, model)
    row = new(runner)
    send(runner, row)
    first = asyncio.create_task(runner.tick(row["id"]))
    await entered.wait()
    await Runner(store, model).tick(row["id"])
    release.set()
    await first
    assert len(model.requests) == 1
    assert store.get(row["id"])["state"]["status"] == "completed"


async def test_structured_answer_rejects_invalid_output(store):
    agents = list(store.settings.agents)
    agents[0] = agents[0].model_copy(
        update={
            "output_schema": {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
                "additionalProperties": False,
            }
        }
    )
    store.settings = store.settings.model_copy(update={"agents": tuple(agents)})
    runner = Runner(store, Model('{"count":"invented"}'))
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "failed"
    assert store.get(row["id"])["state"]["history"] == []


async def test_download_is_scoped_attachment_and_openapi_explains_turn(store, identity):
    domain = f"{store.settings.tenant_id}/{store.settings.workspace_id}"
    with store.settings.policy_file.open("a") as f:
        f.write(
            f"\np, operator, {domain}, workspace:write\np, operator, {domain}, workspace:read\ng, dave, private, {domain}\np, private, {domain}, session:read\n"
        )
    runner = Runner(store, Model())
    row = new(runner)
    runner.upload(
        row["id"],
        Upload(
            command_id=uuid4(),
            expected_revision=row["revision"],
            path="notes.txt",
            content="<script>private</script>",
        ),
        "alice",
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(runner)), base_url="http://test"
    ) as client:
        path = f"/v1/sessions/{row['id']}/files/notes.txt"
        client.headers["Authorization"] = "Bearer " + identity("dave")
        assert (await client.get(path)).status_code == 403
        client.headers["Authorization"] = "Bearer " + identity()
        response = await client.get(path)
        assert response.status_code == 200 and response.text == "<script>private</script>"
        assert response.headers["Content-Disposition"] == 'attachment; filename="notes.txt"'
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        spec = (await client.get("/openapi.json")).json()
        schema = spec["paths"]["/v1/sessions/{session_id}/turns"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        assert {"command_id", "expected_revision", "text"} <= set(schema["required"])
