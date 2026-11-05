"""Bounded SSE framing independent of transport chunk boundaries."""

import codecs
import json


async def events(response, *, limit=2097152):
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer, data, event = "", [], ""
    total = count = data_size = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise ValueError("Event stream exceeded its byte limit.")
        lines = (buffer + decoder.decode(chunk)).split("\n")
        buffer = lines.pop()
        if len(buffer) > 262144:
            raise ValueError("Event line exceeded its limit.")
        for line in lines:
            if len(line) > 262144:
                raise ValueError("Event line exceeded its limit.")
            line = line.removesuffix("\r")
            if not line:
                if data:
                    count += 1
                    if count > 8192:
                        raise ValueError("Too many stream events.")
                    joined = "\n".join(data)
                    yield event, None if joined == "[DONE]" else json.loads(joined)
                data, event, data_size = [], "", 0
            elif line.startswith("data:"):
                value = line[5:].removeprefix(" ")
                data_size += len(value) + bool(data)
                if data_size > 262144:
                    raise ValueError("Event exceeded its limit.")
                data.append(value)
            elif line.startswith("event:"):
                event = line[6:].strip()
    tail = decoder.decode(b"", final=True)
    if buffer.strip() or data or tail:
        raise ValueError("Incomplete event stream.")
