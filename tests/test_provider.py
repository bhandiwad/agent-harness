import json

import httpx2
import pytest

from pandita_agent.agent.provider import Provider, anthropic_messages, openai_messages


@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages"])
async def test_provider_tool_call_and_followup_wire_contract(settings, protocol):
    model = settings.models[0].model_copy(update={"protocol": protocol})
    requests = []

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if protocol == "openai_chat":
            assert request.url.path == "/v1/chat/completions"
            assert request.headers["Authorization"] == "Bearer fixture-secret"
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "provider-call",
                                        "type": "function",
                                        "function": {
                                            "name": "context_lookup",
                                            "arguments": '{"query":"A17"}',
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                },
            )
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "fixture-secret"
        return httpx2.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "provider-call",
                        "name": "context_lookup",
                        "input": {"query": "A17"},
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 2},
            },
        )

    provider = Provider({"FIXTURE_KEY": "fixture-secret"}, transport=httpx2.MockTransport(handle))
    answer = await provider.generate(
        model,
        "System",
        [{"role": "user", "content": "Find A17"}],
        [{"name": "context_lookup", "description": "Context", "schema": {"type": "object"}}],
        100,
        10,
    )
    assert answer.call.name == "context_lookup" and answer.call.arguments == {"query": "A17"}
    assert answer.input_tokens == (10 if protocol == "openai_chat" else 12)
    assert "fixture-secret" not in json.dumps(requests)


def test_both_protocols_preserve_atomic_tool_call_result_pairs():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "call_id": "call-1",
            "call": {"name": "context_lookup", "arguments": {"query": "A17"}},
        },
        {"role": "tool", "call_id": "call-1", "content": '{"value":8}'},
    ]
    assert openai_messages(messages)[1]["tool_call_id"] == "call-1"
    assert anthropic_messages(messages)[1]["content"][0]["tool_use_id"] == "call-1"


@pytest.mark.parametrize("status", [302, 401, 429, 503])
async def test_provider_does_not_retry_or_follow_redirects(settings, status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx2.Response(status, headers={"location": "https://unapproved.invalid"})

    provider = Provider({"FIXTURE_KEY": "test"}, transport=httpx2.MockTransport(handle))
    with pytest.raises(ValueError):
        await provider.generate(
            settings.models[0], "", [{"role": "user", "content": "test"}], [], 100, 10
        )
    assert len(calls) == 1
