import httpx2
import pytest

from pandita_agent.agent.api import create_app
from pandita_agent.agent.authoring import pin_instructions, pin_skill, write_config
from pandita_agent.agent.capabilities import Capabilities, read_instructions
from pandita_agent.agent.contracts import Agent, Call, load_settings
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.sdk import AsyncClient
from pandita_agent.agent.storage import Store
from pandita_agent.runtime.auth import Forbidden

from .test_runner import Model, call, new, send


def package(tmp_path):
    root = tmp_path / "skills" / "asset-analysis"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: asset-analysis\ndescription: Compare asset facts and maintenance history.\n---\nPRIVATE_SKILL_BODY: distinguish source observations from assumptions.\n"
    )
    (root / "references").mkdir()
    (root / "references" / "guide.md").write_text(
        "RESOURCE_DETAIL: rows and excerpts need source versions."
    )
    (root / "scripts").mkdir()
    (root / "scripts" / "example.py").write_text('raise RuntimeError("Must never execute")')
    return root


def configured(store, tmp_path):
    pin = pin_skill(package(tmp_path), version="v2", required_tools=("context_lookup",))
    project = tmp_path / "project"
    (project / "area").mkdir(parents=True)
    (project / "AGENTS.md").write_text("ROOT_GUIDANCE: return source references.")
    (project / "area" / "AGENTS.md").write_text("LEAF_GUIDANCE: use the asset vocabulary.")
    agent = store.settings.agents[0].model_copy(
        update={"skills": (pin,), "instruction_tree": pin_instructions(project, directory="area")}
    )
    store.settings = store.settings.model_copy(update={"agents": (agent, store.settings.agents[1])})
    return pin, agent


async def test_progressive_skills_resource_followup_restart_and_unload(store, tmp_path):
    pin, agent = configured(store, tmp_path)
    model = Model(
        call("harness_select_skills", names=["asset-analysis"], reason="Need source comparison"),
        Call(
            name="harness_read_skill_resource",
            arguments={"name": "asset-analysis", "path": "references/guide.md"},
        ),
        "Compared.",
        call("harness_select_skills", names=[], reason="Finished comparison"),
        "Hello.",
    )
    runner = Runner(store, model)
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["status"] == "completed"
    first = model.requests[0]["instructions"]
    assert "Compare asset facts" in first and "PRIVATE_SKILL_BODY" not in first
    assert first.index("ROOT_GUIDANCE") < first.index("LEAF_GUIDANCE")
    assert "PRIVATE_SKILL_BODY" in model.requests[1]["instructions"]
    assert "RESOURCE_DETAIL" in str(model.requests[2]["messages"])
    assert row["state"]["run"]["frames"][0]["active_skills"] == ["asset-analysis"]
    runner = Runner(Store(store.engine, store.settings), model)
    send(runner, row, "Now say hello.")
    await runner.tick(row["id"])
    assert "PRIVATE_SKILL_BODY" in model.requests[3]["instructions"]
    assert "PRIVATE_SKILL_BODY" not in model.requests[4]["instructions"]
    assert store.get(row["id"])["state"]["run"]["frames"][0]["active_skills"] == []
    events = store.events(row["id"])
    assert sum(e["kind"] == "skills.selected" for e in events) == 2
    assert any(e["kind"] == "skill.resource_read" for e in events)


def test_instruction_additions_edits_and_symlinks_invalidate_pin(tmp_path):
    root = tmp_path / "project"
    (root / "child").mkdir(parents=True)
    (root / "AGENTS.md").write_text("Initial guidance")
    pin = pin_instructions(root, directory="child")
    assert "Initial guidance" in read_instructions(pin)
    (root / "child" / "AGENTS.md").write_text("New guidance")
    with pytest.raises(ValueError, match="changed"):
        read_instructions(pin)
    (root / "child" / "AGENTS.md").unlink()
    (root / "child" / "AGENTS.md").symlink_to(root / "AGENTS.md")
    with pytest.raises(ValueError, match="symlink"):
        pin_instructions(root, directory="child")
    with pytest.raises(ValueError):
        pin_instructions(root, directory="../outside")


def test_skill_permissions_resources_and_changed_packages(store, tmp_path):
    pin, agent = configured(store, tmp_path)
    connections = {c.tool.name: c for c in store.settings.connections}

    def deny(actor, permission):
        raise Forbidden()

    restricted = Capabilities(agent, connections, deny, "alice")
    assert restricted.descriptions() == []
    with pytest.raises(ValueError):
        restricted.select(["asset-analysis"], "Need it")
    allowed = Capabilities(agent, connections, lambda *_: None, "alice")
    with pytest.raises(ValueError):
        allowed.resource({}, "asset-analysis", "references/guide.md")
    frame = {"active_skills": ["asset-analysis"]}
    with pytest.raises(ValueError):
        allowed.resource(frame, "asset-analysis", "scripts/example.py")
    with pytest.raises(ValueError):
        allowed.resource(frame, "asset-analysis", "../SKILL.md")
    (pin.path / "SKILL.md").write_text((pin.path / "SKILL.md").read_text() + "Changed")
    with pytest.raises(ValueError, match="changed"):
        Capabilities(agent, connections, lambda *_: None, "alice")


async def test_changed_skill_and_revoked_selected_permission_stop_before_model(store, tmp_path):
    pin, agent = configured(store, tmp_path)
    model = Model(
        call("harness_select_skills", names=["asset-analysis"], reason="Analyze"), "Ready."
    )
    runner = Runner(store, model)
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    row = store.get(row["id"])
    policy = store.settings.policy_file
    policy.write_text(
        "\n".join(
            line for line in policy.read_text().splitlines() if not line.endswith(", tool:read")
        )
    )
    send(runner, row, "Continue")
    await runner.tick(row["id"])
    assert len(model.requests) == 2
    assert store.get(row["id"])["state"]["status"] == "failed"


async def test_capability_api_authoring_and_no_implicit_tool_grant(store, identity, tmp_path):
    pin, agent = configured(store, tmp_path)
    target = tmp_path / "new-config.json"
    write_config(store.settings, target)
    assert load_settings(target).digest == store.settings.digest
    with pytest.raises(FileExistsError):
        write_config(store.settings, target)
    bad = agent.model_dump(mode="json")
    bad["skills"][0]["required_tools"] = ["uninstalled_database"]
    with pytest.raises(ValueError, match="explicitly enabled"):
        Agent.model_validate_json(__import__("json").dumps(bad))
    async with AsyncClient(
        "http://127.0.0.1",
        identity(),
        transport=httpx2.ASGITransport(app=create_app(Runner(store, Model()))),
    ) as sdk:
        result = await sdk.capabilities("assistant")
        assert result["skills"][0]["version"] == "v2"
        assert result["instruction_digest"] == agent.instruction_tree.digest
        assert str(pin.path) not in str(result) and "PRIVATE_SKILL_BODY" not in str(result)


async def test_guidance_changed_while_awaiting_approval_prevents_external_write(store, tmp_path):
    from .test_runner import Adapter, approve

    pin, agent = configured(store, tmp_path)
    adapter = Adapter()
    runner = Runner(
        store,
        Model(call("create_case", query="A17 issue"), "Created"),
        adapters={"create_case": adapter},
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["status"] == "waiting_approval"
    approve(runner, row)
    (agent.instruction_tree.root / "AGENTS.md").write_text("Changed after review")
    await runner.tick(row["id"])
    assert adapter.calls == []
    assert store.get(row["id"])["state"]["status"] == "failed"
