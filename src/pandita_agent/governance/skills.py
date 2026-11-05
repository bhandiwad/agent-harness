"""Bounded standard skill packages; import and activation never execute scripts."""

import hashlib
from pathlib import Path, PurePosixPath
from typing import Annotated

import yaml
from pydantic import Field

from pandita_agent.contracts import Contract, Digest, Identifier, NonemptyText, content_digest
from pandita_agent.files import read_bounded

MAX_BYTES = 1048576
MAX_FILES = 128


class SkillMetadata(Contract):
    name: Identifier
    description: Annotated[NonemptyText, Field(max_length=1024)]


class Skill(Contract):
    metadata: SkillMetadata
    digest: Digest
    instructions: Annotated[NonemptyText, Field(max_length=32768)]
    files: dict[str, str]


def safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and all(part not in {".", "..", ""} for part in name.split("/"))
        and "\\" not in name
        and ":" not in name
        and "\x00" not in name
    )


def inspect_skill(root: Path) -> Skill:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("A skill must be a regular directory.")
    files, size, skill_bytes = {}, 0, None
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Skill symlinks are unsupported.")
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        if not safe_path(name) or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError("Invalid skill resource.")
        data = read_bounded(path, MAX_BYTES)
        size += len(data)
        if size > MAX_BYTES or len(files) >= MAX_FILES:
            raise ValueError("Skill package exceeds its resource budget.")
        files[name] = hashlib.sha256(data).hexdigest()
        if name == "SKILL.md":
            skill_bytes = data
    if skill_bytes is None:
        raise ValueError("SKILL.md is required.")
    raw = skill_bytes.decode("utf-8")
    if not raw.startswith("---\n") or "\n---\n" not in raw[4:]:
        raise ValueError("Skill metadata is required.")
    header, instructions = raw[4:].split("\n---\n", 1)
    # SafeLoader cannot construct Python objects. Reject aliases and duplicate keys explicitly.
    if len(header) > 4096:
        raise ValueError("Skill metadata exceeds its size limit.")
    if any(
        isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
        for token in yaml.scan(header)
    ):
        raise ValueError("Skill metadata must not use YAML aliases.")

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate skill metadata key.")
            return super().construct_mapping(node, deep=deep)

    metadata = yaml.load(header, Loader=UniqueLoader)
    if not isinstance(metadata, dict):
        raise ValueError("Invalid skill metadata.")
    if set(metadata) - {
        "name",
        "description",
        "allowed-tools",
        "metadata",
        "license",
        "compatibility",
    }:
        raise ValueError("Unsupported skill metadata field.")
    return Skill(
        metadata=SkillMetadata.model_validate(
            {k: metadata.get(k) for k in ("name", "description")}
        ),
        digest=content_digest(files),
        instructions=instructions.strip(),
        files=files,
    )


class SkillCatalog:
    """Construct per authenticated scope using only release-pinned, granted folders."""

    def __init__(self, approved: dict[str, tuple[Path, str]], *, max_active=4):
        self.approved = dict(approved)
        self.max_active = max_active

    def load(self, name: str) -> Skill:
        if name not in self.approved:
            raise ValueError("Skill is not in the permitted catalog.")
        root, digest = self.approved[name]
        skill = inspect_skill(root)
        if skill.metadata.name != name or skill.digest != digest:
            raise ValueError("Skill content differs from the pinned release.")
        return skill

    def descriptions(self) -> list[dict]:
        return [self.load(name).metadata.model_dump() for name in sorted(self.approved)]

    def activate(self, names: tuple[str, ...], reason: str) -> tuple[Skill, ...]:
        if not reason.strip() or len(names) != len(set(names)) or len(names) > self.max_active:
            raise ValueError("Invalid or recursive skill activation.")
        return tuple(self.load(name) for name in names)

    def resource(self, name: str, path: str, *, limit=16384) -> str:
        skill = self.load(name)
        if not safe_path(path) or path not in skill.files or path.startswith("scripts/"):
            raise ValueError("Resource unavailable; scripts require a registered reviewed tool.")
        root, _ = self.approved[name]
        data = read_bounded(root / path, limit)
        if len(data) > limit or hashlib.sha256(data).hexdigest() != skill.files[path]:
            raise ValueError("Resource exceeds its budget or has changed.")
        return data.decode("utf-8")
