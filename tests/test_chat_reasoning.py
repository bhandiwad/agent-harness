import json

import httpx2
import pytest

from pandita_agent.agent.chat_reasoning import MAX_REASONING, extract, select, visible
from pandita_agent.agent.contracts import ModelResult
from pandita_agent.agent.execution import ExecutionProfile
from pandita_agent.agent.provider import Provider, openai_messages
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.storage import Conflict

from .test_runner import Model, call, new, send
from .test_streaming import sse


@pytest.mark.parametrize("family", ["gemma4", "qwen3_8"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
async def test_private_reasoning_assembled_without_answer_stream_leak(
    settings, family, stream, field
):
    profile = ExecutionProfile(id="test", family=family)
    model = settings.models[0].model_copy(update={"execution": profile, "stream": stream})
    emitted = []

    async def delta(value):
        emitted.append(value)

    def respond(request):
        body = json.loads(request.content)
        assert body["chat_template_kwargs"] == {"enable_thinking": True, "preserve_thinking": True}
        message = {"content": "Answer", field: "private-thought"}
        usage = {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "completion_tokens_details": {"reasoning_tokens": 5},
        }
        if stream:
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse(
                    [
                        {"choices": [{"delta": {field: "private-"}}]},
                        {
                            "choices": [
                                {
                                    "delta": {field: "thought", "content": "Answer"},
                                    "finish_reason": "stop",
                                }
                            ]
                        },
                        {"choices": [], "usage": usage},
                        None,
                    ]
                ),
            )
        return httpx2.Response(
            200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage}
        )

    result = await Provider(
        {"FIXTURE_KEY": "fixture"}, transport=httpx2.MockTransport(respond)
    ).generate(model, "system", [], [], 100, 10, on_delta=delta)
    assert result.reasoning == "private-thought" and result.text == "Answer"
    assert result.reasoning_tokens == 5 and result.output_tokens == 7
    assert "private" not in "".join(emitted)
    assert "".join(emitted) == ("Answer" if stream else "")


def test_family_replay_policy_keeps_associated_call_ids_and_never_mutates_history():
    messages = [
        {"role": "user", "content": "First"},
        {
            "role": "assistant",
            "content": "",
            "_reasoning": "tool-thought",
            "call": {"name": "lookup", "arguments": {}},
            "call_id": "internal",
            "provider_call_id": "original",
        },
        {
            "role": "tool",
            "content": "result",
            "call_id": "internal",
            "provider_call_id": "original",
        },
        {"role": "assistant", "content": "Answer", "_reasoning": "answer-thought"},
        {"role": "user", "content": "Follow up"},
    ]
    for family in ("gemma4", "qwen3_8"):
        result = openai_messages(messages, ExecutionProfile(id="test", family=family))
        assert result[1]["reasoning_content"] == "tool-thought"
        assert result[1]["tool_calls"][0]["id"] == result[2]["tool_call_id"] == "original"
        assert result[3].get("reasoning_content") == (
            "answer-thought" if family == "qwen3_8" else None
        )
    assert messages[3]["_reasoning"] == "answer-thought"
    assert "_reasoning" not in json.dumps(visible(messages))
    assert "_reasoning" not in json.dumps(select(messages, None))


@pytest.mark.parametrize(
    "value",
    [
        {"reasoning": []},
        {"reasoning": "x" * (MAX_REASONING + 1)},
        {"reasoning": "a", "reasoning_content": "b"},
    ],
)
def test_malformed_oversized_or_conflicting_reasoning_rejected(value):
    with pytest.raises(ValueError):
        extract(value)
    assert extract({"reasoning": "same", "reasoning_content": "same"}) == "same"


@pytest.mark.parametrize("finish", ["length", None])
async def test_incomplete_thinking_is_never_committed(store, finish):
    model = store.settings.models[0].model_copy(
        update={"stream": True, "execution": ExecutionProfile(id="test", family="qwen3_8")}
    )
    store.settings = store.settings.model_copy(update={"models": (model,)})
    items = [{"choices": [{"delta": {"reasoning": "private-partial"}, "finish_reason": finish}]}]
    if finish:
        items += [{"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 10}}, None]

    def respond(request):
        return httpx2.Response(
            200, content=sse(items), headers={"content-type": "text/event-stream"}
        )

    runner = Runner(
        store, Provider({"FIXTURE_KEY": "fixture"}, transport=httpx2.MockTransport(respond))
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    saved = store.get(row["id"])
    assert saved["state"]["status"] == "failed"
    assert "private-partial" not in json.dumps(saved["state"])


async def test_reasoning_counts_toward_context_and_history_tool_is_public(store):
    model = store.settings.models[0].model_copy(
        update={"execution": ExecutionProfile(id="test", family="qwen3_8")}
    )
    store.settings = store.settings.model_copy(update={"models": (model,)})

    class Thinking(Model):
        async def generate(self, *args):
            return ModelResult(
                text="Answer", reasoning="private-thought", input_tokens=10, output_tokens=20
            )

    runner = Runner(store, Thinking())
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    saved = store.get(row["id"])
    assert saved["state"]["history"][0]["messages"][-1]["_reasoning"] == "private-thought"
    # Fresh worker, same stored conversation; history tool must not return private reasoning.
    runner = Runner(store, Model(call("harness_history", offset=0, limit=1), "Done"))
    send(runner, saved)
    await runner.tick(row["id"])
    await runner.tick(row["id"])
    saved = store.get(row["id"])
    result = next(m for m in saved["state"]["run"]["frames"][0]["messages"] if m["role"] == "tool")
    assert result["role"] == "tool" and "private-thought" not in result["content"]
    # Large active reasoning cannot bypass the same input-capacity check as answer text.
    saved["state"]["run"]["frames"][0]["messages"].append(
        {"role": "assistant", "content": "", "_reasoning": "x" * 65000}
    )
    with pytest.raises(Conflict, match="Active context"):
        runner.context(saved, runner.agents["assistant"])


def test_thinking_can_be_explicitly_disabled_without_discarding_preserved_history():
    for family in ("gemma4", "qwen3_8"):
        profile = ExecutionProfile(id="disabled", family=family, enable_thinking=False)
        assert profile.request_options()["chat_template_kwargs"] == {
            "enable_thinking": False,
            "preserve_thinking": True,
        }
