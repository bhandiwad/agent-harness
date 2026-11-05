"""The sample selects validated operator configuration without exposing model credentials."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent))
import run
from demo import load_model


def test_profile_selection_and_validation(tmp_path):
    path = tmp_path / "model.json"
    data = {
        "model_ref": "qwen",
        "model_name": "Qwen/Qwen3.8-27B",
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key_env": "MODEL_API_KEY",
        "execution": {"id": "qwen-v1", "family": "qwen3_8", "enable_thinking": False},
    }
    path.write_text(json.dumps(data))
    model = load_model({"DEMO_MODEL_CONFIG": str(path), "DEMO_MODEL": "ignored"})
    assert model.model_ref == "qwen" and model.execution.enable_thinking is False
    path.write_text(json.dumps({**data, "api_key_env": "OTHER_KEY"}))
    with pytest.raises(ValueError, match="only MODEL_API_KEY"):
        load_model({"DEMO_MODEL_CONFIG": str(path)})
    path.write_text(json.dumps({**data, "execution": {"id": "bad", "family": "unknown"}}))
    with pytest.raises(ValidationError):
        load_model({"DEMO_MODEL_CONFIG": str(path)})
    assert load_model({"DEMO_MODEL": "custom"}).model_name == "custom"


def test_launcher_mounts_profile_read_only_and_cleans_own_project(tmp_path, monkeypatch):
    profile, secret, output = tmp_path / "profile.json", tmp_path / "model.env", tmp_path / "output"
    profile.write_text("{}")
    secret.write_text("MODEL_API_KEY=fixture")
    calls = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--model-config",
            str(profile),
            "--model-env",
            str(secret),
            "--output",
            str(output),
            "--docker",
            "/fixture/docker",
        ],
    )
    monkeypatch.setattr(
        run.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setenv("UNRELATED_SECRET", "not-for-child")
    assert run.main() == 0
    override = json.loads((output / "model-compose.json").read_text())["services"]["demo"]
    assert override["environment"] == {"DEMO_MODEL_CONFIG": "/sample-model.json"}
    assert override["volumes"] == [
        {"type": "bind", "source": str(profile), "target": "/sample-model.json", "read_only": True}
    ]
    assert len(calls) == 3
    assert calls[-1][0][-3:] == ["down", "--volumes", "--remove-orphans"]
    for command, kwargs in calls:
        assert str(output / "model-compose.json") in command
        assert "UNRELATED_SECRET" not in kwargs["env"]
        assert "MODEL_API_KEY" not in kwargs["env"]
