"""Local sample operator controls; model credentials remain in process memory only."""

import hashlib
import json
import time
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import UUID, uuid4

from pandita_agent.agent.sdk import Client, Model, Settings, write_config
from pandita_agent.agent.storage import Store
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent
IDLE = {"idle", "completed", "failed", "cancelled", "interrupted", "expired"}


class SetupError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def validate_model(value):
    try:
        model = Model.model_validate_json(json.dumps(value))
        if model.protocol != "openai_chat" or model.api_key_env != "MODEL_API_KEY":
            raise ValueError()
        return model
    except Exception:
        raise SetupError(
            "Invalid profile. Use a valid Chat Completions Model JSON with api_key_env MODEL_API_KEY. Remote endpoints require HTTPS and must not contain credentials or query parameters."
        ) from None


class ModelSetup:
    def __init__(self, settings, services, database_url, token, output):
        self.settings, self.services, self.token, self.output = settings, services, token, output
        self.engine = create_engine(database_url, hide_parameters=True)
        self.applied = {}

    def snapshot(self):
        try:
            runtime_version = version("pandita-agent")
        except PackageNotFoundError:
            runtime_version = tomllib.loads((ROOT.parent.parent / "pyproject.toml").read_text())["project"]["version"]
        folder = Path("/config/model-profiles")
        if not folder.is_dir():
            folder = ROOT.parent.parent / "config" / "model-profiles"
        presets = [
            Model.model_validate_json(p.read_bytes()).model_dump(mode="json", exclude_none=True)
            for p in sorted(folder.glob("*.json"))
        ]
        return {
            "version": runtime_version,
            "current": self.settings.models[0].model_dump(mode="json", exclude_none=True),
            "presets": presets,
            "revision": self.settings.digest,
            "output_tokens": self.settings.agents[0].capacity.output_tokens,
        }

    def ready(self):
        deadline = time.monotonic() + 15
        with Client("http://127.0.0.1:8093", self.token, timeout=2) as client:
            while True:
                try:
                    client.agents()
                    if self.services.worker.poll() is not None:
                        raise SetupError("The worker did not start.", 503)
                    return
                except Exception:
                    if time.monotonic() >= deadline:
                        raise SetupError("The local harness did not restart.", 503) from None
                    time.sleep(0.2)

    def apply(self, data):
        try:
            if set(data) - {
                "command_id",
                "expected_revision",
                "model",
                "api_key",
                "reuse_key",
                "no_auth",
            }:
                raise ValueError()
            command = str(UUID(data["command_id"]))
            model = validate_model(data["model"])
            key = data.get("api_key", "")
            if not isinstance(key, str) or len(key) > 8192 or any(c in key for c in ("\r", "\n")):
                raise ValueError()
            if any(type(data.get(k, False)) is not bool for k in ("reuse_key", "no_auth")):
                raise ValueError()
            if model.protocol != "openai_chat" or model.api_key_env != "MODEL_API_KEY":
                raise ValueError()
        except Exception:
            raise SetupError(
                "Invalid profile. Use a valid Chat Completions Model JSON with api_key_env MODEL_API_KEY."
            ) from None
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        if command in self.applied:
            if self.applied[command] != fingerprint:
                raise SetupError("This command was already used with different settings.", 409)
            return self.snapshot()
        if data.get("expected_revision") != self.settings.digest:
            raise SetupError("Model setup changed. Reload setup before applying.", 409)
        if sum([bool(key), data.get("reuse_key", False), data.get("no_auth", False)]) != 1:
            raise SetupError(
                "Choose exactly one: a new API key, current credential, or no authentication."
            )
        if data.get("reuse_key"):
            if model.base_url != self.settings.models[0].base_url:
                raise SetupError(
                    "Current credential can only be reused at the exact same endpoint."
                )
            key = self.services.model_key
        elif data.get("no_auth"):
            key = "local-no-auth"
        with Store(self.engine, self.settings).transaction() as conn:
            states = conn.execute(text("SELECT state->>'status' FROM agent.sessions")).scalars()
            if any(state not in IDLE for state in states):
                raise SetupError(
                    "Finish or stop all running or waiting conversations before changing the model.",
                    409,
                )
        agent = self.settings.agents[0]
        agent = agent.model_copy(
            update={
                "model": model.model_ref,
                "capacity": agent.capacity.model_copy(
                    update={
                        "output_tokens": 8192
                        if model.execution and model.execution.enable_thinking
                        else 2048
                    }
                ),
            }
        )
        updated = Settings.model_validate_json(
            self.settings.model_copy(
                update={"models": (model,), "agents": (agent,)}
            ).model_dump_json()
        )
        config = write_config(
            updated, self.services.config.parent / ("profile-" + uuid4().hex + ".json")
        )
        old_config, old_key = self.services.config, self.services.model_key
        self.services.close()
        try:
            self.services.config, self.services.model_key = config, key
            self.services.start("api")
            self.services.start("worker")
            self.ready()
        except Exception:
            self.services.close()
            self.services.config, self.services.model_key = old_config, old_key
            self.services.start("api")
            self.services.start("worker")
            self.ready()
            raise SetupError(
                "The new setup failed. The previous setup has been restored.", 503
            ) from None
        self.settings = updated
        self.applied[command] = fingerprint
        if len(self.applied) > 32:
            del self.applied[next(iter(self.applied))]
        # Non-secret portable profile. Credentials are never written with the configuration.
        (self.output / "active-model.json").write_text(model.model_dump_json(indent=2))
        return self.snapshot()
