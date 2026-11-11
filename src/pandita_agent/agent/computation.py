"""Model-selected Python analysis through the existing isolated code broker."""

import asyncio
import copy
import json
import time

from pandita_agent.agent import code_runner
from pandita_agent.agent.code_contracts import CodeProject
from pandita_agent.agent.native_tools import ToolInputError, write_file
from pandita_agent.contracts import canonical
from pandita_agent.governance.tools import ToolBlocked

DEFINITION = {
    "description": "Run private Python analysis in an isolated, network-disabled environment. Define run(input, ctx), returning text; input is JSON with the selected files and supplied data. Use standard libraries (csv, json, statistics, math). ctx.write_file saves bounded text outputs. For a chart write a .chart.json file: {title, labels:[strings], series:[{name, values:[numbers]}]}. No model calls, connected tools, credentials, package installs or external actions are available here.",
    "schema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "minLength": 1, "maxLength": 16384},
            "files": {
                "type": "array",
                "maxItems": 12,
                "uniqueItems": True,
                "items": {"type": "string", "maxLength": 160},
            },
            "data": {"type": "string", "maxLength": 16384},
        },
        "required": ["code", "files", "data"],
        "additionalProperties": False,
    },
}


async def execute(runner, row, agent, arguments, commit):
    if not agent.computation:
        raise ToolBlocked("Data analysis is not enabled for this agent.")
    runner.authorize(row["owner"], "workspace:read")
    runner.authorize(row["owner"], "workspace:write")
    files = row["state"].get("files", {})
    if any(path not in files for path in arguments["files"]):
        raise ToolInputError(
            "An input file is unavailable. List the current files and select exact paths."
        )
    selected = {p: files[p]["content"] for p in arguments["files"]}
    message = canonical({"files": selected, "data": arguments["data"]})
    if len(message.encode()) > 32768:
        raise ToolInputError(
            "Analysis inputs exceed 32 KB. Select fewer files or a smaller data range."
        )
    project = CodeProject(
        language="python",
        entrypoint="analysis.py",
        files={"analysis.py": arguments["code"]},
    )
    run = row["state"]["run"]
    operation = run["pending"]["id"]
    commit(
        "analysis.started",
        lambda s: s["run"].update(inflight={"kind": "code", "id": operation}),
        {"tool": "harness_compute", "files": arguments["files"]},
    )
    started = time.monotonic()
    root = runner.agents[row["state"]["agent_id"]]
    async with asyncio.timeout(max(0.01, root.max_seconds - run["elapsed"])):
        result = await code_runner.execute(
            project,
            message,
            [],
            {
                "tenant_id": str(runner.settings.tenant_id),
                "workspace_id": str(runner.settings.workspace_id),
                "subject": row["owner"],
                "task_id": run["id"],
                "plan_revision": 1,
            },
            operation,
        )

    def complete(state):
        runner.authorize(row["owner"], "workspace:read")
        runner.authorize(row["owner"], "workspace:write")
        state["run"]["inflight"] = None
        state["run"]["elapsed"] += time.monotonic() - started
        if not isinstance(result, dict) or result.get("done") is not True:
            runner.result(
                state,
                {
                    "error": "Analysis did not complete. Check the Python code. Only local calculation and text-file outputs are allowed."
                },
            )
            return
        answer, outputs = result.get("answer"), result.get("files", {})
        if (
            not isinstance(answer, str)
            or len(answer.encode()) > 32768
            or not isinstance(outputs, dict)
            or len(outputs) > 8
        ):
            raise ToolBlocked("Invalid analysis output.")
        staged = copy.deepcopy(state)
        if runner.output_issues(answer):
            raise ToolInputError("Analysis output violates workspace safety policy.")
        for path, content in outputs.items():
            if not isinstance(content, str):
                raise ToolBlocked("Analysis outputs must be text.")
            if runner.output_issues(content):
                raise ToolInputError("Analysis file violates workspace safety policy.")
            if path.endswith(".chart.json"):
                validate_chart(content)
            previous = staged.get("files", {}).get(path)
            write_file(staged, path, content, previous["digest"] if previous else None)
        state["files"] = staged.get("files", {})
        runner.result(
            state,
            {"answer": answer, "files": list(outputs), "execution": "isolated_python"},
        )

    try:
        commit(
            "analysis.completed",
            complete,
            {"tool": "harness_compute", "elapsed_seconds": time.monotonic() - started},
        )
    except (ToolInputError, json.JSONDecodeError):

        def rejected(state):
            state["run"].update(
                inflight=None,
                elapsed=state["run"]["elapsed"] + time.monotonic() - started,
            )
            runner.result(
                state,
                {
                    "error": "Analysis output failed validation or workspace safety checks. Correct the output format or remove restricted content."
                },
            )

        commit("analysis.rejected", rejected, {"tool": "harness_compute"})


def validate_chart(raw):
    """Data-only chart artifacts; renderers never accept model-generated markup."""
    import math

    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"title", "labels", "series"}:
        raise ToolInputError("Chart needs title, labels and series.")
    labels, series = value["labels"], value["series"]
    if not isinstance(value["title"], str) or len(value["title"]) > 160:
        raise ToolInputError("Chart title must be text within 160 characters.")
    if (
        not isinstance(labels, list)
        or not 1 <= len(labels) <= 100
        or any(not isinstance(x, str) or len(x) > 100 for x in labels)
    ):
        raise ToolInputError("Chart needs 1–100 short labels.")
    if not isinstance(series, list) or not 1 <= len(series) <= 8:
        raise ToolInputError("Chart needs 1–8 series.")
    for item in series:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "values"}
            or not isinstance(item["name"], str)
            or len(item["name"]) > 100
        ):
            raise ToolInputError("Invalid chart series.")
        values = item["values"]
        if (
            not isinstance(values, list)
            or len(values) != len(labels)
            or any(
                type(n) not in (int, float) or not math.isfinite(n) or abs(n) > 1e15
                for n in values
            )
        ):
            raise ToolInputError(
                "Chart values must be finite numbers matching the labels."
            )
    return value
