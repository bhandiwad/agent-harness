from pathlib import Path
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import Field, model_validator

from pandita_agent.agent.capabilities import InstructionTree
from pandita_agent.agent.code_contracts import CodeProject
from pandita_agent.agent.team_contracts import TeamMember
from pandita_agent.agent.execution import ExecutionProfile
from pandita_agent.agent.mcp import McpBinding
from pandita_agent.agent.native_tools import NATIVE
from pandita_agent.agent.openconnector import OpenConnectorBinding
from pandita_agent.agent.sandbox_contracts import (
    BROWSER_SCHEMA,
    CODE_SCHEMA,
    RESULT_SCHEMA,
    SandboxBinding,
)
from pandita_agent.bindings import ModelBinding
from pandita_agent.contracts import Contract, Identifier, NonemptyText, content_digest
from pandita_agent.files import read_bounded
from pandita_agent.governance.context import Capacity
from pandita_agent.governance.tools import RestBinding, ToolSpec, validate_schema


class Model(ModelBinding):
    input_cost_per_million: float | None = Field(
        default=None, ge=0, exclude_if=lambda v: v is None
    )
    output_cost_per_million: float | None = Field(
        default=None, ge=0, exclude_if=lambda v: v is None
    )
    protocol: Literal["openai_chat", "anthropic_messages", "openai_responses"] = (
        "openai_chat"
    )
    stream: bool = False
    execution: ExecutionProfile | None = None
    request_timeout_seconds: int = Field(default=120, ge=1, le=600)
    reasoning_context: Literal["auto", "current_turn", "all_turns"] = "auto"
    compact_threshold: int | None = Field(default=None, ge=1024, le=1900000)

    @model_validator(mode="after")
    def native_options(self):
        if self.execution is not None and self.protocol != "openai_chat":
            raise ValueError(
                "vLLM execution profiles require the Chat Completions protocol."
            )
        if self.protocol != "openai_responses" and (
            self.compact_threshold is not None or self.reasoning_context != "auto"
        ):
            raise ValueError(
                "Native reasoning and compaction require the Responses protocol."
            )
        return self


class Skill(Contract):
    version: Identifier = "v1"
    required_tools: tuple[Identifier, ...] = Field(default=(), max_length=32)
    permission: str | None = Field(default=None, min_length=1, max_length=128)
    path: Path
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class Agent(Contract):
    team_execution: Literal["legacy", "nested"] = Field(
        default="legacy", exclude_if=lambda v: v == "legacy"
    )
    native_definitions: ClassVar[dict] = NATIVE
    team: tuple[TeamMember, ...] = Field(
        default=(), max_length=6, exclude_if=lambda v: not v
    )
    code: CodeProject | None = Field(default=None, exclude_if=lambda v: v is None)
    id: Identifier
    instructions: NonemptyText = Field(max_length=16384)
    model: Identifier
    tools: tuple[Identifier, ...] = Field(default=(), max_length=64)
    native_tools: tuple[Identifier, ...] = ("harness_calculate", "harness_time")
    delegates: tuple[Identifier, ...] = Field(default=(), max_length=16)
    skills: tuple[Skill, ...] = Field(default=(), max_length=16)
    skill_mode: Literal["on_demand", "eager"] = "on_demand"
    max_active_skills: int = Field(default=4, ge=1, le=16)
    instruction_tree: InstructionTree | None = None
    capacity: Capacity
    max_steps: int = Field(default=16, ge=1, le=100)
    max_tokens: int = Field(default=32768, ge=1024, le=1000000)
    max_seconds: int = Field(default=300, ge=1, le=3600)
    output_schema: dict | None = None
    completion_check: Identifier | None = None
    max_repairs: int = Field(default=3, ge=0, le=8)

    @model_validator(mode="after")
    def output_contract(self):
        if not set(self.native_tools) <= self.native_definitions.keys():
            raise ValueError("Unknown native tool.")
        if any(
            not set(skill.required_tools) <= set(self.tools) | set(self.native_tools)
            for skill in self.skills
        ):
            raise ValueError(
                "Skill requirements must be explicitly enabled agent tools."
            )
        if self.output_schema is not None:
            validate_schema(self.output_schema)
        if self.capacity.output_tokens > self.max_tokens:
            raise ValueError("Output reservation exceeds the run token budget.")
        return self


class Connection(Contract):
    tool: ToolSpec
    binding: RestBinding | McpBinding | SandboxBinding | OpenConnectorBinding
    subject_token_files: dict[str, Path] = Field(default_factory=dict)

    @model_validator(mode="after")
    def fixed_mapping(self):
        if (
            self.tool.transport == "rest"
            and isinstance(self.binding, RestBinding)
            and self.tool.name in self.binding.operations
        ):
            pass
        elif self.tool.transport == "openconnector" and isinstance(
            self.binding, OpenConnectorBinding
        ):
            pass
        elif self.tool.transport == "mcp" and isinstance(self.binding, McpBinding):
            if self.tool.effect == "write" and not self.binding.receipt_tool:
                raise ValueError("MCP write tools require an explicit receipt tool.")
        elif self.tool.transport == "sandbox" and isinstance(
            self.binding, SandboxBinding
        ):
            expected = BROWSER_SCHEMA if self.binding.kind == "browser" else CODE_SCHEMA
            if (
                self.tool.effect != "read"
                or self.tool.input_schema != expected
                or self.tool.output_schema != RESULT_SCHEMA
            ):
                raise ValueError(
                    "Sandbox tools use fixed schemas and private workspace effects only."
                )
        else:
            raise ValueError("Configure a fixed binding for the tool transport.")
        if self.tool.name.startswith("harness_"):
            raise ValueError("Internal tool names are reserved.")
        return self


class Settings(Contract):
    tenant_id: UUID
    workspace_id: UUID
    issuer: NonemptyText
    audience: NonemptyText = "pandita-agent"
    jwks_file: Path
    policy_file: Path
    database_url_env: str = "AGENT_DATABASE_URL"
    models: tuple[Model, ...] = Field(min_length=1, max_length=16)
    agents: tuple[Agent, ...] = Field(min_length=1, max_length=64)
    connections: tuple[Connection, ...] = Field(default=(), max_length=64)
    approval_seconds: int = Field(default=3600, ge=60, le=86400)
    max_active_sessions: int = Field(default=100, ge=1, le=10000)

    @model_validator(mode="after")
    def catalog(self):
        models = {model.model_ref for model in self.models}
        agents = {agent.id for agent in self.agents}
        tools = {connection.tool.name for connection in self.connections}
        if (
            len(models) != len(self.models)
            or len(agents) != len(self.agents)
            or len(tools) != len(self.connections)
        ):
            raise ValueError("Catalog identifiers must be unique.")
        from pandita_agent.agent.teams import walk_members

        all_members = [
            (agent, path, member)
            for agent in self.agents
            for path, member in walk_members(agent)
        ]
        if any(
            sum(root is agent for root, _, _ in all_members) > 24
            for agent in self.agents
        ):
            raise ValueError("A team supports at most 24 members in total.")
        for root, _, member in all_members:
            if member.team and root.team_execution != "nested":
                raise ValueError("Nested teams require nested execution mode.")
            if member.model_ref and member.model_ref not in models:
                raise ValueError("A specialist references an unconfigured model.")
            if not set(member.tools) <= tools:
                raise ValueError("A specialist references an unconfigured tool.")
        generated = [
            root.id + "".join("_s" + str(i) for i in path)
            for root, path, _ in all_members
        ]
        if len(set(generated)) != len(generated) or set(generated) & agents:
            raise ValueError(
                "Generated specialist identifiers must not shadow installed agents."
            )
        for agent in self.agents:
            if any(not set(member.tools) <= tools for member in agent.team):
                raise ValueError("A specialist references an unconfigured tool.")
            model = (
                next(model for model in self.models if model.model_ref == agent.model)
                if agent.model in models
                else None
            )
            if (
                model
                and model.compact_threshold is not None
                and model.compact_threshold
                + agent.capacity.output_tokens
                + agent.capacity.safety_tokens
                >= agent.capacity.context_tokens
            ):
                raise ValueError(
                    "Native compaction threshold must leave output and safety headroom."
                )
            if (
                agent.model not in models
                or not set(agent.tools) <= tools
                or not set(agent.delegates) <= agents
            ):
                raise ValueError("An agent references an unconfigured capability.")
        return self

    @property
    def digest(self):
        return content_digest(self.model_dump(mode="json"))


def load_settings(path: Path, settings_type=Settings):
    settings = settings_type.model_validate_json(read_bounded(path, 262144))
    base = path.resolve().parent
    return settings.model_copy(
        update={
            "jwks_file": (base / settings.jwks_file).resolve(),
            "policy_file": (base / settings.policy_file).resolve(),
            "agents": tuple(
                agent.model_copy(
                    update={
                        "instruction_tree": agent.instruction_tree.model_copy(
                            update={
                                "root": (base / agent.instruction_tree.root).absolute()
                            }
                        )
                        if agent.instruction_tree
                        else None,
                        "skills": tuple(
                            skill.model_copy(
                                update={"path": (base / skill.path).resolve()}
                            )
                            for skill in agent.skills
                        ),
                    }
                )
                for agent in settings.agents
            ),
            "connections": tuple(
                connection.model_copy(
                    update={
                        "subject_token_files": {
                            subject: (base / value).resolve()
                            for subject, value in connection.subject_token_files.items()
                        },
                    }
                )
                for connection in settings.connections
            ),
        }
    )


class CreateSession(Contract):
    command_id: UUID
    agent_id: Identifier
    title: NonemptyText = Field(default="New conversation", max_length=160)


class SourceReference(Contract):
    reference: NonemptyText = Field(max_length=2048)
    version: NonemptyText = Field(max_length=256)


class ContextAccessRequest(Contract):
    sources: tuple[SourceReference, ...] = Field(min_length=1, max_length=100)


class Command(Contract):
    command_id: UUID
    expected_revision: int = Field(ge=1)


class Turn(Command):
    text: NonemptyText = Field(max_length=32768)


class Decision(Command):
    action_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    approve: bool
    reason: NonemptyText = Field(max_length=2048)


class Memory(Command):
    facts: dict[Identifier, str] = Field(max_length=32)

    @model_validator(mode="after")
    def bounded(self):
        if sum(len(value.encode()) for value in self.facts.values()) > 8192:
            raise ValueError("Pinned memory exceeds its budget.")
        return self


class Upload(Command):
    path: str = Field(pattern=r"^[a-zA-Z0-9_./-]{1,160}$")
    content: str = Field(max_length=32768)
    expected_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class Call(Contract):
    name: Identifier
    arguments: dict


class ModelResult(Contract):
    text: str = Field(default="", max_length=65536)
    call: Call | None = None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    reasoning: str = Field(default="", max_length=65536, repr=False)
    provider_context: dict | None = None
    provider_call_id: str | None = None
    effective_reasoning_context: str | None = None
