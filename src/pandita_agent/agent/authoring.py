"""Operator-side authoring helpers. Saving a configuration never installs or grants it."""

from pathlib import Path

from pandita_agent.agent.capabilities import (
    Capabilities,
    InstructionTree,
    instruction_digest,
    instruction_files,
    read_instructions,
)
from pandita_agent.agent.contracts import Connection, Settings, Skill
from pandita_agent.agent.mcp import McpBinding
from pandita_agent.governance.skills import inspect_skill
from pandita_agent.governance.tools import RestBinding, ToolSpec


def pin_skill(path, *, version="v1", required_tools=(), permission=None):
    root = Path(path).absolute()
    inspected = inspect_skill(root)
    return Skill(
        path=root,
        digest=inspected.digest,
        version=version,
        required_tools=tuple(required_tools),
        permission=permission,
    )


def pin_instructions(root, *, directory="."):
    root = Path(root).absolute()
    return InstructionTree(
        root=root,
        directory=directory,
        digest=instruction_digest(instruction_files(root, directory)),
    )


def rest_tool(
    *,
    name,
    endpoint,
    operation_path,
    input_schema,
    output_schema,
    permission,
    owner,
    version="v1",
    effect="read",
    status_path=None,
    subject_token_files=None,
):
    return Connection(
        tool=ToolSpec(
            name=name,
            version=version,
            owner=owner,
            transport="rest",
            effect=effect,
            permission=permission,
            input_schema=input_schema,
            output_schema=output_schema,
            approval_required=effect == "write",
        ),
        binding=RestBinding(
            endpoint=endpoint, operations={name: operation_path}, status_path=status_path
        ),
        subject_token_files=subject_token_files or {},
    )


def mcp_tool(
    *,
    name,
    endpoint,
    remote_name,
    input_schema,
    output_schema,
    permission,
    owner,
    version="v1",
    effect="read",
    receipt_tool=None,
    subject_token_files=None,
):
    return Connection(
        tool=ToolSpec(
            name=name,
            version=version,
            owner=owner,
            transport="mcp",
            effect=effect,
            permission=permission,
            input_schema=input_schema,
            output_schema=output_schema,
            approval_required=effect == "write",
        ),
        binding=McpBinding(endpoint=endpoint, tool_name=remote_name, receipt_tool=receipt_tool),
        subject_token_files=subject_token_files or {},
    )


def write_config(settings, path):
    """Validate and exclusively save a new config; relative paths are relative to its folder."""
    settings = Settings.model_validate_json(settings.model_dump_json())
    destination = Path(path).absolute()
    base = destination.parent
    connections = {c.tool.name: c for c in settings.connections}
    for agent in settings.agents:
        resolved = agent.model_copy(
            update={
                "skills": tuple(s.model_copy(update={"path": base / s.path}) for s in agent.skills),
                "instruction_tree": agent.instruction_tree.model_copy(
                    update={"root": base / agent.instruction_tree.root}
                )
                if agent.instruction_tree
                else None,
            }
        )
        Capabilities(resolved, connections, lambda actor, permission: None, "author")
        if resolved.instruction_tree:
            read_instructions(resolved.instruction_tree)
    encoded = settings.model_dump_json(indent=2) + "\n"
    if len(encoded.encode()) > 262144:
        raise ValueError("Configuration exceeds its byte budget.")
    with destination.open("x", encoding="utf-8") as output:
        output.write(encoded)
    return destination
