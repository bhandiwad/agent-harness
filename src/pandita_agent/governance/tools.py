"""Reviewed, versioned tool bindings. Model input cannot choose identity or destination."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

import httpx2
from jsonschema import Draft202012Validator
from pydantic import Field, model_validator

from pandita_agent.bindings import ModelBinding
from pandita_agent.contracts import Contract, Digest, Identifier, canonical, content_digest


class ToolBlocked(ValueError):
    pass


class ToolUncertain(Exception):
    """Transport failure or invalid receipt cannot establish an external write outcome."""


class ToolSpec(Contract):
    name: Identifier
    version: Identifier
    owner: Identifier
    transport: Literal["rest", "mcp", "internal", "sandbox", "openconnector"]
    effect: Literal["read", "write"]
    permission: Annotated[str, Field(min_length=1, max_length=128)]
    input_schema: dict
    output_schema: dict
    timeout_seconds: Annotated[int, Field(ge=1, le=120)] = 30
    max_result_bytes: Annotated[int, Field(ge=128, le=1048576)] = 65536
    idempotent: bool = False
    approval_required: bool = True

    @model_validator(mode="after")
    def schemas(self) -> Self:
        for schema in (self.input_schema, self.output_schema):
            if len(canonical(schema)) > 32768:
                raise ValueError("Tool schema is too large.")
            validate_schema(schema)
        if self.effect == "write" and not self.approval_required:
            raise ValueError("Consequential tools require explicit approval.")
        return self

    @property
    def digest(self):
        return content_digest(self.model_dump(mode="json"))


def validate_schema(schema: dict):
    def refs(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"} and (
                    not isinstance(item, str) or not item.startswith("#/")
                ):
                    raise ValueError("Remote schema references are unsupported.")
                refs(item)
        elif isinstance(value, list):
            for item in value:
                refs(item)

    refs(schema)
    Draft202012Validator.check_schema(schema)


def validate_value(value, schema):
    if len(canonical(value).encode()) > 1048576:
        raise ToolBlocked("Tool payload exceeds the supported limit.")
    if next(Draft202012Validator(schema).iter_errors(value), None) is not None:
        raise ToolBlocked("Payload does not match the pinned tool schema.")


class Invocation(Contract):
    tenant_id: UUID
    workspace_id: UUID
    subject: str
    task_id: UUID
    plan_revision: int
    operation_id: UUID


class Adapter(Protocol):
    async def invoke(self, operation: str, arguments: dict, context: Invocation) -> dict: ...
    async def reconcile(self, operation_id: UUID, context: Invocation) -> dict | None: ...


class Registry:
    def __init__(
        self,
        tools: tuple[ToolSpec, ...],
        adapters: dict[str, Adapter],
        authorize: Callable[[Invocation, str], None],
    ):
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            raise ValueError("Tool names must be unique.")
        self.adapters, self.authorize = dict(adapters), authorize

    def resolve(self, name: str, digest: str, arguments: dict, context: Invocation):
        tool = self.tools.get(name)
        if tool is None or tool.digest != digest:
            raise ToolBlocked("Tool is not registered at the requested version.")
        self.authorize(context, tool.permission)
        if name not in self.adapters:
            raise ToolBlocked("Tool connection is not configured.")
        validate_value(arguments, tool.input_schema)
        return tool, self.adapters[name]

    async def read(self, name, digest, arguments, context):
        tool, adapter = self.resolve(name, digest, arguments, context)
        if tool.effect != "read":
            raise ToolBlocked("Writes must pass the durable effect ledger.")
        try:
            async with asyncio.timeout(tool.timeout_seconds):
                result = await adapter.invoke(name, arguments, context)
            if len(canonical(result).encode()) > tool.max_result_bytes:
                raise ToolBlocked("Tool result exceeds its configured limit.")
            validate_value(result, tool.output_schema)
            return result
        except (TimeoutError, httpx2.HTTPError, ToolUncertain) as exc:
            raise ToolBlocked("Tool service unavailable; no evidence returned.") from exc


class RestBinding(Contract):
    endpoint: str
    operations: dict[Identifier, str]
    status_path: str | None = None

    @model_validator(mode="after")
    def destination(self):
        ModelBinding.explicit_endpoint(self.endpoint)
        for path in (*self.operations.values(), self.status_path):
            if path is not None and (
                not path.startswith("/")
                or path.startswith("//")
                or any(item in path for item in ("..", "?", "#", "\\", "%"))
            ):
                raise ValueError("Use fixed relative operation paths.")
        return self


class RestAdapter:
    """A customer adapter implements this envelope, or supplies another Adapter implementation.

    credentials issues a delegated token for the authenticated Invocation and this fixed binding.
    It never receives model-provided identity. No redirects, ambient proxy or automatic retries.
    """

    def __init__(
        self,
        binding: RestBinding,
        credentials: Callable[[Invocation], Awaitable[str]],
        *,
        limit=65536,
        timeout=30,
        transport=None,
    ):
        self.binding, self.credentials = binding, credentials
        self.limit, self.timeout, self.transport = limit, timeout, transport

    async def _request(self, path, body, context):
        token = await self.credentials(context)
        if not token or "\n" in token or "\r" in token:
            raise ToolBlocked("Delegated access is not configured.")
        async with httpx2.AsyncClient(
            timeout=self.timeout, trust_env=False, follow_redirects=False, transport=self.transport
        ) as client:
            async with client.stream(
                "POST",
                self.binding.endpoint.rstrip("/") + path,
                headers={
                    "Authorization": "Bearer " + token,
                    "Idempotency-Key": str(context.operation_id),
                },
                json={
                    "version": 1,
                    "operation_id": str(context.operation_id),
                    "scope": context.model_dump(mode="json", exclude={"operation_id"}),
                    "arguments": body,
                },
            ) as response:
                if response.status_code != 200:
                    raise ToolUncertain("Adapter did not return a confirmed result.")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > self.limit:
                        raise ToolUncertain("Adapter response exceeded its limit.")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ToolUncertain("Adapter returned an invalid result.")
                return result

    async def invoke(self, operation, arguments, context):
        path = self.binding.operations.get(operation)
        if path is None:
            raise ToolBlocked("Operation is not configured.")
        return await self._request(path, arguments, context)

    async def reconcile(self, operation_id, context):
        if self.binding.status_path is None:
            return None
        return await self._request(
            self.binding.status_path, {"operation_id": str(operation_id)}, context
        )


class RetrievalPlan(Contract):
    source: Identifier
    fields: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=32)]
    filters: dict[Identifier, str | int | bool] = Field(default_factory=dict, max_length=32)
    limit: Annotated[int, Field(ge=1, le=1000)] = 100
    timeout_seconds: Annotated[int, Field(ge=1, le=30)] = 10

    def permitted(self, sources: dict[str, set[str]], required_filters: dict):
        if (
            self.source not in sources
            or not set(self.fields) <= sources[self.source]
            or not set(self.filters) <= sources[self.source]
            or any(self.filters.get(k) != v for k, v in required_filters.items())
        ):
            raise ToolBlocked("Retrieval exceeds the authorized source, field or filter scope.")
        return self


class EvidenceResult(Contract):
    source: Identifier
    version: str = Field(min_length=1, max_length=256)
    retrieved_at: str = Field(min_length=1, max_length=64)
    operation_id: UUID
    access_context_digest: Digest
    snapshot_digest: Digest
    evidence_ref: str = Field(min_length=1, max_length=2048)
    truncated: bool
    records: Annotated[tuple[dict, ...], Field(max_length=1000)]

    def verify(self, context: Invocation):
        if (
            self.operation_id != context.operation_id
            or self.access_context_digest != content_digest(context.model_dump(mode="json"))
            or self.snapshot_digest != content_digest(list(self.records))
        ):
            raise ToolBlocked("Evidence provenance does not match this invocation.")
        return self


class DataFusion:
    """Pluggable v1 discovery/resolve/retrieve/evidence contract; no assumed vendor endpoint."""

    def __init__(
        self,
        adapter: Adapter | None,
        sources: dict[str, set[str]],
        authorize: Callable[[Invocation, str], None],
    ):
        self.adapter, self.sources, self.authorize = adapter, sources, authorize

    async def call(
        self, operation: str, arguments: dict, context: Invocation, *, required_filters: dict
    ):
        if operation not in {"discover", "resolve", "retrieve", "evidence"}:
            raise ToolBlocked("Unsupported DataFusion operation.")
        self.authorize(context, "datafusion:" + operation)
        if self.adapter is None:
            raise ToolBlocked("DataFusion connection is not configured.")
        if operation == "retrieve":
            plan = RetrievalPlan.model_validate_json(canonical(arguments))
            plan.permitted(self.sources, required_filters)
        elif operation == "discover":
            if arguments:
                raise ToolBlocked("Discovery takes no caller-defined scope.")
        else:
            if (
                set(arguments) != {"reference"}
                or not isinstance(arguments["reference"], str)
                or len(arguments["reference"]) > 1024
            ):
                raise ToolBlocked("A bounded source reference is required.")
        async with asyncio.timeout(plan.timeout_seconds if operation == "retrieve" else 30):
            result = await self.adapter.invoke(operation, arguments, context)
        if len(canonical(result).encode()) > 65536:
            raise ToolBlocked("DataFusion response exceeds the supported limit.")
        if operation in {"retrieve", "evidence"}:
            evidence = EvidenceResult.model_validate_json(canonical(result)).verify(context)
            if evidence.source not in self.sources or any(
                set(record) - self.sources[evidence.source] for record in evidence.records
            ):
                raise ToolBlocked("DataFusion returned evidence outside the authorized scope.")
            if operation == "retrieve" and (
                evidence.source != plan.source
                or len(evidence.records) > plan.limit
                or any(set(record) - set(plan.fields) for record in evidence.records)
            ):
                raise ToolBlocked("DataFusion returned records outside the requested scope.")
            if operation == "retrieve" and any(
                key in record and (type(record[key]) is not type(value) or record[key] != value)
                for record in evidence.records
                for key, value in plan.filters.items()
            ):
                raise ToolBlocked("DataFusion returned records that contradict the requested filters.")
            return evidence
        return result


def difference(left: str, right: str, *, unit: str, expected_unit: str) -> dict:
    from decimal import Decimal, InvalidOperation

    if not unit or unit != expected_unit or max(len(left), len(right)) > 64:
        raise ToolBlocked("Both values must use the configured unit.")
    try:
        a, b = Decimal(left), Decimal(right)
        if (
            not a.is_finite()
            or not b.is_finite()
            or abs(a.adjusted()) > 30
            or abs(b.adjusted()) > 30
        ):
            raise ValueError()
        return {"value": str(a - b), "unit": unit, "calculator": "difference-v1"}
    except (InvalidOperation, ValueError) as exc:
        raise ToolBlocked("Finite numeric values are required.") from exc
