"""Adapters preserve core isolation, validation and failure recovery."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pandita_agent.agent.contracts import CreateSession
from pandita_agent.agent.main import work


def test_metadata_is_atomic_and_cannot_replace_runtime_state(store):
    request = CreateSession(command_id=uuid4(), agent_id="assistant")
    row = store.create(request, "alice", metadata={"application": {"revision": 2}})
    assert store.get(row["id"])["state"]["application"] == {"revision": 2}
    assert (
        store.create(request, "alice", metadata={"application": {"revision": 2}})["id"]
        == row["id"]
    )
    from pandita_agent.agent.storage import Conflict

    with pytest.raises(Conflict):
        store.create(request, "alice", metadata={"application": {"revision": 3}})
    with pytest.raises(ValueError):
        store.create(
            CreateSession(command_id=uuid4(), agent_id="assistant"),
            "alice",
            metadata={"status": "completed"},
        )


async def test_maintenance_failure_cannot_stop_session_processing(caplog):
    completed = asyncio.Event()
    pending = ["healthy"]

    class Fake:
        settings = SimpleNamespace(max_active_sessions=10)
        store = SimpleNamespace(list=lambda **kw: [{"id": i} for i in pending])

        async def tick(self, ident):
            pending.remove(ident)
            completed.set()

    def maintenance(runner):
        raise RuntimeError("sensitive credential")

    task = asyncio.create_task(work(Fake(), maintenance=maintenance))
    try:
        await asyncio.wait_for(completed.wait(), 2)
        assert not task.done()
        assert "sensitive credential" not in caplog.text
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
