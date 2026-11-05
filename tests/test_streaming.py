import json

import httpx2
import pytest

from pandita_agent.agent.provider import Provider
from pandita_agent.agent.runner import Runner

from .test_runner import new, send


def sse(items):
    return "".join(
        "data: " + (json.dumps(item) if item is not None else "[DONE]") + "\n\n" for item in items
    ).encode()


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages", "openai_responses"])
async def test_streamed_text_is_persisted_before_complete_and_usage_is_counted(store, protocol):
    settings = store.settings
    store.settings = settings.model_copy(
        update={
            "models": (
                settings.models[0].model_copy(update={"protocol": protocol, "stream": True}),
            )
        }
    )
    observed = []
    row = None

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            if protocol == "openai_chat":
                yield sse(
                    [
                        {
                            "choices": [
                                {"index": 0, "delta": {"content": "Hello "}, "finish_reason": None}
                            ]
                        }
                    ]
                )
                observed.append(store.get(row["id"])["state"]["run"]["stream"]["text"])
                yield sse(
                    [
                        {
                            "choices": [
                                {"index": 0, "delta": {"content": "world"}, "finish_reason": "stop"}
                            ]
                        },
                        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3}},
                        None,
                    ]
                )
            elif protocol == "anthropic_messages":
                yield sse(
                    [
                        {
                            "type": "message_start",
                            "message": {"usage": {"input_tokens": 10, "output_tokens": 0}},
                        },
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": "Hello "},
                        },
                    ]
                )
                observed.append(store.get(row["id"])["state"]["run"]["stream"]["text"])
                yield sse(
                    [
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": "world"},
                        },
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn"},
                            "usage": {"output_tokens": 3},
                        },
                        {"type": "message_stop"},
                    ]
                )
            else:
                yield sse([{"type": "response.output_text.delta", "delta": "Hello "}])
                observed.append(store.get(row["id"])["state"]["run"]["stream"]["text"])
                yield sse(
                    [
                        {"type": "response.output_text.delta", "delta": "world"},
                        {
                            "type": "response.completed",
                            "response": {
                                "status": "completed",
                                "output": [
                                    {
                                        "type": "message",
                                        "role": "assistant",
                                        "content": [{"type": "output_text", "text": "Hello world"}],
                                    }
                                ],
                                "usage": {"input_tokens": 10, "output_tokens": 3},
                            },
                        },
                    ]
                )

    def handle(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json={"input_tokens": 10})
        assert json.loads(request.content)["stream"] is True
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    runner = Runner(
        store, Provider({"FIXTURE_KEY": "fixture"}, transport=httpx2.MockTransport(handle))
    )
    row = new(runner)
    send(runner, row, "Hello")
    await runner.tick(row["id"])
    state = store.get(row["id"])["state"]
    assert observed == ["Hello "]
    assert state["status"] == "completed"
    assert state["history"][-1]["messages"][-1]["content"] == "Hello world"
    assert state["run"]["output_tokens"] == 3
    assert any(e["kind"] == "model.delta" for e in store.events(row["id"]))


async def test_disconnected_stream_is_incomplete_and_does_not_execute_partial_tool(store):
    model = store.settings.models[0].model_copy(update={"stream": True})
    store.settings = store.settings.model_copy(update={"models": (model,)})

    def handle(request):
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "Unfinished answer"},
                                "finish_reason": None,
                            }
                        ]
                    }
                ]
            ),
        )

    runner = Runner(
        store, Provider({"FIXTURE_KEY": "fixture"}, transport=httpx2.MockTransport(handle))
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    state = store.get(row["id"])["state"]
    assert state["status"] == "failed" and state["history"] == []
    assert state["run"]["stream"]["text"] == "Unfinished answer"


async def test_cancel_during_stream_stops_before_final_answer_or_tool_execution(store):
    from uuid import uuid4

    from pandita_agent.agent.contracts import Command

    model = store.settings.models[0].model_copy(update={"stream": True})
    store.settings = store.settings.model_copy(update={"models": (model,)})

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield sse([{"choices": [{"index": 0, "delta": {"content": "First text "}}]}])
            current = store.get(row["id"])
            runner.cancel(
                row["id"],
                Command(command_id=uuid4(), expected_revision=current["revision"]),
                "alice",
            )
            yield sse([{"choices": [{"index": 0, "delta": {"content": "x" * 300}}]}])
            raise AssertionError("Cancelled stream was consumed beyond the next bounded delta")

    def handle(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    runner = Runner(
        store, Provider({"FIXTURE_KEY": "fixture"}, transport=httpx2.MockTransport(handle))
    )
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    state = store.get(row["id"])["state"]
    assert state["status"] == "cancelled" and not state["history"]
    assert state["run"]["stream"]["text"] == "First text "
    events = store.events(row["id"])
    assert events[-1]["kind"] == "run.cancelled"
    assert not any(event["data"].get("delta") == "x" * 300 for event in events)
