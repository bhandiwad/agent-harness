"""Fixed isolated-execution profiles. No model-selected images, hosts, mounts or privileges."""

from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from pandita_agent.agent.native_tools import PATH, schema, valid_path
from pandita_agent.bindings import ModelBinding
from pandita_agent.contracts import Contract


class SandboxBinding(Contract):
    endpoint: str
    kind: Literal["shell", "python", "browser"]

    @model_validator(mode="after")
    def endpoint_valid(self):
        ModelBinding.explicit_endpoint(self.endpoint)
        return self


FILES = {"type": "array", "items": PATH, "maxItems": 16, "uniqueItems": True}
CODE_SCHEMA = schema(
    {
        "code": {"type": "string", "minLength": 1, "maxLength": 16384},
        "files": FILES,
        "output_files": FILES,
    },
    ["code", "files", "output_files"],
)
BROWSER_SCHEMA = schema(
    {
        "html_path": PATH,
        "files": FILES,
        "output_files": FILES,
        "steps": {
            "type": "array",
            "maxItems": 20,
            "items": schema(
                {
                    "action": {"type": "string", "enum": ["click", "fill", "text"]},
                    "selector": {"type": "string", "minLength": 1, "maxLength": 256},
                    "value": {"type": "string", "maxLength": 2048},
                },
                ["action", "selector", "value"],
            ),
        },
    },
    ["html_path", "files", "output_files", "steps"],
)
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "operation_id": {"type": "string"},
        "status": {"type": "string", "enum": ["completed", "failed", "unknown"]},
        "exit_code": {"type": ["integer", "null"]},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "files": {"type": "object", "additionalProperties": {"type": "string"}},
        "isolation": {"type": "object"},
    },
    "required": ["operation_id", "status", "exit_code", "stdout", "stderr", "files", "isolation"],
    "additionalProperties": False,
}


class JobScope(Contract):
    tenant_id: UUID
    workspace_id: UUID
    subject: str = Field(min_length=1, max_length=256)
    task_id: UUID
    plan_revision: int = Field(ge=1)


class JobArguments(Contract):
    kind: Literal["shell", "python", "browser"]
    request: dict
    files: dict[str, str] = Field(max_length=16)

    @model_validator(mode="after")
    def bounded(self):
        from pandita_agent.governance.tools import validate_value

        validate_value(self.request, BROWSER_SCHEMA if self.kind == "browser" else CODE_SCHEMA)
        expected = set(self.request["files"])
        if self.kind == "browser":
            expected.add(self.request["html_path"])
        if set(self.files) != expected:
            raise ValueError("Sandbox file inventory mismatch.")
        for path in (*self.files, *self.request["output_files"]):
            valid_path(path)
            if path.startswith(".harness"):
                raise ValueError("Reserved sandbox path.")
        if sum(len(value.encode()) for value in self.files.values()) > 131072:
            raise ValueError("Sandbox inputs exceed their bound.")
        return self


class Job(Contract):
    version: Literal[1]
    operation_id: UUID
    scope: JobScope
    arguments: JobArguments


class BrokerSettings(Contract):
    tenant_id: UUID
    workspace_id: UUID
    issuer: str
    audience: str = "pandita-agent-sandbox"
    jwks_file: Path
    policy_file: Path
    state_dir: Path
    docker_binary: Path = Path("/usr/bin/docker")
    docker_host: str = Field(
        default="unix:///run/user/1000/docker.sock", pattern=r"^unix:///[^\r\n]+$"
    )
    runtime: Literal["runsc", "runc"] = "runsc"
    allow_standard_runtime: bool = False
    require_landlock: bool = True
    images: dict[Literal["shell", "python", "browser"], str]
    max_seconds: int = Field(default=30, ge=1, le=120)
    memory_mb: int = Field(default=512, ge=128, le=4096)
    pids: int = Field(default=128, ge=16, le=512)
    concurrency: int = Field(default=2, ge=1, le=8)

    @model_validator(mode="after")
    def pinned(self):
        import re

        if self.runtime == "runc" and not self.allow_standard_runtime:
            raise ValueError("Standard OCI runtime requires explicit lower-assurance opt-in.")
        if not self.images or any(
            not re.fullmatch(r"(?:[a-zA-Z0-9_./:-]+@)?sha256:[a-f0-9]{64}", image)
            for image in self.images.values()
        ):
            raise ValueError("Sandbox images must be pinned by content digest.")
        if not self.docker_binary.is_absolute() or not self.state_dir.is_absolute():
            raise ValueError("Broker paths must be absolute.")
        return self
