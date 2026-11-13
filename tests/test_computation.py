import json

import pytest
import httpx2

from pandita_agent.agent.computation import validate_chart
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.safety import SafetyPolicy, issues
from .test_runner import Model, call, new, send


async def test_broker_readiness_is_authenticated_and_scope_specific(settings, tmp_path):
    from pandita_agent.agent.code_broker import create_app
    from .test_sandbox import broker_settings
    cfg = broker_settings(settings, tmp_path).model_copy(update={"images": {"shell": "sha256:" + "a" * 64}})
    token = tmp_path / "transport-key"
    token.write_text("test-only-" * 5)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=create_app(cfg, token)), base_url="http://test") as client:
        assert (await client.get("/v1/capabilities")).status_code == 401
        response = await client.get("/v1/capabilities", headers={"Authorization": "Bearer " + token.read_text()})
        assert response.json() == {"tenant_id": str(settings.tenant_id), "workspace_id": str(settings.workspace_id), "network": "none", "languages": ["python", "javascript"]}


def test_chart_only_accepts_bounded_data():
    chart = {
        "title": "Revenue",
        "labels": ["A", "B"],
        "series": [{"name": "Sales", "values": [2, 3]}],
    }
    assert validate_chart(json.dumps(chart)) == chart
    for change in (
        {"script": "alert(1)"},
        {"labels": []},
        {"series": [{"name": "bad", "values": [float("nan"), 2]}]},
    ):
        with pytest.raises(ValueError):
            validate_chart(json.dumps({**chart, **change}))


def test_safety_does_not_claim_to_remove_all_personal_data():
    assert issues("Contact person@example.invalid", SafetyPolicy()) == []
    assert issues(
        "Contact person@example.invalid", SafetyPolicy(block_email_addresses=True)
    )
    assert issues("-----BEGIN PRIVATE KEY-----", SafetyPolicy())
    assert issues(
        "CUSTOM confidential item", SafetyPolicy(blocked_text=("confidential item",))
    )


async def test_computation_is_opt_in_and_cannot_call_connected_tools(
    store, monkeypatch
):
    calls = []

    async def execute(project, message, replay, scope, operation):
        calls.append((project, json.loads(message), scope))
        return {
            "done": False,
            "request": {"kind": "tool", "name": "create_case", "arguments": {}},
        }

    monkeypatch.setattr("pandita_agent.agent.code_runner.execute", execute)
    compute = call(
        "harness_compute", code="def run(input,ctx): return 'ok'", files=[], data="1,2"
    )
    runner = Runner(store, Model(compute), authorize=lambda *args: None)
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    assert not calls
    assert store.get(row["id"])["state"]["status"] == "failed"
    agents = tuple(
        a.model_copy(update={"computation": True}) for a in store.settings.agents
    )
    store.settings = store.settings.model_copy(update={"agents": agents})
    runner = Runner(
        store,
        Model(compute, "Cannot use connected tools in analysis."),
        authorize=lambda *args: None,
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    assert len(calls) == 1
    assert calls[0][2]["subject"] == "alice"
    assert store.get(row["id"])["state"]["status"] == "completed"


async def test_analysis_artifacts_saved_atomically_and_invalid_chart_can_be_corrected(
    store, monkeypatch
):
    store.settings = store.settings.model_copy(
        update={
            "agents": tuple(
                a.model_copy(update={"computation": True})
                for a in store.settings.agents
            )
        }
    )
    attempts = []

    async def execute(*args):
        attempts.append(True)
        chart = {
            "title": "Sales",
            "labels": ["A"],
            "series": [{"name": "Revenue", "values": [10]}],
        }
        return {
            "done": True,
            "answer": "Total 10",
            "files": {
                "report.md": "Total: 10",
                "revenue.chart.json": json.dumps(chart) if len(attempts) > 1 else "bad",
            },
        }

    monkeypatch.setattr("pandita_agent.agent.code_runner.execute", execute)
    compute = call(
        "harness_compute", code="def run(input,ctx): return 'ok'", files=[], data=""
    )
    runner = Runner(
        store, Model(compute, compute, "Total 10"), authorize=lambda *args: None
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    state = store.get(row["id"])["state"]
    assert state["status"] == "completed"
    assert len(attempts) == 2
    assert state["files"]["report.md"]["version"] == 1
    assert state["run"]["inflight"] is None


async def test_blocked_final_content_is_not_exposed_as_rejected_draft(store):
    runner = Runner(
        store, Model("-----BEGIN PRIVATE KEY-----", "Sensitive material removed.")
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    state = store.get(row["id"])["state"]
    assert state["status"] == "completed"
    assert "BEGIN PRIVATE" not in json.dumps(state)


@pytest.mark.parametrize("diagnostic, visible", [
    ("name 'ctx' is not defined", True),
    ("string indices must be integers, not 'str'", True),
    ("x" * 2100, True),
    ("x" * 2100 + "-----BEGIN PRIVATE KEY-----", False),
])
async def test_analysis_error_feedback_is_bounded_safe_and_allows_correction(
    store, monkeypatch, diagnostic, visible
):
    store.settings = store.settings.model_copy(update={"agents": tuple(
        a.model_copy(update={"computation": True}) for a in store.settings.agents
    )})
    results = iter([
        {"error": diagnostic, "files": {"partial.txt": "must not persist"}},
        {"done": True, "answer": "Total 0", "files": {"result.txt": "0"}},
    ])
    async def execute(*args):
        return next(results)
    monkeypatch.setattr("pandita_agent.agent.code_runner.execute", execute)
    compute = call("harness_compute", code="def run(input, ctx): return '0'", files=[], data="0")
    model = Model(compute, compute, "Total 0")
    runner = Runner(store, model, authorize=lambda *args: None)
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    feedback = json.loads(model.requests[1]["messages"][-1]["content"])
    assert ("diagnostic" in feedback) == visible
    if visible:
        assert feedback["diagnostic"] == diagnostic[:2000]
    state = store.get(row["id"])["state"]
    assert state["status"] == "completed"
    assert "partial.txt" not in state["files"]
    assert state["files"]["result.txt"]["content"] == "0"
    assert "BEGIN PRIVATE" not in json.dumps(model.requests)
