import json
from pathlib import Path

import httpx2
import pytest
from pydantic import ValidationError

from pandita_agent.agent import contracts
from pandita_agent.agent.contracts import Model
from pandita_agent.agent.execution import ExecutionProfile, Sampling
from pandita_agent.agent.provider import Provider
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.storage import Conflict, Store

from .test_runner import Model as FakeModel
from .test_runner import new, send
from .test_streaming import sse


def profile_files():
    root = Path(contracts.__file__).parent.parent / "generic_template" / "config"
    if not root.exists():
        root = Path(__file__).parent.parent / "config"
    return sorted((root / "model-profiles").glob("*.json"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("temperature", -0.1),
        ("temperature", float("nan")),
        ("top_p", 0.0),
        ("top_p", 1.1),
        ("top_k", -2),
        ("top_k", True),
        ("min_p", float("inf")),
        ("repetition_penalty", 0.0),
        ("presence_penalty", 2.1),
        ("frequency_penalty", -2.1),
        ("seed", 2**63),
        ("stop", ("",)),
        ("stop", ("a",) * 5),
        ("messages", []),
        ("max_tokens", 1000000),
    ],
)
def test_invalid_or_authority_overriding_parameters_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Sampling(**{field: value})


def test_protocol_and_thinking_controls_fail_explicitly(settings):
    data = settings.models[0].model_dump(mode="json")
    data["execution"] = {"id": "fixture-v1", "family": "gemma4"}
    for protocol in ("openai_responses", "anthropic_messages"):
        with pytest.raises(ValidationError):
            Model.model_validate_json(json.dumps({**data, "protocol": protocol}))
    for family, thinking in (("gemma4", True), ("qwen3_8", False), ("qwen3_8", None)):
        with pytest.raises(ValidationError):
            ExecutionProfile(
                id="fixture", family=family, enable_thinking=thinking, reasoning_effort="low"
            )
    profile = ExecutionProfile(
        id="fixture", family="qwen3_8", enable_thinking=True, reasoning_effort="medium"
    )
    assert profile.request_options() == {
        "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True},
        "reasoning_effort": "medium",
    }
    for timeout in (0, 601):
        with pytest.raises(ValidationError):
            Model.model_validate_json(json.dumps({**data, "request_timeout_seconds": timeout}))


@pytest.mark.parametrize("stream", [False, True])
async def test_shipped_profiles_forward_parameters_and_preserve_tool_boundaries(stream):
    paths = profile_files()
    assert len(paths) == 4
    for path in paths:
        data = json.loads(path.read_text())
        data.update(base_url="http://127.0.0.1:9999/v1", stream=stream)
        model = Model.model_validate_json(json.dumps(data))
        requests = []

        def respond(request, requests=requests, model=model):
            body = json.loads(request.content)
            requests.append(body)
            assert "extra_body" not in body
            assert body["chat_template_kwargs"] == {
                "enable_thinking": True,
                "preserve_thinking": True,
            }
            assert body["max_tokens"] == 200 and body["parallel_tool_calls"] is False
            assert body["tools"][0]["function"]["name"] == "lookup"
            assert body["model"] == model.model_name
            for key, value in model.execution.sampling.model_dump(exclude_none=True).items():
                assert body[key] == value
            tool = {
                "id": "c1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"query":"pump"}'},
            }
            if stream:
                return httpx2.Response(
                    200,
                    content=sse(
                        [
                            {
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"tool_calls": [{"index": 0, **tool}]},
                                        "finish_reason": "tool_calls",
                                    }
                                ]
                            },
                            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
                            None,
                        ]
                    ),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": None, "tool_calls": [tool]},
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )

        result = await Provider(
            {"MODEL_API_KEY": "fixture-secret"}, transport=httpx2.MockTransport(respond)
        ).generate(
            model,
            "System",
            [{"role": "user", "content": "Find pump"}],
            [{"name": "lookup", "description": "Lookup", "schema": {"type": "object"}}],
            200,
            10,
        )
        assert result.call.name == "lookup" and result.call.arguments == {"query": "pump"}
        assert len(requests) == 1


async def test_omitted_sampling_is_not_silently_replaced(settings):
    model = settings.models[0].model_copy(
        update={"execution": ExecutionProfile(id="inherit", family="gemma4")}
    )

    def respond(request):
        body = json.loads(request.content)
        assert not set(Sampling.model_fields).intersection(body)
        assert body["chat_template_kwargs"] == {"enable_thinking": True, "preserve_thinking": True}
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    result = await Provider(
        {"FIXTURE_KEY": "secret"}, transport=httpx2.MockTransport(respond)
    ).generate(model, "System", [], [], 100, 10)
    assert result.text == "Hello"


@pytest.mark.parametrize("request_seconds,run_budget,expected", [(7, 300, 7), (600, 10, 10)])
async def test_profile_request_seconds_is_capped_by_run_budget_and_audited(
    store, request_seconds, run_budget, expected
):
    model = store.settings.models[0].model_copy(
        update={
            "request_timeout_seconds": request_seconds,
            "execution": ExecutionProfile(id="test-v1", family="qwen3_8", enable_thinking=False),
        }
    )
    agent = store.settings.agents[0].model_copy(update={"max_seconds": run_budget})
    store.settings = store.settings.model_copy(
        update={"models": (model,), "agents": (agent, store.settings.agents[1])}
    )

    class Recording(FakeModel):
        async def generate(self, *args):
            assert args[-1] == expected
            return await super().generate(*args)

    runner = Runner(store, Recording("Done"))
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "completed"
    event = next(e for e in store.events(row["id"]) if e["kind"] == "model.started")
    assert event["data"]["execution"]["id"] == "test-v1"
    assert event["data"]["request_timeout_seconds"] == expected
    assert "api_key_env" not in json.dumps(event["data"]) and "secret" not in json.dumps(
        event["data"]
    )
    changed_model = model.model_copy(update={"request_timeout_seconds": request_seconds + 1})
    changed = store.settings.model_copy(update={"models": (changed_model,)})
    with pytest.raises(Conflict):
        send(Runner(Store(store.engine, changed), FakeModel()), store.get(row["id"]))
