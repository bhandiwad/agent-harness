"""Fixed OpenConnector Actions with subject-scoped tokens and no write replay."""

import json

import httpx2
from pydantic import Field, model_validator

from pandita_agent.bindings import ModelBinding
from pandita_agent.contracts import Contract
from pandita_agent.governance.tools import ToolBlocked, ToolUncertain, validate_value


class OpenConnectorBinding(Contract):
    kind: str = Field(default="openconnector", pattern="^openconnector$")
    endpoint: str
    action_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+\.[a-zA-Z0-9_.-]+$", max_length=160)
    # The account is selected by the operator, never by model arguments.
    subject_connections: dict[str, str] = Field(min_length=1, max_length=10000)
    fixed_input: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def destination(self):
        ModelBinding.explicit_endpoint(self.endpoint)
        for connection in self.subject_connections.values():
            if (
                not connection
                or len(connection) > 255
                or any(ord(c) < 33 or ord(c) > 126 for c in connection)
            ):
                raise ValueError("Use an opaque OpenConnector connection ID without whitespace.")
        return self


class OpenConnectorAdapter:
    def __init__(self, binding, tool, credentials, *, transport=None):
        self.binding, self.tool, self.credentials = binding, tool, credentials
        self.transport = transport

    async def invoke(self, operation, arguments, context):
        if operation != self.tool.name:
            raise ToolBlocked("OpenConnector action is not configured.")
        if self.tool.effect == "write":
            if set(arguments) != {"approved_arguments", "approval_digest"} or not isinstance(
                arguments.get("approval_digest"), str
            ):
                raise ToolBlocked("The harness must approve this action before dispatch.")
            arguments = arguments["approved_arguments"]
        validate_value(arguments, self.tool.input_schema)
        connection = self.binding.subject_connections.get(context.subject)
        if not connection:
            raise ToolBlocked("Connect this account before using the agent.")
        if any(
            key in arguments and arguments[key] != value
            for key, value in self.binding.fixed_input.items()
        ):
            raise ToolBlocked("The request changes a fixed action input.")
        token = await self.credentials(context)
        if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ToolBlocked("Delegated OpenConnector access is unavailable.")
        # OpenConnector's completed-response replay expires after 24h. The harness
        # owns the durable operation ledger; this adapter never re-dispatches a write.
        async with httpx2.AsyncClient(
            timeout=self.tool.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            async with client.stream(
                "POST",
                self.binding.endpoint.rstrip("/") + "/v1/actions/" + self.binding.action_id,
                headers={
                    "Authorization": "Bearer " + token,
                    "x-oo-connector-app-id": connection,
                    "Idempotency-Key": str(context.operation_id),
                },
                json={"input": {**arguments, **self.binding.fixed_input}},
            ) as response:
                if response.status_code != 200:
                    raise ToolUncertain("OpenConnector did not confirm the action result.")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > self.tool.max_result_bytes + 16384:
                        raise ToolUncertain("OpenConnector response exceeded its limit.")
        try:
            result = json.loads(raw)
            if (
                not isinstance(result, dict)
                or result.get("success") is not True
                or not isinstance(result.get("data"), dict)
            ):
                raise ValueError()
            if self.tool.effect == "write":
                return {
                    "operation_id": str(context.operation_id),
                    "status": "confirmed",
                    "result": result["data"],
                }
            return result["data"]
        except (ValueError, TypeError):
            raise ToolUncertain("OpenConnector returned an unconfirmed result.") from None

    async def reconcile(self, operation_id, context):
        # Do not use another action POST as a status query. Operator reconciliation
        # is required when a response was lost; no exactly-once claim is made.
        return None
