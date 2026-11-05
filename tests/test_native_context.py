import json

import httpx2
import pytest

from pandita_agent.agent.api import create_app
from pandita_agent.agent.provider import Provider
from pandita_agent.agent.runner import Runner

from .test_runner import Adapter, new, send


async def test_responses_preserves_reasoning_compaction_tool_ids_restart_and_hides_opaque_state(
    store, identity
):
    model = store.settings.models[0].model_copy(
        update={
            "protocol": "openai_responses",
            "reasoning_context": "all_turns",
            "compact_threshold": 32000,
        }
    )
    store.settings = store.settings.model_copy(update={"models": (model,)})
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            assert body["truncation"] == "disabled"
            return httpx2.Response(200, json={"input_tokens": 120})
        assert request.url.path.endswith("/responses")
        assert body["store"] is False
        assert body["reasoning"] == {"context": "all_turns"}
        assert body["context_management"] == [{"type": "compaction", "compact_threshold": 32000}]
        bodies.append(body)
        if len(bodies) == 1:
            output = [
                {
                    "type": "reasoning",
                    "id": "r1",
                    "summary": [],
                    "encrypted_content": "OPAQUE-REASONING",
                },
                {
                    "type": "function_call",
                    "id": "f1",
                    "call_id": "provider-call-17",
                    "name": "context_lookup",
                    "arguments": '{"query":"A17"}',
                    "status": "completed",
                },
            ]
        elif len(bodies) == 2:
            assert any(
                item.get("encrypted_content") == "OPAQUE-REASONING" for item in body["input"]
            )
            result = body["input"][-1]
            assert (
                result["type"] == "function_call_output" and result["call_id"] == "provider-call-17"
            )
            output = [
                {"type": "compaction", "id": "c1", "encrypted_content": "OPAQUE-COMPACTION"},
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "8 pumps [fixture://assets/A17]",
                            "annotations": [],
                        }
                    ],
                },
            ]
        else:
            assert body["input"][0]["encrypted_content"] == "OPAQUE-COMPACTION"
            assert not any(
                item.get("encrypted_content") == "OPAQUE-REASONING" for item in body["input"]
            )
            assert body["input"][1]["phase"] == "final_answer"
            assert body["input"][-1]["content"] == "How many?"
            output = [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Previously, 8 pumps."}],
                }
            ]
        return httpx2.Response(
            200,
            json={
                "status": "completed",
                "output": output,
                "reasoning": {"context": "all_turns"},
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "input_tokens_details": {"cached_tokens": 40},
                    "output_tokens_details": {"reasoning_tokens": 15},
                },
            },
        )

    provider = Provider({"FIXTURE_KEY": "fixture"}, transport=httpx2.MockTransport(handle))
    runner = Runner(store, provider, adapters={"context_lookup": Adapter()})
    row = new(runner)
    send(runner, row)
    await runner.tick(row["id"])
    row = store.get(row["id"])
    assert row["state"]["status"] == "completed"
    assert row["state"]["run"]["cached_tokens"] == 80
    assert row["state"]["run"]["reasoning_tokens"] == 30
    runner = Runner(store, provider, adapters={"context_lookup": Adapter()})
    send(runner, row, "How many?")
    await runner.tick(row["id"])
    assert store.get(row["id"])["state"]["status"] == "completed"
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(runner)),
        base_url="http://test",
        headers={"Authorization": "Bearer " + identity()},
    ) as client:
        visible = await client.get("/v1/sessions/" + str(row["id"]))
        assert "OPAQUE" not in visible.text
        assert "8 pumps" in visible.text
        events = await client.get("/v1/sessions/" + str(row["id"]) + "/events")
        assert "OPAQUE" not in events.text


async def test_native_count_failure_never_silently_discards_reasoning(settings):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx2.Response(404)

    provider = Provider({"FIXTURE_KEY": "fixture"}, transport=httpx2.MockTransport(handle))
    model = settings.models[0].model_copy(update={"protocol": "openai_responses"})
    with pytest.raises(ValueError, match="preflight"):
        await provider.generate(
            model, "System", [{"role": "user", "content": "Question"}], [], 100, 10
        )
    assert calls == ["/v1/responses/input_tokens"]


@pytest.mark.parametrize("effective", [None, "current_turn"])
def test_cross_turn_retention_requires_provider_confirmation(settings, effective):
    from pandita_agent.agent import native_context

    model = settings.models[0].model_copy(
        update={"protocol": "openai_responses", "reasoning_context": "all_turns"}
    )
    body = native_context.request(model, "System", [], [], None)
    data = {
        "status": "completed",
        "output": [],
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "reasoning": {"context": effective},
    }
    with pytest.raises(ValueError, match="cross-turn"):
        native_context.result(model, data, body, 0)


async def test_native_exact_token_preflight_blocks_over_capacity_before_generation(settings):
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx2.Response(200, json={"input_tokens": 63999})

    provider = Provider({"FIXTURE_KEY": "fixture"}, transport=httpx2.MockTransport(handle))
    model = settings.models[0].model_copy(update={"protocol": "openai_responses"})
    with pytest.raises(ValueError, match="capacity"):
        await provider.generate(
            model,
            "System",
            [{"role": "user", "content": "Question"}],
            [],
            2048,
            10,
            context_limit=64000,
            remaining_tokens=100000,
        )
    assert calls == ["/v1/responses/input_tokens"]
