import json
from uuid import uuid4

import pytest

from pandita_agent.agent.contracts import Upload
from pandita_agent.agent.native_tools import NATIVE, invoke, write_file
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.storage import Store

from .test_runner import Model, call, new, send


def test_decimal_calculation_and_no_code_execution():
    assert (
        invoke(
            "harness_calculate",
            {"operation": "subtract", "left": "10.1", "right": "8.2", "unit": "units"},
            {},
        )["value"]
        == "1.9"
    )
    for left, right in [("__import__('os')", "2"), ("NaN", "1"), ("1", "0"), ("1e999", "1")]:
        with pytest.raises(ValueError):
            invoke(
                "harness_calculate",
                {"operation": "divide", "left": left, "right": right, "unit": ""},
                {},
            )


@pytest.mark.parametrize("path", ["/etc/passwd", "../secret", "a/../../secret", "a//b", "./a"])
def test_workspace_never_accepts_host_paths(path):
    with pytest.raises(ValueError):
        write_file({}, path, "test", None)


def test_workspace_versions_search_and_stale_replacement():
    state = {}
    receipt = write_file(state, "notes/asset.txt", "Asset A17\n8 pumps\n", None)
    assert receipt["published"] is False
    with pytest.raises(ValueError):
        write_file(state, "notes/asset.txt", "Wrong overwrite", None)
    value = invoke(
        "harness_read_file", {"path": "notes/asset.txt", "start_line": 2, "max_lines": 1}, state
    )
    assert value["lines"] == [{"line": 2, "text": "8 pumps"}]
    assert invoke("harness_search_files", {"query": "PUMPS"}, state)["matches"][0]["line"] == 2
    updated = write_file(state, "notes/asset.txt", "9 pumps", receipt["digest"])
    assert updated["version"] == 2


async def test_agent_creates_private_artifact_and_reads_it_after_restart(store):
    settings = store.settings.model_copy(
        update={
            "agents": (
                store.settings.agents[0].model_copy(update={"native_tools": tuple(NATIVE)}),
                store.settings.agents[1],
            )
        }
    )
    domain = f"{settings.tenant_id}/{settings.workspace_id}"
    with settings.policy_file.open("a") as stream:
        stream.write(
            f"\np, operator, {domain}, workspace:read\np, operator, {domain}, workspace:write\n"
        )
    model = Model(
        call("harness_write_file", path="result.txt", content="8 pumps", expected_digest=None),
        "Saved a private result.",
        call("harness_read_file", path="result.txt", start_line=1, max_lines=10),
        "The file says 8 pumps.",
    )
    local = Store(store.engine, settings)
    runner = Runner(local, model)
    row = new(runner)
    send(runner, row, "Save the answer in result.txt")
    await runner.tick(row["id"])
    row = local.get(row["id"])
    assert row["state"]["status"] == "completed"
    assert row["state"]["files"]["result.txt"]["content"] == "8 pumps"
    runner = Runner(Store(store.engine, settings), model)
    send(runner, row, "Read the file")
    await runner.tick(row["id"])
    assert local.get(row["id"])["state"]["status"] == "completed"
    assert (
        json.loads(model.requests[-1]["messages"][-1]["content"])["lines"][0]["text"] == "8 pumps"
    )


def test_upload_is_owner_scoped_and_idempotent(store):
    domain = f"{store.settings.tenant_id}/{store.settings.workspace_id}"
    with store.settings.policy_file.open("a") as stream:
        stream.write(f"\np, operator, {domain}, workspace:write\n")
    runner = Runner(store, Model())
    row = new(runner)
    request = Upload(
        command_id=uuid4(),
        expected_revision=row["revision"],
        path="context.txt",
        content="Private input",
    )
    first = runner.upload(row["id"], request, "alice")
    assert runner.upload(row["id"], request, "alice")["revision"] == first["revision"]
    assert first["state"]["files"]["context.txt"]["version"] == 1


@pytest.mark.parametrize("repeat_conflict", [False, True])
async def test_rejected_local_edit_returns_error_without_overwrite_and_remains_bounded(
    store, repeat_conflict
):
    import hashlib

    settings = store.settings.model_copy(
        update={
            "agents": (
                store.settings.agents[0].model_copy(
                    update={"native_tools": tuple(NATIVE), "max_steps": 5}
                ),
                store.settings.agents[1],
            )
        }
    )
    domain = f"{settings.tenant_id}/{settings.workspace_id}"
    with settings.policy_file.open("a") as stream:
        stream.write(
            f"\np, operator, {domain}, workspace:read\np, operator, {domain}, workspace:write\n"
        )
    initial = call("harness_write_file", path="plan.md", content="Original", expected_digest=None)
    wrong = call(
        "harness_write_file", path="plan.md", content="Unsafe replacement", expected_digest=None
    )
    steps = (
        [wrong] * 6
        if repeat_conflict
        else [
            call("harness_read_file", path="plan.md", start_line=1, max_lines=10),
            call(
                "harness_write_file",
                path="plan.md",
                content="Reviewed revision",
                expected_digest=hashlib.sha256(b"Original").hexdigest(),
            ),
            "Saved.",
        ]
    )
    model = Model(initial, wrong, *steps)
    local = Store(store.engine, settings)
    runner = Runner(local, model)
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    row = local.get(row["id"])
    error = json.loads(model.requests[2]["messages"][-1]["content"])
    assert error["error"]["code"] == "workspace_conflict"
    file = row["state"]["files"]["plan.md"]
    assert file["content"] == ("Original" if repeat_conflict else "Reviewed revision")
    assert file["version"] == (1 if repeat_conflict else 2)
    assert row["state"]["status"] == ("failed" if repeat_conflict else "completed")
    assert row["state"]["run"]["steps"] <= 5
    assert any(e["kind"] == "native.rejected" for e in local.events(row["id"]))
