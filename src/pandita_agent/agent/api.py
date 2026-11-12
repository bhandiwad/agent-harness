"""Generic authenticated session API and portable chat interface."""

import asyncio
import copy
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from pandita_agent.agent.capabilities import Capabilities, read_instructions
from pandita_agent.agent.chat_reasoning import visible
from pandita_agent.agent.contracts import (
    Command,
    ContextAccessRequest,
    CreateSession,
    Decision,
    Memory,
    Turn,
    Upload,
)
from pandita_agent.agent.storage import Conflict, NotFound
from pandita_agent.contracts import canonical
from pandita_agent.runtime.auth import Access, Forbidden, Unauthorized


def public_snapshot(row, authorize, user):
    row = copy.deepcopy(row)
    state = row.get("state", {})
    state.pop("native_context", None)
    try:
        authorize(user, "workspace:read")
    except Forbidden:
        state["files"] = {
            path: {key: value for key, value in file.items() if key != "content"}
            for path, file in state.get("files", {}).items()
        }
    for frame in (state.get("run") or {}).get("frames", []):
        frame.pop("provider_context", None)
        frame["messages"] = visible(frame["messages"])
    for turn in state.get("history", []):
        turn["messages"] = visible(turn["messages"])
    return row


def create_app(runner):
    app = FastAPI(title="Pandita generic agent harness", version="0.6.0")
    bearer = HTTPBearer(auto_error=False)

    def actor(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ):
        if credentials is None:
            raise Unauthorized()
        try:
            access = Access(runner.settings)
        except (OSError, ValueError) as exc:
            raise HTTPException(503, "Identity configuration is unavailable.") from exc
        principal = access.authenticate(credentials.credentials)
        if principal.draft_digest is not None:
            raise Unauthorized()  # A report-bound credential cannot become a generic credential.
        return principal.subject

    User = Annotated[str, Depends(actor)]

    @app.get("/v1/usage")
    def usage(user: User, period: int = 30, scope: str = "mine"):
        from pandita_agent.agent.usage import report

        runner.authorize(user, "session:read")
        if scope not in ("mine", "workspace"):
            raise ValueError("Choose personal or workspace usage.")
        if scope == "workspace":
            runner.authorize(user, "usage:workspace")
        result = report(runner.store, user, period, scope == "workspace")
        try:
            runner.authorize(user, "usage:workspace")
            result["can_view_workspace"] = True
        except Forbidden:
            result["can_view_workspace"] = False
        return result

    async def payload(request, contract):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise HTTPException(413, "Request exceeds its limit.")
        return contract.model_validate_json(raw)

    def readable(session_id, user):
        runner.authorize(user, "session:read")
        row = runner.store.get(session_id)
        if row["owner"] != user:
            runner.authorize(user, "session:approve")
        return row

    for error, status, message in (
        (Unauthorized, 401, "Authentication required or invalid."),
        (Forbidden, 403, "Permission denied."),
        (NotFound, 404, "Conversation not found."),
        (
            Conflict,
            409,
            "Command conflicts with current conversation state; reload and inspect status.",
        ),
        (ValidationError, 422, "Request does not match the contract."),
        (RequestValidationError, 422, "Request does not match the contract."),
        (SQLAlchemyError, 503, "Conversation storage is unavailable."),
        (ValueError, 422, "Input is invalid for this operation."),
    ):

        async def failure(request, exc, status=status, message=message):
            return JSONResponse({"detail": message}, status_code=status)

        app.add_exception_handler(error, failure)

    @app.get("/healthz")
    def health():
        return {"service": "generic-agent", "alive": True}

    @app.get("/v1/agents")
    def agents(user: User):
        runner.authorize(user, "session:read")
        permitted = []
        for agent in runner.settings.agents:
            try:
                runner.authorize(user, "agent:" + agent.id)
            except Forbidden:
                continue
            permitted.append(
                {
                    "id": agent.id,
                    "tools": agent.tools,
                    "delegates": agent.delegates,
                    "max_steps": agent.max_steps,
                    "max_tokens": agent.max_tokens,
                    "model": agent.model,
                }
            )

        return {
            "agents": permitted,
            "config_digest": runner.settings.digest,
        }

    @app.post("/v1/context/access")
    async def context_access(request: Request, user: User):
        """Recheck artifact evidence for this actor; never infer access from a share."""
        from pandita_agent.governance.tools import Invocation, Registry, ToolBlocked

        runner.authorize(user, "session:read")
        body = await payload(request, ContextAccessRequest)
        connection = runner.connections.get("datafusion_authorize")
        if connection is None or connection.tool.effect != "read":
            raise HTTPException(
                503, "DataFusion source-access verification is not configured."
            )
        context = Invocation(
            tenant_id=runner.settings.tenant_id,
            workspace_id=runner.settings.workspace_id,
            subject=user,
            task_id=uuid4(),
            plan_revision=1,
            operation_id=uuid4(),
        )
        registry = Registry(
            (connection.tool,),
            runner.adapters,
            lambda scope, permission: runner.authorize(scope.subject, permission),
        )
        try:
            result = await registry.read(
                connection.tool.name,
                connection.tool.digest,
                body.model_dump(mode="json"),
                context,
            )
            granted = {
                (item["reference"], item["version"])
                for item in result["authorized_sources"]
            }
        except (ToolBlocked, KeyError, TypeError, ValueError):
            raise HTTPException(
                503, "Source-access verification is unavailable."
            ) from None
        requested = {(item.reference, item.version) for item in body.sources}
        return {
            "allowed": requested <= granted,
            "config_digest": runner.settings.digest,
            "authorized_sources": [
                {"reference": ref, "version": version}
                for ref, version in sorted(requested & granted)
            ],
        }

    @app.get("/v1/agents/{agent_id}/capabilities")
    def capabilities(agent_id: str, user: User):
        runner.authorize(user, "session:read")
        runner.authorize(user, "agent:" + agent_id)
        agent = runner.agents.get(agent_id)
        if agent is None:
            raise NotFound()
        catalog = Capabilities(agent, runner.connections, runner.authorize, user)
        if agent.instruction_tree:
            read_instructions(agent.instruction_tree)
        return {
            "agent_id": agent.id,
            "config_digest": runner.settings.digest,
            "skill_mode": agent.skill_mode,
            "max_active_skills": agent.max_active_skills,
            "skills": catalog.descriptions(),
            "instruction_digest": agent.instruction_tree.digest
            if agent.instruction_tree
            else None,
            "tools": runner.tools(agent, user),
            "delegation": "sequential",
        }

    @app.get("/v1/sessions")
    def sessions(user: User, review: bool = False):
        runner.authorize(user, "session:read")
        if review:
            runner.authorize(user, "session:approve")
        return {"sessions": runner.store.list(None if review else user, viewer=user)}

    def public(row, user):
        return public_snapshot(row, runner.authorize, user)

    def request_schema(contract):
        return {
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": contract.model_json_schema()}
                },
            }
        }

    @app.post(
        "/v1/sessions", status_code=201, openapi_extra=request_schema(CreateSession)
    )
    async def create(request: Request, user: User):
        body = await payload(request, CreateSession)
        return public(await run_in_threadpool(runner.create, body, user), user)

    @app.get("/v1/sessions/{session_id}")
    def get(session_id: UUID, user: User):
        row = readable(session_id, user)
        own = row["owner"] == user
        actions = {}
        for name, permission, eligible in (
            ("turn", "session:create", own),
            ("cancel", "session:cancel", own),
            ("memory", "session:create", own),
            ("upload", "workspace:write", own),
            ("approve", "session:approve", not own),
            ("reconcile", "session:approve", True),
            ("download", "workspace:read", True),
        ):
            try:
                runner.authorize(user, permission)
                actions[name] = eligible
            except Forbidden:
                actions[name] = False
        return {**public(row, user), "actions": actions}

    for route, contract, method in (
        ("turns", Turn, runner.turn),
        ("memory", Memory, runner.memory),
        ("cancel", Command, runner.cancel),
        ("decisions", Decision, runner.decide),
        ("reconcile", Command, runner.reconcile),
        ("files", Upload, runner.upload),
    ):

        def endpoint(contract, method):
            async def apply(session_id: UUID, request: Request, user: User):
                body = await payload(request, contract)
                runner.authorize(
                    user,
                    {
                        "turn": "session:create",
                        "memory": "session:create",
                        "cancel": "session:cancel",
                        "decide": "session:approve",
                        "reconcile": "session:approve",
                        "upload": "workspace:write",
                    }[method.__name__],
                )
                readable(session_id, user)
                return public(
                    await run_in_threadpool(method, session_id, body, user), user
                )

            return apply

        app.add_api_route(
            "/v1/sessions/{session_id}/" + route,
            endpoint(contract, method),
            methods=["POST"],
            name=route,
            openapi_extra=request_schema(contract),
        )

    @app.get("/v1/sessions/{session_id}/files/{path:path}")
    def download(session_id: UUID, path: str, user: User):
        row = readable(session_id, user)
        runner.authorize(user, "workspace:read")
        file = row["state"].get("files", {}).get(path)
        if file is None:
            raise NotFound()
        return Response(
            file["content"].encode(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": 'attachment; filename="'
                + path.rsplit("/", 1)[-1]
                + '"',
                "X-Content-Type-Options": "nosniff",
                "ETag": '"' + file["digest"] + '"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/v1/sessions/{session_id}/events")
    def events(session_id: UUID, user: User, after: int = 0):
        readable(session_id, user)
        return {"events": runner.store.events(session_id, max(0, after))}

    @app.get("/v1/sessions/{session_id}/stream")
    async def stream(session_id: UUID, request: Request, user: User, after: int = 0):
        await run_in_threadpool(readable, session_id, user)

        async def emit():
            cursor = max(0, after)
            for _ in range(60):
                if await request.is_disconnected():
                    return
                # Recheck signature/expiry and permissions throughout a long-lived stream.
                try:
                    credentials = await bearer(request)
                    await run_in_threadpool(actor, credentials)
                    await run_in_threadpool(readable, session_id, user)
                except (Unauthorized, Forbidden, HTTPException):
                    yield 'event: access.closed\ndata: {"reconnect": true}\n\n'
                    return
                batch = await run_in_threadpool(runner.store.events, session_id, cursor)
                for event in batch:
                    cursor = event["revision"]
                    yield f"id: {cursor}\nevent: {event['kind']}\ndata: {canonical(jsonable_encoder(event))}\n\n"
                if not batch:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            emit(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/status")
    def status(user: User):
        runner.authorize(user, "session:read")
        rows = runner.store.list(user)
        return {
            "config_digest": runner.settings.digest,
            "visible_sessions": len(rows),
            "worker_health": "unprobed",
            "model_health": "unprobed",
            "context_connections": [
                connection.tool.name for connection in runner.settings.connections
            ],
        }

    assets = Path(__file__).parent / "web"

    @app.get("/", include_in_schema=False)
    def chat():
        return FileResponse(
            assets / "index.html", headers={"Cache-Control": "no-store"}
        )

    @app.get("/app.js", include_in_schema=False)
    def javascript():
        return FileResponse(assets / "app.js", media_type="text/javascript")

    return app
