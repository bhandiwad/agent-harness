"""Provider event assembly. Text is provisional; no partial tool arguments are executable."""

import time

from pandita_agent.agent.chat_reasoning import MAX_REASONING, extract
from pandita_agent.agent.sse import events


async def collect(response, protocol, on_delta):
    text, parts, finish, usage, complete = "", {}, None, None, False
    native = None
    reasoning = ""
    buffer, last_emit = "", 0.0

    async def emit(delta, force=False):
        nonlocal buffer, last_emit
        buffer += delta
        if len(buffer) > 65536:
            raise ValueError("Stream text exceeded its limit.")
        now = time.monotonic()
        if buffer and (force or len(buffer) >= 256 or now - last_emit >= 0.05):
            if on_delta:
                await on_delta(buffer)
            buffer, last_emit = "", now

    async for event, data in events(response):
        if data is None:
            if protocol != "openai_chat":
                raise ValueError("Unexpected stream terminator.")
            complete = True
            break
        if protocol == "openai_chat":
            if "error" in data or len(data.get("choices", [])) > 1:
                raise ValueError("Model stream failed.")
            if data.get("usage"):
                usage = data["usage"]
            for choice in data.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise ValueError("Unexpected stream choice.")
                delta = choice.get("delta", {})
                if delta.get("refusal"):
                    raise ValueError("Model refused this response.")
                reasoning += extract(delta)
                if len(reasoning) > MAX_REASONING:
                    raise ValueError("Model reasoning exceeds its limit.")
                value = delta.get("content") or ""
                text += value
                await emit(value)
                for call in delta.get("tool_calls", []):
                    index = call["index"]
                    if index != 0:
                        raise ValueError("Parallel tool calls are disabled.")
                    part = parts.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    part["id"] += call.get("id", "")
                    for name in ("name", "arguments"):
                        part["function"][name] += call.get("function", {}).get(name, "")
                finish = choice.get("finish_reason") or finish
        elif protocol == "anthropic_messages":
            kind = data.get("type", event)
            if kind == "error":
                raise ValueError("Model stream failed.")
            if kind == "message_start":
                usage = data["message"]["usage"]
            elif kind == "content_block_start":
                block = data["content_block"]
                if block["type"] not in {"text", "tool_use"} or data["index"] in parts:
                    raise ValueError("Unsupported streamed content.")
                parts[data["index"]] = {**block, "_json": ""}
                if block["type"] == "text":
                    await emit(block.get("text", ""))
            elif kind == "content_block_delta":
                block, delta = parts[data["index"]], data["delta"]
                if delta["type"] == "text_delta" and block["type"] == "text":
                    block["text"] += delta["text"]
                    await emit(delta["text"])
                elif delta["type"] == "input_json_delta" and block["type"] == "tool_use":
                    block["_json"] += delta["partial_json"]
                else:
                    raise ValueError("Unsupported streamed delta.")
            elif kind == "message_delta":
                finish = data["delta"].get("stop_reason")
                usage = {**(usage or {}), **data.get("usage", {})}
            elif kind == "message_stop":
                complete = True
                break
        else:
            kind = data.get("type", event)
            if kind in {"error", "response.failed", "response.incomplete"}:
                raise ValueError("Responses stream did not complete.")
            if kind == "response.output_text.delta":
                await emit(data["delta"])
            elif kind == "response.completed":
                native = data["response"]
                complete = True
                break
        if len(text) + len(reasoning) + sum(len(str(p)) for p in parts.values()) > 262144:
            raise ValueError("Accumulated model output exceeds its limit.")
    await emit("", force=True)
    if not complete:
        raise ValueError("Disconnected model stream; partial output is not a final answer.")
    if protocol == "openai_responses":
        return native
    if usage is None:
        raise ValueError("Model stream omitted usage accounting.")
    if protocol == "openai_chat":
        return {
            "choices": [
                {
                    "finish_reason": finish,
                    "message": {
                        "content": text,
                        "tool_calls": list(parts.values()),
                        "reasoning_content": reasoning,
                    },
                }
            ],
            "usage": usage,
        }
    import json

    for part in parts.values():
        encoded = part.pop("_json")
        if part["type"] == "tool_use" and encoded:
            part["input"] = json.loads(encoded)
    return {"stop_reason": finish, "content": [parts[key] for key in sorted(parts)], "usage": usage}
