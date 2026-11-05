"""Framework-independent operator-owned model endpoint contract."""

from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, field_validator

from pandita_agent.contracts import Contract, Identifier, NonemptyText


class ModelBinding(Contract):
    model_ref: Identifier
    model_name: Annotated[NonemptyText, Field(max_length=256)]
    base_url: Annotated[str, Field(max_length=2048)]
    api_key_env: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]

    @field_validator("base_url")
    @classmethod
    def explicit_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        # Accessing port rejects malformed/out-of-range ports before client construction.
        if parsed.port == 0:
            raise ValueError("Model endpoint port must be between 1 and 65535.")
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use an endpoint without credentials, query parameters or fragments.")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError(
                "Model endpoints require HTTPS, except loopback development endpoints."
            )
        if any(char.isspace() for char in value):
            raise ValueError("Model endpoint must not contain whitespace.")
        return value
