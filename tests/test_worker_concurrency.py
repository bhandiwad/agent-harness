"""A slow user's model call must not serialize every other user's run."""

import asyncio
from types import SimpleNamespace

import pytest

from pandita_agent.agent.main import work


async def test_worker_overlaps_users_without_dispatching_same_session_twice():
    completed = asyncio.Event()
    release = asyncio.Event()
    pending = ["slow", "fast"]
    seen = []

    class Runner:
        settings = SimpleNamespace(max_active_sessions=10)
        store = SimpleNamespace(list=lambda **kwargs: [{"id": ident} for ident in pending])

        async def tick(self, ident):
            seen.append(ident)
            if ident == "slow":
                await release.wait()
            pending.remove(ident)
            if ident == "fast":
                completed.set()

    job = asyncio.create_task(work(Runner()))
    try:
        await asyncio.wait_for(completed.wait(), 2)
        assert pending == ["slow"] and sorted(seen) == ["fast", "slow"]
    finally:
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
