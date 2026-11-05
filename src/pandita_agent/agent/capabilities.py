"""Reviewed instruction scopes and lazily activated skills; files never grant authority."""

import hashlib
from pathlib import Path, PurePosixPath

from pydantic import Field

from pandita_agent.agent.native_tools import ToolInputError
from pandita_agent.contracts import Contract, Digest, content_digest
from pandita_agent.files import read_bounded
from pandita_agent.governance.skills import SkillCatalog, inspect_skill, safe_path
from pandita_agent.runtime.auth import Forbidden


class InstructionTree(Contract):
    root: Path
    directory: str = Field(default=".", max_length=256)
    digest: Digest


def instruction_files(root, directory="."):
    root = Path(root)
    relative = PurePosixPath(directory)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or str(relative) != directory
        or "\\" in directory
    ):
        raise ValueError("Instruction scope must be a normalized relative directory.")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Instruction root must be a regular directory.")
    # Do not traverse a symlink in an operator-provided root or selected scope.
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("Instruction path contains a symlink.")
    directories = [root]
    for part in relative.parts:
        directories.append(directories[-1] / part)
        if directories[-1].is_symlink() or not directories[-1].is_dir():
            raise ValueError("Instruction scope is unavailable.")
    if len(directories) > 16:
        raise ValueError("Instruction scope exceeds its depth limit.")
    files = {}
    total = 0
    for folder in directories:
        path = folder / "AGENTS.md"
        if path.is_symlink():
            raise ValueError("Instruction symlinks are unsupported.")
        name = path.relative_to(root).as_posix()
        if not path.exists():
            files[name] = None  # Addition of a previously absent file also changes the pin.
            continue
        value = read_bounded(path, 32768).decode("utf-8")
        total += len(value.encode())
        if total > 65536 or "\0" in value:
            raise ValueError("Scoped instructions exceed their text budget.")
        files[name] = value
    return files


def instruction_digest(files):
    return content_digest(
        {
            name: hashlib.sha256(value.encode()).hexdigest() if value is not None else None
            for name, value in files.items()
        }
    )


def read_instructions(pin):
    files = instruction_files(pin.root, pin.directory)
    if instruction_digest(files) != pin.digest:
        raise ValueError("Scoped instructions changed; repin and start a new session.")
    return "\n".join(
        f"Project guidance ({name}):\n{value}" for name, value in files.items() if value is not None
    )


class Capabilities:
    def __init__(self, agent, connections, authorize, actor):
        self.agent = agent
        self.skills = {}
        self.pins = {}
        for pin in agent.skills:
            skill = inspect_skill(pin.path)
            if skill.digest != pin.digest:
                raise ValueError("Pinned skill changed.")
            name = skill.metadata.name
            if name in self.skills:
                raise ValueError("Skill names must be unique within an agent.")
            try:
                if pin.permission:
                    authorize(actor, pin.permission)
                for tool_name in pin.required_tools:
                    if tool_name in connections:
                        authorize(actor, connections[tool_name].tool.permission)
                    else:
                        from pandita_agent.agent.native_tools import NATIVE

                        permission = NATIVE[tool_name]["permission"]
                        if permission:
                            authorize(actor, permission)
            except Forbidden:
                continue
            self.skills[name] = skill
            self.pins[name] = pin
        self.catalog = SkillCatalog(
            {name: (pin.path, pin.digest) for name, pin in self.pins.items()},
            max_active=agent.max_active_skills,
        )

    def descriptions(self):
        return [
            {
                "name": name,
                "description": skill.metadata.description,
                "version": self.pins[name].version,
                "required_tools": list(self.pins[name].required_tools),
            }
            for name, skill in self.skills.items()
        ]

    def active(self, frame):
        names = (
            tuple(self.skills)
            if self.agent.skill_mode == "eager"
            else tuple(frame.get("active_skills", []))
        )
        if self.agent.skill_mode == "eager":
            return tuple(self.skills.values())
        return self.catalog.activate(names, "Restore selected skills for this execution frame")

    def select(self, names, reason):
        if self.agent.skill_mode != "on_demand":
            raise ValueError("Skill selection requires on-demand mode.")
        self.catalog.activate(tuple(names), reason)
        return [
            {
                "name": name,
                "version": self.pins[name].version,
                "digest": self.pins[name].digest,
                "resources": [p for p in self.skills[name].files if not p.startswith("scripts/")],
            }
            for name in names
        ]

    def resource(self, frame, name, path):
        if name not in {skill.metadata.name for skill in self.active(frame)}:
            raise ToolInputError("Select a permitted skill before reading its resources.")
        if not safe_path(path) or path.startswith("scripts/"):
            raise ValueError("Skill resource path is not permitted.")
        if path not in self.skills[name].files:
            available = [p for p in self.skills[name].files if not p.startswith("scripts/")]
            raise ToolInputError(
                "Resource not found. Available paths: " + ", ".join(available)[:400]
            )
        return self.catalog.resource(name, path)
