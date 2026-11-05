"""Bounded private Chat reasoning; serving templates own token formatting."""

MAX_REASONING = 65536


def extract(message):
    values = [
        message[key] for key in ("reasoning_content", "reasoning") if message.get(key) is not None
    ]
    if any(not isinstance(value, str) for value in values):
        raise ValueError("Invalid model reasoning field.")
    nonempty = [value for value in values if value]
    if len(set(nonempty)) > 1:
        raise ValueError("Conflicting model reasoning fields.")
    value = nonempty[0] if nonempty else ""
    if len(value) > MAX_REASONING:
        raise ValueError("Model reasoning exceeds its limit.")
    return value


def select(messages, execution):
    """Keep tool thoughts for Gemma, all retained thoughts for Qwen; never mutate state."""
    last_user = max((i for i, row in enumerate(messages) if row["role"] == "user"), default=-1)
    result = []
    for index, message in enumerate(messages):
        row = dict(message)
        if (
            not execution
            or row["role"] != "assistant"
            or (execution.family == "gemma4" and index < last_user and not row.get("call"))
        ):
            row.pop("_reasoning", None)
        result.append(row)
    return result


def visible(messages):
    return [{key: value for key, value in row.items() if key != "_reasoning"} for row in messages]
