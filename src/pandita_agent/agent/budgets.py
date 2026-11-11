"""Model spending reservations in the existing immutable, workspace-scoped ledger.

No separate billing service or application dependency. An uncertain attempt keeps
its reservation. Prices are operator estimates, not provider invoices.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import Field
from sqlalchemy import text

from pandita_agent.contracts import Contract

Money = Annotated[
    Decimal, Field(strict=False, ge=0, le=1000000000, max_digits=20, decimal_places=10)
]


class Price(Contract):
    input_per_million: Money
    output_per_million: Money

    def cost(self, inputs, outputs):
        return (
            inputs * self.input_per_million + outputs * self.output_per_million
        ) / 1000000


class BudgetPolicy(Contract):
    run_usd: Money | None = None
    agent_month_usd: Money | None = None
    workspace_month_usd: Money | None = None
    agents: dict[str, Money] = Field(default_factory=dict, max_length=1000)
    prices: dict[str, Price] = Field(default_factory=dict, max_length=64)

    @property
    def enforced(self):
        return any(
            v is not None
            for v in (self.run_usd, self.agent_month_usd, self.workspace_month_usd)
        ) or bool(self.agents)


class BudgetExceeded(ValueError):
    """Safe user-facing failure before dispatch; never contains provider data."""


STARTED = {"model.started", "code.model_started"}
COMPLETED = {"model.completed", "code.model_completed"}


def limits(policy, meter, previous, reserve):
    """Pure decision over the latest durable entry for each relevant call."""
    for label, cap, relevant in (
        ("run", policy.run_usd, lambda m: m.get("run_id") == meter["run_id"]),
        (
            "agent monthly",
            policy.agents.get(meter["budget_agent"], policy.agent_month_usd),
            lambda m: (
                m.get("budget_agent", m.get("source_key") or m.get("root_agent"))
                == meter["budget_agent"]
                and m["month"] == meter["month"]
            ),
        ),
        (
            "workspace monthly",
            policy.workspace_month_usd,
            lambda m: m["month"] == meter["month"],
        ),
    ):
        if cap is None:
            continue
        total = Decimal(0)
        for old in previous:
            if not relevant(old):
                continue
            amount = (
                old.get("cost_usd")
                if old.get("status") == "recorded"
                else old.get("reserved_usd")
            )
            if amount is None:
                raise BudgetExceeded(
                    f"The {label} allowance has unpriced or unconfirmed usage. Ask an administrator to reconcile it before continuing."
                )
            total += Decimal(amount)
        if total + reserve > cap:
            raise BudgetExceeded(
                f"The {label} allowance cannot cover the next model call. Increase the allowance or use a smaller task."
            )


def apply(store, conn, row, kind, data):
    if kind not in STARTED | COMPLETED:
        return data
    meter = data["meter"]
    # Every budget writer uses this lock, including changes made by administrators.
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {
            "key": "model-budget:"
            + store.scope["tenant"]
            + ":"
            + store.scope["workspace"]
        },
    )
    policy = store.budget_policy(conn)
    if kind in COMPLETED:
        prior = conn.execute(
            text("""SELECT data->'meter' FROM agent.events
            WHERE data->'meter'->>'call_id'=:call AND kind IN ('model.started','code.model_started')
            ORDER BY revision DESC LIMIT 1"""),
            {"call": meter["call_id"]},
        ).scalar()
        if prior and prior.get("price") is not None:
            price = Price.model_validate(prior["price"])
            meter.update(
                {
                    key: prior[key]
                    for key in ("price", "reserved_usd", "month", "budget_agent")
                }
            )
            meter["cost_usd"] = str(
                price.cost(meter["input_tokens"], meter["output_tokens"])
            )
            meter["pricing"] = "configured_estimate"
        return data

    model_ref = data.get("model_ref")
    price = policy.prices.get(model_ref)
    if price is None:
        model = next(
            (m for m in store.settings.models if m.model_ref == model_ref), None
        )
        if (
            model
            and model.input_cost_per_million is not None
            and model.output_cost_per_million is not None
        ):
            price = Price(
                input_per_million=str(model.input_cost_per_million),
                output_per_million=str(model.output_cost_per_million),
            )
    if price is None:
        if policy.enforced:
            raise BudgetExceeded(
                "This model has no configured price. Ask an administrator to set its price before using a monetary allowance."
            )
        return data
    inputs, outputs = data.get("input_token_bound"), data.get("output_token_limit")
    if any(type(v) is not int or v < 0 for v in (inputs, outputs)):
        raise BudgetExceeded(
            "The model call has no valid spending reservation. No request was sent."
        )
    reserve = price.cost(inputs, outputs)
    month = conn.execute(
        text("SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM')")
    ).scalar_one()
    meter.update(
        price=price.model_dump(mode="json"),
        reserved_usd=str(reserve),
        month=month,
        budget_agent=meter.get("source_key") or meter["root_agent"],
    )
    if policy.enforced:
        previous = (
            conn.execute(
                text("""SELECT m FROM (
            SELECT DISTINCT ON (data->'meter'->>'call_id')
                (data->'meter') || jsonb_build_object('month',coalesce(data->'meter'->>'month',
                    to_char(created_at AT TIME ZONE 'UTC','YYYY-MM'))) AS m
            FROM agent.events WHERE data ? 'meter'
              AND (created_at >= date_trunc('month',clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                   OR data->'meter'->>'run_id'=:run)
            ORDER BY data->'meter'->>'call_id',revision DESC
        ) entries"""),
                {"run": meter["run_id"]},
            )
            .scalars()
            .all()
        )
        limits(policy, meter, previous, reserve)
    return data
