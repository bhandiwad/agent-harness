import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import text

from pandita_agent.agent.budgets import BudgetExceeded, BudgetPolicy, Price, limits
from pandita_agent.agent.native_context import request
from pandita_agent.agent.runner import Runner
from .test_runner import Model, call, new, send


def test_limits_cover_unknowns_agent_identity_and_month():
    m = {"run_id": "run", "budget_agent": "stable", "month": "2026-11"}
    old = {**m, "status": "recorded", "cost_usd": "0.7"}
    limits(BudgetPolicy(agent_month_usd="1"), m, [old], Decimal("0.3"))
    with pytest.raises(BudgetExceeded, match="agent monthly"):
        limits(BudgetPolicy(agent_month_usd="1"), m, [old], Decimal("0.31"))
    limits(
        BudgetPolicy(workspace_month_usd="0.1"),
        m,
        [{**old, "month": "2026-10"}],
        Decimal("0.1"),
    )
    with pytest.raises(BudgetExceeded, match="unpriced"):
        limits(
            BudgetPolicy(run_usd="1"), m, [{**old, "cost_usd": None}], Decimal("0.1")
        )
    with pytest.raises(BudgetExceeded):
        limits(
            BudgetPolicy(workspace_month_usd="0.9"),
            m,
            [{**old, "status": "unconfirmed", "reserved_usd": "0.8"}],
            Decimal("0.2"),
        )


def test_prices_and_effort_are_explicit(settings):
    assert Price(input_per_million="2", output_per_million="8").cost(
        1000, 100
    ) == Decimal("0.0028")
    with pytest.raises(ValueError):
        BudgetPolicy(run_usd="NaN")
    m = settings.models[0].model_copy(
        update={"protocol": "openai_responses", "reasoning_effort": "low"}
    )
    assert request(m, "Do work", [], [], None)["reasoning"]["effort"] == "low"
    assert (
        "effort"
        not in request(settings.models[0], "Do work", [], [], None)["reasoning"]
    )


async def test_no_price_blocks_before_provider_and_saved_error_is_actionable(store):
    store.settings = store.settings.model_copy(
        update={"budgets": BudgetPolicy(workspace_month_usd="10")}
    )
    model = Model()
    runner = Runner(store, model)
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    assert not model.requests
    assert store.get(row["id"])["state"]["status"] == "failed"
    assert "no configured price" in store.get(row["id"])["state"]["error"]


async def test_completed_call_reconciles_and_retains_price_snapshot(store):
    store.settings = store.settings.model_copy(
        update={
            "budgets": BudgetPolicy(
                workspace_month_usd="100",
                prices={"local": Price(input_per_million="2", output_per_million="8")},
            )
        }
    )
    runner = Runner(store, Model())
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    with store.transaction() as conn:
        meters = (
            conn.execute(
                text(
                    "SELECT data->'meter' FROM agent.events WHERE data ? 'meter' ORDER BY revision"
                )
            )
            .scalars()
            .all()
        )
    assert len(meters) == 2
    assert Decimal(meters[0]["reserved_usd"]) > Decimal(meters[1]["cost_usd"])
    assert meters[1]["cost_usd"] == "0.00044"
    assert meters[0]["price"] == meters[1]["price"]


async def test_concurrent_runs_cannot_both_reserve_and_unknown_call_keeps_hold(store):
    # Each call reserves at least 0.02048 for output, so only one fits.
    store.settings = store.settings.model_copy(
        update={
            "budgets": BudgetPolicy(
                workspace_month_usd="0.03",
                prices={"local": Price(input_per_million="0", output_per_million="10")},
            )
        }
    )
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(Model):
        async def generate(self, *args, **kwargs):
            entered.set()
            await release.wait()
            raise TimeoutError("Lost provider result")

    slow, other = Runner(store, Slow()), Runner(store, Model())
    a, b = new(slow), new(other)
    send(slow, a)
    send(other, b)
    pending = asyncio.create_task(slow.tick(a["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await other.tick(b["id"])
        assert not other.provider.requests
        assert "workspace monthly" in store.get(b["id"])["state"]["error"]
    finally:
        release.set()
        await pending
    c = new(other)
    send(other, c)
    await other.tick(c["id"])
    assert not other.provider.requests


async def test_structured_plan_can_be_revised_and_baseline_is_inherited(store):
    store.settings = store.settings.model_copy(
        update={"safety_instructions": "Do not disclose account numbers."}
    )
    model = Model(
        call("harness_plan", steps=[{"title": "Read Source", "status": "working"}]),
        call("harness_plan", steps=[{"title": "Read Source", "status": "completed"}]),
        "Ready",
    )
    runner = Runner(store, model)
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["run"]["plan"][0]["status"] == "completed"
    assert "Do not disclose account numbers." in model.requests[0]["instructions"]
