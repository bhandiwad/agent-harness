"""Portable code project; source is data until dispatched to the isolated executor."""

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from pandita_agent.contracts import Contract


class PackageRequest(Contract):
    language: Literal["python", "javascript"]
    packages: dict[str, str] = Field(default_factory=dict, max_length=20, exclude_if=lambda v: not v)

    @model_validator(mode="after")
    def packages_valid(self):
        for name, version in self.packages.items():
            pattern = r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}" if self.language == "python" else r"(?:@[a-z0-9-]+/)?[a-z0-9][a-z0-9._-]{0,99}"
            if not re.fullmatch(pattern, name) or not re.fullmatch(r"[0-9][a-zA-Z0-9.+-]{0,49}", version):
                raise ValueError("Use registry package names and exact versions, without URLs or version ranges.")
        normalized = [re.sub(r"[-_.]+", "-", n).lower() for n in self.packages]
        if len(set(normalized)) != len(normalized):
            raise ValueError("Package names must be unique.")
        return self


class GitSource(Contract):
    repository: str = Field(pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", max_length=200)
    branch: str = Field(min_length=1, max_length=100)
    directory: str = Field(default="agents/my-agent", min_length=1, max_length=150)
    commit: str = Field(default="", pattern=r"^(?:[a-f0-9]{40})?$")

    @model_validator(mode="after")
    def paths(self):
        if any(part in ('.', '..') for part in self.repository.split('/')):
            raise ValueError("Use a GitHub owner/repository without dot segments.")
        for value in (self.branch, self.directory):
            if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:[./][a-zA-Z0-9_-]+)*", value):
                raise ValueError("Use a branch and folder without spaces or hidden paths.")
        return self


class CodeProject(PackageRequest):
    language: Literal["python", "javascript"]
    entrypoint: str = Field(min_length=1, max_length=100)
    files: dict[str, str] = Field(min_length=1, max_length=12)
    environment: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda v: v is None)
    git: GitSource | None = Field(default=None, exclude_if=lambda v: v is None)
    test_input: str = Field(default="Prepare a brief from the supplied facts.", max_length=8192)

    @model_validator(mode="after")
    def project(self):
        import re

        for name, value in self.files.items():
            path = PurePosixPath(name)
            if (
                not re.fullmatch(r"[a-zA-Z0-9_./-]{1,100}", name)
                or path.is_absolute()
                or ".." in path.parts
                or str(path) != name
                or any(part.startswith(".") for part in path.parts)
                or not name.endswith((".py", ".js", ".json", ".txt", ".md"))
            ):
                raise ValueError(
                    "Use relative .py, .js, .json, .txt or .md project files without hidden paths."
                )
            if "\0" in value:
                raise ValueError("Project files must contain text.")
        if sum(len(v.encode()) for v in self.files.values()) > 32768:
            raise ValueError("Project source must fit within 32 KB.")
        if self.entrypoint not in self.files or not self.entrypoint.endswith(
            ".py" if self.language == "python" else ".js"
        ):
            raise ValueError("Choose an entrypoint matching the project language.")
        return self
