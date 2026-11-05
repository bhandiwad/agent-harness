"""Coded-agent effects use the same budgets, tool dispatch and durable session state."""

import asyncio
import copy
import json
import os
import time
from uuid import uuid4

import httpx2

from pandita_agent.agent.native_tools import write_file
from pandita_agent.agent.storage import Conflict
from pandita_agent.contracts import content_digest
from pandita_agent.bindings import ModelBinding
from pandita_agent.governance.context import token_upper_bound


class CodeError(ValueError):
    """A bounded, user-actionable code error; never a raw provider exception."""


def available():
    return bool(os.environ.get("AGENT_CODE_URL") and os.environ.get("AGENT_CODE_TOKEN"))


async def execute(project, message, replay, scope, operation):
    if not available():
        raise CodeError("Code execution is not configured for this workspace.")
    ModelBinding.explicit_endpoint(os.environ["AGENT_CODE_URL"])
    import ssl

    tls = ssl.create_default_context(cafile=os.environ.get("AGENT_CODE_CA"))
    async with httpx2.AsyncClient(
        verify=tls, trust_env=False, follow_redirects=False, timeout=45
    ) as client:
        # Only isolated, network-disabled computation is retried. Tool/model calls
        # are resolved by the harness after this step and replayed unchanged.
        for attempt in range(2):
            async with client.stream(
                "POST",
                os.environ["AGENT_CODE_URL"] + "/v1/code",
                headers={"Authorization": "Bearer " + os.environ["AGENT_CODE_TOKEN"]},
                json={
                    "project": project.model_dump(mode="json"),
                    "input": message,
                    "replay": replay,
                    "scope": scope,
                    "operation_id": operation if attempt == 0 else str(uuid4()),
                },
            ) as response:
                response.raise_for_status()
                value = await bounded_json(response, 98304)
            if value.get("status") != "unknown" or attempt:
                break

    if value.get("status") != "completed":
        return {
            "error": value.get("stderr")
            or "Code execution did not finish within its sandbox limit. Retry the run; if this repeats, ask the administrator to inspect runtime capacity."
        }
    return decode_result(value["files"]["result.json"])


def decode_result(raw):
    """Compressed transport does not relax per-file or total decoded bounds."""
    import base64
    import zlib

    value = json.loads(raw)
    if isinstance(value, dict) and value.get("encoding") == "zlib-base64-v1":
        try:
            if set(value) != {"encoding", "data"} or len(value["data"]) > 32768:
                raise ValueError()
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(
                base64.b64decode(value["data"], validate=True), 600001
            )
            if (
                len(decoded) > 600000
                or not decoder.eof
                or decoder.unused_data
                or decoder.unconsumed_tail
            ):
                raise ValueError()
            value = json.loads(decoded)
        except (ValueError, TypeError, zlib.error) as exc:
            raise CodeError("Invalid or oversized compressed code result.") from exc
    if not isinstance(value, dict):
        raise CodeError("Code result must be an object.")
    return value


async def step(runner, row, agent, root):
    sid, run = row["id"], row["state"]["run"]
    operation = str(uuid4())
    started = time.monotonic()

    def reserve(s):
        if s["status"] not in {"queued", "running"} or s["run"].get("cancel_requested"):
            raise Conflict("Run cancelled.")
        s["status"] = "running"
        s["run"]["inflight"] = {"kind": "code", "id": operation}
        s["run"]["steps"] += 1

    runner.commit(sid, "code.started", reserve)
    message = next(
        m["content"] for m in run["frames"][-1]["messages"] if m["role"] == "user"
    )
    async with asyncio.timeout(root.max_seconds - run["elapsed"]):
        result = await execute(
            agent.code,
            message,
            run["frames"][-1].get(
                "code_results",
                run.get("code_results", []) if len(run["frames"]) == 1 else [],
            ),
            {
                "tenant_id": str(runner.settings.tenant_id),
                "workspace_id": str(runner.settings.workspace_id),
                "subject": row["owner"],
                "task_id": run["id"],
                "plan_revision": 1,
            },
            operation,
        )
    if not isinstance(result, dict):
        raise CodeError("Invalid code result.")
    request = result.get("request")

    def guarded(s):
        runner.authorize(row["owner"], "session:create")
        runner.authorize(row["owner"], "agent:" + agent.id)
        if s["status"] == "cancelling" or s["run"].get("cancel_requested"):
            s["status"] = "cancelled"
            s["run"]["inflight"] = None
            return False
        s["run"]["inflight"] = None
        s["run"]["elapsed"] += time.monotonic() - started
        s["run"]["code_logs"] = str(result.get("logs", ""))[-4000:]
        if s["run"]["elapsed"] > root.max_seconds:
            raise Conflict("Code run time budget exhausted.")
        return True

    if "error" in result:

        def failed(s):
            if guarded(s):
                s["status"] = "failed"
                s["run"]["code_error"] = str(result["error"])[-4000:]

        runner.commit(sid, "code.failed", failed)
        return
    if result.get("done") is True:
        answer, files = result.get("answer"), result.get("files", {})
        if (
            not isinstance(answer, str)
            or not answer.strip()
            or len(answer) > 32768
            or not isinstance(files, dict)
            or len(files) > 16
        ):
            raise CodeError(
                "Return a nonempty answer within 32 KB and at most 16 files."
            )

        def complete(s):
            if not guarded(s):
                return
            if files:
                runner.authorize(row["owner"], "workspace:write")
                if "harness_write_file" not in agent.native_tools:
                    raise Conflict("File creation is not enabled for this agent.")
            staged = copy.deepcopy(s)
            for path, content in files.items():
                if not isinstance(content, str):
                    raise CodeError("File content must be text.")
                prior = staged.get("files", {}).get(path)
                write_file(staged, path, content, prior["digest"] if prior else None)
            issues = runner.completion_issues(staged, agent, answer, row["owner"])
            if agent.output_schema or agent.completion_check:
                s["run"]["completion"] = {
                    "check": agent.completion_check,
                    "status": "rejected" if issues else "passed",
                    "issues": issues,
                    "step": s["run"]["steps"],
                }
            if issues:
                s["status"] = "failed"
                return  # Deterministic code cannot repair its own source; preserve prior files.
            if files:
                s["files"] = staged["files"]
            s["run"]["frames"][-1]["messages"].append(
                {"role": "assistant", "content": answer}
            )
            runner.finish_answer(s, answer)

        runner.commit(sid, "code.completed", complete)
        return
    if not isinstance(request, dict):
        raise CodeError("Code returned an invalid operation.")
    if request.get("kind") == "model":
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 32768:
            raise CodeError("ctx.generate requires a prompt within 32 KB.")
        model = runner.models[agent.model]
        estimate = token_upper_bound(
            {"instructions": agent.instructions, "prompt": prompt}
        )
        remaining = root.max_tokens - run["input_tokens"] - run["output_tokens"]
        max_output = min(agent.capacity.output_tokens, remaining - estimate)
        if (
            max_output <= 0
            or estimate + max_output + agent.capacity.safety_tokens
            > agent.capacity.context_tokens
        ):
            raise Conflict("Model context or token budget exhausted.")

        def model_reserve(s):
            if guarded(s):
                s["run"]["inflight"] = {"kind": "model"}

        current = runner.commit(sid, "code.model_started", model_reserve)
        if current["state"]["status"] == "cancelled":
            return
        timeout = min(
            model.request_timeout_seconds,
            root.max_seconds - current["state"]["run"]["elapsed"],
        )
        if timeout <= 0:
            raise Conflict("Run time budget exhausted.")
        model_started = time.monotonic()
        async with asyncio.timeout(timeout):
            answer = await runner.provider.generate(
                model,
                agent.instructions,
                [{"role": "user", "content": prompt}],
                [],
                max_output,
                timeout,
            )
        if answer.call or not answer.text.strip():
            raise CodeError("Model returned an invalid answer to ctx.generate.")

        def received(s):
            runner.authorize(row["owner"], "session:create")
            runner.authorize(row["owner"], "agent:" + agent.id)
            s["run"]["inflight"] = None
            s["run"]["input_tokens"] += answer.input_tokens
            s["run"]["output_tokens"] += answer.output_tokens
            s["run"]["elapsed"] += time.monotonic() - model_started
            if s["status"] == "cancelling" or s["run"].get("cancel_requested"):
                s["status"] = "cancelled"
            elif (
                s["run"]["elapsed"] > root.max_seconds
                or s["run"]["input_tokens"] + s["run"]["output_tokens"]
                > root.max_tokens
            ):
                s["status"] = "failed"
            else:
                s["run"]["frames"][-1].setdefault("code_results", []).append(
                    {"request": request, "result": answer.text}
                )

        runner.commit(
            sid,
            "code.model_completed",
            received,
            {
                "agent": agent.id,
                "input_tokens": answer.input_tokens,
                "output_tokens": answer.output_tokens,
                "cached_tokens": answer.cached_tokens,
            },
        )
    elif request.get("kind") == "tool":
        name, arguments = request.get("name"), request.get("arguments")
        if name not in (*agent.tools, *agent.native_tools) or not isinstance(
            arguments, dict
        ):
            raise CodeError(
                "Choose a tool enabled for this agent and pass an arguments object."
            )

        def pending(s):
            if not guarded(s):
                return
            call = {
                "id": str(uuid4()),
                "name": name,
                "arguments": arguments,
                "expires_at": time.time() + runner.settings.approval_seconds,
            }
            call["digest"] = content_digest(
                {
                    "session": str(sid),
                    "run": run["id"],
                    "config": runner.settings.digest,
                    "call": dict(call),
                }
            )
            s["run"]["pending"] = call
            s["run"]["code_request"] = request
            s["run"]["frames"][-1]["messages"].append(
                {
                    "role": "assistant",
                    "content": "",
                    "call": {"name": name, "arguments": arguments},
                    "call_id": call["id"],
                }
            )

        runner.commit(sid, "code.tool_requested", pending)
    else:
        raise CodeError("Unknown code operation.")


async def environment_request(method, key="", body=None):
    """Small authenticated control requests; the build itself runs asynchronously."""
    ModelBinding.explicit_endpoint(os.environ["AGENT_CODE_URL"])
    import ssl

    if not available():
        raise CodeError("Code execution is not configured for this workspace.")
    async with httpx2.AsyncClient(
        verify=ssl.create_default_context(cafile=os.environ.get("AGENT_CODE_CA")),
        trust_env=False,
        follow_redirects=False,
        timeout=8,
    ) as client:
        async with client.stream(
            method,
            os.environ["AGENT_CODE_URL"]
            + "/v1/environments"
            + ("/" + key if key else ""),
            headers={"Authorization": "Bearer " + os.environ["AGENT_CODE_TOKEN"]},
            json=body,
        ) as response:
            if response.status_code >= 400:
                raise CodeError(
                    "Package build service is unavailable. Check packages and retry."
                )
            return await bounded_json(response, 65536)


async def bounded_json(response, limit):
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw.extend(chunk)
        if len(raw) > limit:
            raise CodeError("Code service response exceeded its limit.")
    return json.loads(raw)
