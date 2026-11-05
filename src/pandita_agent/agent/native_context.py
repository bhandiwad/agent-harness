"""Opaque Responses context; native compaction is distinct from visible chat history."""

import json

from pandita_agent.contracts import canonical, content_digest


def input_items(messages):
    result = []
    for message in messages:
        if message["role"] == "tool":
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("provider_call_id", message["call_id"]),
                    "output": message["content"],
                }
            )
        elif message.get("call"):
            if message.get("content"):
                result.append({"role": "assistant", "content": message["content"]})
            result.append(
                {
                    "type": "function_call",
                    "call_id": message.get("provider_call_id", message["call_id"]),
                    "name": message["call"]["name"],
                    "arguments": canonical(message["call"]["arguments"]),
                }
            )
        else:
            result.append({"role": message["role"], "content": message.get("content", "")})
    return result


def carry(frame):
    previous = frame.get("provider_context")
    if previous is None:
        return None
    return {
        **previous,
        "items": previous["items"] + input_items(frame["messages"][previous["through"] :]),
        "through": 0,
    }


def request(model, instructions, messages, tools, previous):
    digest = content_digest(model.model_dump(mode="json"))
    if previous and previous["model_digest"] != digest:
        raise ValueError("Native context belongs to another model configuration.")
    items = list(previous["items"]) if previous else []
    offset = previous["through"] if previous else 0
    if not 0 <= offset <= len(messages):
        raise ValueError("Native transcript cursor is invalid.")
    items += input_items(messages[offset:])
    body = {
        "model": model.model_name,
        "instructions": instructions,
        "input": items,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "truncation": "disabled",
        "reasoning": {"context": model.reasoning_context},
        "parallel_tool_calls": False,
        "tools": [
            {
                "type": "function",
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["schema"],
                "strict": False,
            }
            for tool in tools
        ],
    }
    if model.compact_threshold is not None:
        body["context_management"] = [
            {"type": "compaction", "compact_threshold": model.compact_threshold}
        ]
    return body


def result(model, data, body, message_count):
    from pandita_agent.agent.contracts import Call, ModelResult

    if data.get("status") != "completed" or not isinstance(data.get("output"), list):
        raise ValueError("Responses generation did not complete.")
    output = data["output"]
    text, calls = [], []
    for item in output:
        kind = item.get("type")
        if kind == "message":
            if item.get("role") != "assistant" or item.get("status", "completed") != "completed":
                raise ValueError("Invalid Responses message.")
            for block in item.get("content", []):
                if block.get("type") != "output_text":
                    raise ValueError("Unsupported or refused Responses content.")
                text.append(block["text"])
        elif kind == "function_call":
            calls.append(item)
        elif kind in {"reasoning", "compaction"}:
            if not isinstance(item.get("encrypted_content"), str) or not item["encrypted_content"]:
                raise ValueError("Replayable native context was not returned.")
            # Opaque items are retained verbatim; never summarize, decode or expose them.
        else:
            raise ValueError("Provider returned a tool or item outside the configured runtime.")
    if len(calls) > 1:
        raise ValueError("Parallel tool calls are disabled.")
    items = body["input"] + output
    boundaries = [index for index, item in enumerate(items) if item.get("type") == "compaction"]
    if boundaries:
        items = items[boundaries[-1] :]
    if len(canonical(items).encode()) > 1048576:
        raise ValueError("Native state exceeds its storage limit.")
    usage = data["usage"]
    effective = (data.get("reasoning") or {}).get("context")
    if model.reasoning_context == "all_turns" and effective != "all_turns":
        raise ValueError("Model did not confirm requested cross-turn reasoning retention.")
    return ModelResult(
        text="\n".join(text),
        call=Call.model_validate_json(
            canonical({"name": calls[0]["name"], "arguments": json.loads(calls[0]["arguments"])})
        )
        if calls
        else None,
        provider_call_id=calls[0]["call_id"] if calls else None,
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        cached_tokens=usage.get("input_tokens_details", {}).get("cached_tokens", 0),
        reasoning_tokens=usage.get("output_tokens_details", {}).get("reasoning_tokens", 0),
        provider_context={
            "model_digest": content_digest(model.model_dump(mode="json")),
            "items": items,
            "through": message_count + 1,
        },
        effective_reasoning_context=effective,
    )
