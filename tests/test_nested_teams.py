import json
import pytest
from pandita_agent.agent.contracts import Settings
from pandita_agent.agent.runner import Runner
from pandita_agent.runtime.auth import Forbidden
from .test_runner import Model, new, send


def nested(settings):
    data = settings.model_dump(mode="json")
    data["agents"][0].update(
        team_execution="nested",
        team=[
            {
                "name": "Research Team",
                "instructions": "Combine evidence.",
                "team": [
                    {"name": "Researcher", "instructions": "Read supplied evidence."}
                ],
            },
            {"name": "Reviewer", "instructions": "Review result."},
        ],
    )
    return Settings.model_validate_json(json.dumps(data))


async def test_nested_order_recovery_and_shared_budget(store, settings):
    store.settings = nested(settings)
    runner = Runner(
        store,
        Model("facts", "research result", "review", "final"),
        authorize=lambda *a: None,
    )
    row = send(runner, new(runner))
    # Reconstructed runner simulates a worker restart before dispatch.
    restored = Runner(store, runner.provider, authorize=lambda *a: None)
    await restored.tick(row["id"])
    state = store.get(row["id"])["state"]
    assert state["status"] == "completed"
    assert [s["name"] for s in state["run"]["team_steps"]] == [
        "Research Team",
        "Researcher",
        "Reviewer",
    ]
    assert [s["depth"] for s in state["run"]["team_steps"]] == [0, 1, 0]
    assert all(s["status"] == "completed" for s in state["run"]["team_steps"])
    assert state["run"]["input_tokens"] == 400
    assert len(state["run"]["frames"]) == 1
    assert "research result" in json.dumps(runner.provider.requests[2]["messages"])
    before = len(runner.provider.requests)
    await restored.tick(row["id"])
    assert len(runner.provider.requests) == before


async def test_nested_child_access_revoked_before_dispatch(store, settings):
    store.settings = nested(settings)

    def authorize(actor, permission):
        if permission == "agent:assistant_s0_s0":
            raise Forbidden()

    runner = Runner(store, Model(), authorize=authorize)
    row = send(runner, new(runner))
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "failed"
    assert not runner.provider.requests


def test_nested_depth_and_model_validation(settings):
    data = nested(settings).model_dump(mode="json")
    data["agents"][0]["team"][0]["model_ref"] = "missing"
    with pytest.raises(ValueError, match="unconfigured model"):
        Settings.model_validate_json(json.dumps(data))
    data = nested(settings).model_dump(mode="json")
    node = data["agents"][0]["team"][0]["team"][0]
    node["team"] = [
        {
            "name": "Third",
            "instructions": "Work",
            "team": [{"name": "Fourth", "instructions": "Work"}],
        }
    ]
    with pytest.raises(ValueError, match="three nested"):
        Settings.model_validate_json(json.dumps(data))


async def test_restart_after_nested_leaf_completion_does_not_repeat_leaf(
    store, settings
):
    store.settings = nested(settings)
    runner = Runner(store, Model("facts"), authorize=lambda *a: None)
    row = send(runner, new(runner))
    original = runner.commit

    class WorkerStopped(BaseException):
        pass

    def stop_after_commit(session_id, kind, mutate, data=None):
        result = original(session_id, kind, mutate, data)
        if kind == "model.completed":
            raise WorkerStopped()
        return result

    runner.commit = stop_after_commit
    with pytest.raises(WorkerStopped):
        await runner.tick(row["id"])
    recovered = Runner(
        store, Model("research result", "review", "final"), authorize=lambda *a: None
    )
    await recovered.tick(row["id"])
    state = store.get(row["id"])["state"]
    assert state["status"] == "completed"
    assert len(runner.provider.requests) == 1 and len(recovered.provider.requests) == 3
    assert state["run"]["steps"] == 4


def test_completion_extension_cannot_bypass_core_schema(store, settings):
    calls = []

    class ApplicationRunner(Runner):
        def completion_extension(self, state, agent, answer, actor):
            calls.append(answer)
            return []

    runner = ApplicationRunner(store, Model(), authorize=lambda *a: None)
    agent = settings.agents[0].model_copy(update={"output_schema": {"type": "object"}})
    assert runner.completion_issues({}, agent, "not JSON", "alice")
    assert calls == []
    assert runner.completion_issues({}, agent, "{}", "alice") == []
    assert calls == ["{}"]
