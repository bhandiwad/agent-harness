"""Usage ledger derived from immutable model-attempt events, including child agents."""

from decimal import Decimal

from sqlalchemy import text

KINDS = {
    "model.started",
    "model.completed",
    "code.model_started",
    "code.model_completed",
}


def stamp(settings, row, kind, data):
    if kind not in KINDS:
        return data
    run = row["state"].get("run") or {}
    agent_id = data.get("agent") or run.get("frames", [{}])[-1].get(
        "agent", row["state"]["agent_id"]
    )
    from pandita_agent.agent.teams import members

    agents = [a for root in settings.agents for a in (root, *members(root))]
    base = next((a for a in agents if a.id == agent_id), None)
    model = next(
        (m for m in settings.models if base and m.model_ref == base.model), None
    )
    root_name = row["state"]["agent_id"]
    complete = kind in ("model.completed", "code.model_completed")
    inputs, outputs = data.get("input_tokens"), data.get("output_tokens")
    cost = None
    if (
        complete
        and inputs is not None
        and outputs is not None
        and model
        and model.input_cost_per_million is not None
        and model.output_cost_per_million is not None
    ):
        cost = str(
            (
                Decimal(inputs) * Decimal(str(model.input_cost_per_million))
                + Decimal(outputs) * Decimal(str(model.output_cost_per_million))
            )
            / Decimal(1000000)
        )
    return {
        **data,
        "meter": {
            "call_id": str(run.get("id")) + ":" + str(run.get("steps")),
            "run_id": run.get("id"),
            "session_id": str(row["id"]),
            "owner": row["owner"],
            "agent": agent_id,
            "root_agent": row["state"]["agent_id"],
            "name": agent_id,
            "root_name": root_name,
            "team_step": run.get("frames", [{}])[-1].get("team_step"),
            "model": model.model_name if model else "unavailable",
            "status": "recorded" if complete else "unconfirmed",
            "input_tokens": inputs if complete else None,
            "output_tokens": outputs if complete else None,
            "cost_usd": cost,
            "pricing": "configured_estimate" if cost is not None else "unavailable",
        },
    }


def report(store, actor, period=30, workspace=False):
    if period not in (7, 30, 90):
        raise ValueError("Choose a 7, 30 or 90 day period.")
    with store.transaction() as conn:
        # Aggregate the entire period in one database snapshot. Only history is paged.
        result = conn.execute(
            text("""WITH latest AS MATERIALIZED (
            SELECT DISTINCT ON (data->'meter'->>'call_id') data->'meter' AS m,created_at
            FROM agent.events WHERE data ? 'meter'
              AND created_at >= now()-(:days * interval '1 day')
              AND (:all_users OR data->'meter'->>'owner'=:actor)
            ORDER BY data->'meter'->>'call_id',revision DESC
        ), measured AS MATERIALIZED (
            SELECT m,created_at,coalesce((m->>'input_tokens')::bigint,0) AS inputs,
                coalesce((m->>'output_tokens')::bigint,0) AS outputs,
                (m->>'cost_usd')::numeric AS cost FROM latest
        ), models AS (
            SELECT m->>'model' AS model,count(*) AS calls,sum(inputs) AS input_tokens,
                sum(outputs) AS output_tokens,count(*) FILTER(WHERE cost IS NULL) AS unpriced_calls,
                CASE WHEN count(*) FILTER(WHERE cost IS NULL)=0 THEN sum(cost) END AS cost_usd
            FROM measured GROUP BY m->>'model'
        ) SELECT jsonb_build_object(
            'summary',(SELECT jsonb_build_object(
                'calls',count(*),'input_tokens',coalesce(sum(inputs),0),'output_tokens',coalesce(sum(outputs),0),
                'unconfirmed_calls',count(*) FILTER(WHERE m->>'status'<>'recorded'),
                'unpriced_calls',count(*) FILTER(WHERE cost IS NULL),
                'cost_usd',CASE WHEN count(*) FILTER(WHERE cost IS NULL)=0 THEN coalesce(sum(cost),0) END,
                'known_cost_usd',coalesce(sum(cost),0)) FROM measured),
            'models',(SELECT coalesce(jsonb_agg(to_jsonb(models) ORDER BY model),'[]'::jsonb) FROM models),
            'calls',(SELECT coalesce(jsonb_agg(m || jsonb_build_object('at',created_at) ORDER BY created_at DESC),'[]'::jsonb)
                FROM (SELECT * FROM measured ORDER BY created_at DESC LIMIT 200) recent)
        )"""),
            {"days": period, "actor": actor, "all_users": workspace},
        ).scalar_one()
    return {
        **result,
        "period": period,
        "scope": "workspace" if workspace else "mine",
        "has_more_calls": result["summary"]["calls"] > 200,
        "truncated": False,
        "coverage": "Native harness calls recorded after usage metering was enabled. Earlier runs and external provider calls are not included.",
    }
