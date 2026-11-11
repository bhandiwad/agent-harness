"""Only explicit pre-execution capacity refusals retry with the same operation ID."""
import json
from unittest.mock import AsyncMock

import httpx2
import pytest

from pandita_agent.agent import code_runner
from pandita_agent.agent.code_contracts import CodeProject


@pytest.mark.parametrize('statuses', [(429, 429, 200), (429,) * 6, (503,)])
async def test_code_capacity_admission_is_bounded_and_preserves_identity(monkeypatch, statuses):
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        status = statuses[len(calls)-1]
        return httpx2.Response(status, json={'status': 'completed', 'files': {'result.json': json.dumps({'done': True, 'answer': '42', 'files': {}})}})
    client = httpx2.AsyncClient
    monkeypatch.setenv('AGENT_CODE_URL', 'http://127.0.0.1:8097')
    monkeypatch.setenv('AGENT_CODE_TOKEN', 'fixture')
    monkeypatch.delenv('AGENT_CODE_CA', raising=False)
    monkeypatch.setattr(code_runner.httpx2, 'AsyncClient', lambda **kw: client(transport=httpx2.MockTransport(handle), **kw))
    sleep = AsyncMock()
    monkeypatch.setattr(code_runner.asyncio, 'sleep', sleep)
    project = CodeProject(language='python', entrypoint='main.py', files={'main.py': 'def run(input, ctx): return "42"'})
    if statuses[-1] == 200:
        assert (await code_runner.execute(project, 'input', [], {}, 'same-operation'))['answer'] == '42'
    else:
        error = code_runner.CodeError if statuses[-1] == 429 else httpx2.HTTPStatusError
        with pytest.raises(error):
            await code_runner.execute(project, 'input', [], {}, 'same-operation')
    assert len(calls) == len(statuses)
    assert {c['operation_id'] for c in calls} == {'same-operation'}
    assert sleep.await_count == min(statuses.count(429), 5)
