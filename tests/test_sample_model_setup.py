"""Sample operator setup boundaries and actual service restart, separate from live inference."""

import sys
from pathlib import Path
from uuid import uuid4

import pytest

from pandita_agent.agent.sdk import Client, write_config
from pandita_agent.agent.storage import Store

source = (
    Path(__file__).parents[2] / "src/pandita_agent/generic_template/examples/maintenance_agent"
)
if not source.exists():
    source = Path(__file__).parents[1] / "examples/maintenance_agent"
sys.path.insert(0, str(source))
from demo import Services  # noqa: E402
from model_setup import ModelSetup, SetupError  # noqa: E402


def setup_manager(store, identity, tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "fixture-original")
    agent = store.settings.agents[0].model_copy(update={"delegates": ()})
    settings = store.settings.model_copy(update={"agents": (agent,)})
    identity()  # Establish the local test identity files.
    config = write_config(settings, tmp_path / "initial.json")
    services = Services(config, store.engine.url.render_as_string(hide_password=False))
    manager = ModelSetup(settings, services, services.env["AGENT_DATABASE_URL"], identity, tmp_path)
    return manager


def payload(manager, **changes):
    model = manager.settings.models[0].model_dump(mode="json", exclude_none=True)
    model.update(
        model_ref="qwen-custom",
        model_name="custom-served-qwen",
        api_key_env="MODEL_API_KEY",
        execution={"id": "custom-thinking", "family": "qwen3_8"},
    )
    return {
        "command_id": str(uuid4()),
        "expected_revision": manager.settings.digest,
        "model": model,
        "api_key": "fixture-new",
        **changes,
    }


def test_setup_rejects_cross_endpoint_key_reuse_and_invalid_config(
    store, identity, tmp_path, monkeypatch
):
    manager = setup_manager(store, identity, tmp_path, monkeypatch)
    body = payload(manager, api_key="", reuse_key=True)
    body["model"]["base_url"] = "https://another.invalid/v1"
    with pytest.raises(SetupError, match="same endpoint"):
        manager.apply(body)
    body = payload(manager, expected_revision="stale")
    with pytest.raises(SetupError, match="changed"):
        manager.apply(body)
    body = payload(manager)
    body["model"]["api_key"] = "must-not-be-saved"
    with pytest.raises(SetupError, match="Invalid profile"):
        manager.apply(body)
    assert manager.services.children == []
    assert not (tmp_path / "active-model.json").exists()
    manager.engine.dispose()


def test_setup_waiting_conversation_blocks_restart(store, identity, tmp_path, monkeypatch):
    manager = setup_manager(store, identity, tmp_path, monkeypatch)
    from pandita_agent.agent.runner import Runner

    from .test_runner import Model, call, new, send

    runner = Runner(
        Store(store.engine, manager.settings), Model(call("harness_ask_user", question="Crew?"))
    )
    row = new(runner)
    send(runner, row)
    # Queued work is sufficient to block setup; it cannot race a worker startup.
    with pytest.raises(SetupError, match="Finish or stop"):
        manager.apply(payload(manager))
    assert manager.services.children == []
    manager.engine.dispose()


def test_setup_real_api_worker_restart_and_idempotent_retry(store, identity, tmp_path, monkeypatch):
    manager = setup_manager(store, identity, tmp_path, monkeypatch)
    try:
        manager.services.start("api")
        manager.services.start("worker")
        manager.ready()
        with Client("http://127.0.0.1:8093", identity) as client:
            old = client.create_session("assistant", "Preserve me")
        body = payload(manager)
        result = manager.apply(body)
        assert result["current"]["execution"]["enable_thinking"] is True
        assert result["output_tokens"] == 8192
        assert manager.services.model_key == "fixture-new"
        assert "MODEL_API_KEY" not in manager.services.env
        pids = [p.pid for p in manager.services.children]
        assert manager.apply(body) == result
        assert [p.pid for p in manager.services.children] == pids
        assert "fixture-new" not in (tmp_path / "active-model.json").read_text()
        with Client("http://127.0.0.1:8093", identity) as client:
            assert client.session(old.id).id == old.id
            fresh = client.create_session("assistant", "New model")
            assert fresh.state["config_digest"] == manager.settings.digest
        different = {**body, "api_key": "changed-secret"}
        with pytest.raises(SetupError, match="different settings"):
            manager.apply(different)
    finally:
        manager.services.close()
        manager.engine.dispose()


def test_setup_failed_start_restores_previous_config_and_key(
    store, identity, tmp_path, monkeypatch
):
    manager = setup_manager(store, identity, tmp_path, monkeypatch)
    original = manager.services.config
    original_settings = manager.settings
    starts = []
    monkeypatch.setattr(manager.services, "close", lambda: None)

    def start(kind):
        starts.append(kind)
        if len(starts) == 1:
            raise RuntimeError("Synthetic startup failure")

    monkeypatch.setattr(manager.services, "start", start)
    monkeypatch.setattr(manager, "ready", lambda: None)
    with pytest.raises(SetupError, match="restored"):
        manager.apply(payload(manager))
    assert manager.services.config == original and manager.services.model_key == "fixture-original"
    assert manager.settings == original_settings and manager.applied == {}
    manager.engine.dispose()
