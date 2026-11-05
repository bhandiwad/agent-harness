"""Opt-in real Linux OCI qualification. Images must already be built and digest-pinned."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from pandita_agent.agent.sandbox import DockerExecutor
from pandita_agent.agent.sandbox_contracts import BrokerSettings, Job


@pytest.fixture(scope="module")
def executor():
    path = os.environ.get("AGENT_SANDBOX_TEST_CONFIG")
    if not path:
        pytest.skip("Set AGENT_SANDBOX_TEST_CONFIG for real OCI qualification")
    settings = BrokerSettings.model_validate_json(Path(path).read_bytes())
    return DockerExecutor(settings)


async def execute(executor, kind, request, files=None):
    cfg = executor.settings
    job = Job.model_validate_json(
        json.dumps(
            {
                "version": 1,
                "operation_id": str(uuid4()),
                "scope": {
                    "tenant_id": str(cfg.tenant_id),
                    "workspace_id": str(cfg.workspace_id),
                    "subject": "synthetic-test",
                    "task_id": str(uuid4()),
                    "plan_revision": 1,
                },
                "arguments": {"kind": kind, "request": request, "files": files or {}},
            }
        )
    )
    return await executor.execute(job)


async def test_linux_python_boundaries(executor):
    code = """import os, socket, pathlib, json
assert os.getuid()==65532
assert 'FIXTURE_SECRET' not in os.environ
try:
    os.listdir('/sys')
    raise AssertionError('Landlock read restriction missing')
except PermissionError: pass
assert not pathlib.Path('/host-private').exists()
try:
    pathlib.Path('/etc/escape').write_text('forbidden')
    raise AssertionError('Root filesystem is writable')
except OSError: pass
try:
    socket.create_connection(('1.1.1.1',443),timeout=1)
    raise AssertionError('Network is reachable')
except OSError: pass
status=pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\\t1' in status
assert 'CapEff:\\t0000000000000000' in status
pathlib.Path('answer.txt').write_text(str(6*7))
print('boundaries enforced')"""
    result = await execute(
        executor, "python", {"code": code, "files": [], "output_files": ["answer.txt"]}
    )
    assert result["status"] == "completed", result
    assert result["files"] == {"answer.txt": "42"}
    assert result["isolation"]["network"] == "none"


async def test_linux_shell_input_and_output(executor):
    result = await execute(
        executor,
        "shell",
        {
            "code": "cat input.txt > output.txt; printf done",
            "files": ["input.txt"],
            "output_files": ["output.txt"],
        },
        {"input.txt": "isolated input"},
    )
    assert result["status"] == "completed", result
    assert result["files"] == {"output.txt": "isolated input"}


@pytest.mark.parametrize(
    "code,expected",
    [
        ("import time; time.sleep(10)", 124),
        ("print('x'*20000)", 122),
        ("import os; os.symlink('/etc/passwd','answer.txt')", 125),
    ],
)
async def test_linux_time_output_and_artifact_bounds(executor, code, expected):
    outputs = ["answer.txt"] if expected == 125 else []
    result = await execute(executor, "python", {"code": code, "files": [], "output_files": outputs})
    assert result["status"] == "failed" and result["exit_code"] == expected, result
    assert not result["files"]


async def test_linux_browser_executes_dom_and_blocks_network(executor):
    html = """<title>Sandbox</title><button id="go" onclick="document.querySelector('#result').textContent='42'">Calculate</button><p id="result">0</p><script>fetch('https://example.com').then(()=>document.title='LEAK').catch(()=>{});</script>"""
    result = await execute(
        executor,
        "browser",
        {
            "html_path": "page.html",
            "files": [],
            "output_files": [],
            "steps": [
                {"action": "click", "selector": "#go", "value": ""},
                {"action": "text", "selector": "#result", "value": ""},
            ],
        },
        {"page.html": html},
    )
    assert result["status"] == "completed", result
    value = json.loads(result["stdout"])
    assert value["title"] == "Sandbox" and value["observations"] == ["42"]
