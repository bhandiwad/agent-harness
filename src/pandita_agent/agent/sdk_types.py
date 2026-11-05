"""Public client contracts. Commands retain their exact serialized retry envelope."""

import codecs
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from pandita_agent.contracts import canonical

ACTIVE = {"queued", "running", "cancelling", "reconciling"}
PAUSE_EVENTS = {
    "model.completed",
    "approval.requested",
    "input.requested",
    "run.cancelled",
    "run.failed",
    "run.expired",
    "run.recovery_required",
    "tool.outcome_unknown",
    "reconciliation.blocked",
    "run.cancelled_before_dispatch",
}


class Session(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    id: UUID
    revision: int = Field(ge=1)
    owner: str
    state: dict[str, Any]
    actions: dict[str, bool] = Field(default_factory=dict)

    @property
    def status(self):
        return self.state["status"]

    @property
    def answer(self):
        history = self.state.get("history", [])
        if self.status != "completed" or not history:
            return None
        return next(
            (
                m["content"]
                for m in reversed(history[-1]["messages"])
                if m["role"] == "assistant" and not m.get("call")
            ),
            None,
        )


class Event(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    revision: int = Field(ge=1)
    kind: str
    actor: str
    data: dict[str, Any]
    created_at: str


@dataclass(frozen=True, init=False)
class PreparedCommand:
    path: str
    _encoded: str

    def __init__(self, path, body):
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "_encoded", canonical(body))

    @property
    def body(self):
        return json.loads(self._encoded)

    def dumps(self):
        return canonical({"path": self.path, "body": self.body})

    @classmethod
    def loads(cls, value):
        if len(value.encode()) > 262144:
            raise ValueError("Command exceeds its bound.")
        data = json.loads(value)
        if set(data) != {"path", "body"} or not isinstance(data["body"], dict):
            raise ValueError("Invalid command envelope.")
        return cls(data["path"], data["body"])

    def __repr__(self):
        return f"PreparedCommand(path={self.path!r}, command_id={self.body.get('command_id')!r})"


class SDKError(Exception):
    pass


class APIError(SDKError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(
            f"Harness request failed (HTTP {status_code}); inspect the current session before retrying."
        )


class AuthenticationError(APIError):
    pass


class PermissionDenied(APIError):
    pass


class RevisionConflict(APIError):
    pass


class NotFound(APIError):
    pass


class InvalidRequest(APIError):
    pass


class Unavailable(APIError):
    pass


class TransportError(SDKError):
    def __init__(self, command=None):
        self.command = command
        super().__init__(
            "Connection failed. If a command was sent, its outcome is unknown; retry the SAME prepared command."
        )


class WaitTimeout(SDKError):
    def __init__(self, session_id, after=None):
        self.session_id = str(session_id)
        self.after = after
        super().__init__("Wait deadline reached; the remote run has not been cancelled.")


class StreamClosed(SDKError):
    def __init__(self, after, reason="Stream closed; resume from the recorded revision."):
        self.after = after
        super().__init__(reason)


def check_status(response):
    if not 200 <= response.status_code < 300:
        cls = {
            401: AuthenticationError,
            403: PermissionDenied,
            404: NotFound,
            409: RevisionConflict,
            422: InvalidRequest,
            429: Unavailable,
            503: Unavailable,
        }.get(response.status_code, APIError)
        raise cls(response.status_code)


def session_id(value):
    return str(UUID(str(value.id if isinstance(value, Session) else value)))


def api_path(path):
    if (
        not isinstance(path, str)
        or not path.startswith("/v1/")
        or any(c in path for c in ("..", "\\", "#", "%", "\r", "\n"))
    ):
        raise ValueError("Use a fixed relative /v1/ API path.")
    return path


class IncompleteStream(ValueError):
    pass


class EventDecoder:
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer = ""
        self.total = 0

    def feed(self, chunk):
        self.total += len(chunk)
        if self.total > 8388608:
            raise ValueError("Event connection exceeded its byte bound.")
        self.buffer = (self.buffer + self.decoder.decode(chunk)).replace("\r\n", "\n")
        records = []
        while "\n\n" in self.buffer:
            block, self.buffer = self.buffer.split("\n\n", 1)
            if len(block) > 262144:
                raise ValueError("Event exceeded its bound.")
            kind = ""
            data = []
            for line in block.splitlines():
                if line.startswith("event:"):
                    kind = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
            if data:
                records.append((kind, json.loads("\n".join(data))))
        if len(self.buffer) > 262144:
            raise ValueError("Incomplete event exceeded its bound.")
        return records

    def finish(self):
        try:
            tail = self.decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise IncompleteStream("Stream ended in the middle of UTF-8 text.") from exc
        if self.buffer.strip() or tail:
            raise IncompleteStream("Stream ended in the middle of an event.")
