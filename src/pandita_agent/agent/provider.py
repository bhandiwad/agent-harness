"""Provider-neutral tool loop boundary; no redirects, ambient proxies or automatic retries."""

import json

import httpx2

from pandita_agent.agent import chat_reasoning, native_context
from pandita_agent.agent.contracts import Call, ModelResult
from pandita_agent.agent.model_stream import collect
from pandita_agent.contracts import canonical


def openai_messages(messages, execution=None):
    result = []
    for item in chat_reasoning.select(messages, execution):
        row = {"role": item["role"], "content": item.get("content", "")}
        if item.get("_reasoning"):
            row["reasoning_content"] = item["_reasoning"]
        if item.get("call"):
            row["tool_calls"] = [
                {
                    "id": item.get("provider_call_id", item["call_id"]),
                    "type": "function",
                    "function": {
                        "name": item["call"]["name"],
                        "arguments": canonical(item["call"]["arguments"]),
                    },
                }
            ]
        if item["role"] == "tool":
            row["tool_call_id"] = item.get("provider_call_id", item["call_id"])
        result.append(row)
    return result


def anthropic_messages(messages):
    result = []
    for item in messages:
        if item["role"] == "tool":
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": item["call_id"],
                            "content": item["content"],
                        }
                    ],
                }
            )
        elif item.get("call"):
            blocks = [{"type": "text", "text": item["content"]}] if item.get("content") else []
            blocks.append(
                {
                    "type": "tool_use",
                    "id": item["call_id"],
                    "name": item["call"]["name"],
                    "input": item["call"]["arguments"],
                }
            )
            result.append({"role": "assistant", "content": blocks})
        else:
            result.append({"role": item["role"], "content": item["content"]})
    return result


class Provider:
    extended_protocol = True

    def __init__(self, environ, *, transport=None):
        self.environ, self.transport = environ, transport

    async def generate(
        self,
        model,
        instructions,
        messages,
        tools,
        max_tokens,
        request_timeout,
        *,
        on_delta=None,
        provider_context=None,
        remaining_tokens=None,
        context_limit=None,
        safety_tokens=1024,
    ):
        token = self.environ.get(model.api_key_env, "")
        if not token or any(char in token for char in ("\r", "\n")):
            raise ValueError("Model credentials unavailable.")
        if model.protocol == "openai_responses":
            return await self.responses(
                model,
                instructions,
                messages,
                tools,
                max_tokens,
                request_timeout,
                token,
                on_delta,
                provider_context,
                remaining_tokens,
                context_limit,
                safety_tokens,
            )
        if model.protocol == "openai_chat":
            path, headers = "/chat/completions", {"Authorization": "Bearer " + token}
            body = {
                "model": model.model_name,
                "messages": [
                    {"role": "system", "content": instructions},
                    *openai_messages(messages, model.execution),
                ],
                "max_tokens": max_tokens,
            }
            if tools:
                body.update(
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": tool["name"],
                                "description": tool["description"],
                                "parameters": tool["schema"],
                            },
                        }
                        for tool in tools
                    ],
                    parallel_tool_calls=False,
                )
        else:
            path, headers = "/messages", {"x-api-key": token, "anthropic-version": "2023-06-01"}
            body = {
                "model": model.model_name,
                "system": instructions,
                "messages": anthropic_messages(messages),
                "max_tokens": max_tokens,
            }
            if tools:
                body.update(
                    tools=[
                        {
                            "name": tool["name"],
                            "description": tool["description"],
                            "input_schema": tool["schema"],
                        }
                        for tool in tools
                    ],
                    tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                )
        if model.execution is not None:
            body.update(model.execution.request_options())
        if model.stream:
            body["stream"] = True
            if model.protocol == "openai_chat":
                body["stream_options"] = {"include_usage": True}
        async with httpx2.AsyncClient(
            timeout=request_timeout,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            async with client.stream(
                "POST", model.base_url.rstrip("/") + path, headers=headers, json=body
            ) as response:
                if response.status_code != 200:
                    raise ValueError("Model request did not complete.")
                if model.stream:
                    if not response.headers.get("content-type", "").startswith("text/event-stream"):
                        raise ValueError("Provider did not return the requested stream.")
                    data = await collect(response, model.protocol, on_delta)
                else:
                    data = await self.json(response)
        if model.protocol == "openai_chat":
            if len(data["choices"]) != 1 or data["choices"][0]["finish_reason"] not in {
                "stop",
                "tool_calls",
            }:
                raise ValueError("Incomplete model response.")
            message = data["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if len(calls) > 1 or message.get("refusal"):
                raise ValueError("Unsupported model response.")
            call = None
            if calls:
                call = Call.model_validate_json(
                    canonical(
                        {
                            "name": calls[0]["function"]["name"],
                            "arguments": json.loads(calls[0]["function"]["arguments"]),
                        }
                    )
                )
            return ModelResult(
                text=message.get("content") or "",
                reasoning=chat_reasoning.extract(message) if model.execution else "",
                provider_call_id=calls[0].get("id") if calls else None,
                call=call,
                input_tokens=data["usage"]["prompt_tokens"],
                output_tokens=data["usage"]["completion_tokens"],
                cached_tokens=data["usage"]
                .get("prompt_tokens_details", {})
                .get("cached_tokens", 0),
                reasoning_tokens=data["usage"]
                .get("completion_tokens_details", {})
                .get("reasoning_tokens", 0),
            )
        if data["stop_reason"] not in {"end_turn", "tool_use"}:
            raise ValueError("Incomplete model response.")
        blocks = data["content"]
        if any(block["type"] not in {"text", "tool_use"} for block in blocks):
            raise ValueError("Unsupported model content.")
        calls = [block for block in blocks if block["type"] == "tool_use"]
        if len(calls) > 1:
            raise ValueError("Parallel tool calls are not enabled.")
        return ModelResult(
            text="\n".join(block["text"] for block in blocks if block["type"] == "text"),
            call=Call.model_validate_json(
                canonical({"name": calls[0]["name"], "arguments": calls[0]["input"]})
            )
            if calls
            else None,
            input_tokens=data["usage"]["input_tokens"]
            + data["usage"].get("cache_read_input_tokens", 0)
            + data["usage"].get("cache_creation_input_tokens", 0),
            output_tokens=data["usage"]["output_tokens"],
        )

    @staticmethod
    async def json(response):
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > 1048576:
                raise ValueError("Provider response exceeds its byte limit.")
        return json.loads(raw)

    async def responses(
        self,
        model,
        instructions,
        messages,
        tools,
        max_tokens,
        request_timeout,
        token,
        on_delta,
        previous,
        remaining_tokens,
        context_limit,
        safety_tokens,
    ):
        body = native_context.request(model, instructions, messages, tools, previous)
        headers = {"Authorization": "Bearer " + token}
        async with httpx2.AsyncClient(
            timeout=request_timeout,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            count_body = {
                key: body[key] for key in ("model", "instructions", "input", "tools", "truncation")
            }
            async with client.stream(
                "POST",
                model.base_url.rstrip("/") + "/responses/input_tokens",
                headers=headers,
                json=count_body,
            ) as response:
                if response.status_code != 200:
                    raise ValueError(
                        "Native token preflight is unavailable; no silent history fallback."
                    )
                count = (await self.json(response))["input_tokens"]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("Invalid native token count.")
            if context_limit is not None and count + max_tokens + safety_tokens > context_limit:
                raise ValueError(
                    "Native request exceeds serving capacity; adjust compaction threshold before continuing."
                )
            if remaining_tokens is not None:
                max_tokens = min(max_tokens, remaining_tokens - count)
            if max_tokens <= 0:
                raise ValueError("Native input exhausts the remaining run budget.")
            body["max_output_tokens"] = max_tokens
            body["stream"] = model.stream
            async with client.stream(
                "POST", model.base_url.rstrip("/") + "/responses", headers=headers, json=body
            ) as response:
                if response.status_code != 200:
                    raise ValueError("Responses generation did not complete.")
                if model.stream:
                    if not response.headers.get("content-type", "").startswith("text/event-stream"):
                        raise ValueError("Provider did not return a Responses stream.")
                    data = await collect(response, "openai_responses", on_delta)
                else:
                    data = await self.json(response)
        return native_context.result(model, data, body, len(messages))
