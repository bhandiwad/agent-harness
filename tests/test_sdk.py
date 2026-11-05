import json
from uuid import uuid4

import httpx2
import pytest

from pandita_agent.agent.api import create_app
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.sdk import (
    AsyncClient,
    Client,
    PermissionDenied,
    PreparedCommand,
    RevisionConflict,
    StreamClosed,
    TransportError,
    WaitTimeout,
)
from pandita_agent.agent.sdk_types import EventDecoder

from .test_runner import Model


def snapshot(identifier, status="completed", revision=3):
    return {
        "id": str(identifier),
        "revision": revision,
        "owner": "alice",
        "state": {"status": status, "history": []},
    }


def wire(revision, kind="model.delta"):
    event = {
        "revision": revision,
        "kind": kind,
        "actor": "worker",
        "data": {"text": "é"},
        "created_at": "2026-10-23T00:00:00Z",
    }
    return (
        "event: " + kind + "\r\ndata: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n"
    ).encode()


class Chunks(httpx2.SyncByteStream, httpx2.AsyncByteStream):
    def __init__(self, *values):
        self.values = values

    def __iter__(self):
        yield from self.values

    async def __aiter__(self):
        for value in self.values:
            yield value


def test_decoder_chunked_utf8_crlf_and_bounds():
    decoder = EventDecoder()
    result = []
    for byte in wire(1):
        result.extend(decoder.feed(bytes([byte])))
    decoder.finish()
    assert result[0][1]["data"]["text"] == "é"
    with pytest.raises(ValueError):
        decoder.feed(b"x" * 262145)


async def test_sdk_real_api_command_retry_followup_and_conflict(store, identity):
    runner = Runner(store, Model("First answer.", "Follow-up answer."))
    async with AsyncClient(
        "http://127.0.0.1", identity(), transport=httpx2.ASGITransport(app=create_app(runner))
    ) as sdk:
        creation = sdk.create("assistant")
        first = await sdk.submit(creation)
        assert (await sdk.submit(PreparedCommand.loads(creation.dumps()))).id == first.id
        command = sdk.prepare_turn(first, "Hello")
        assert command.body == PreparedCommand.loads(command.dumps()).body
        body = command.body
        body["text"] = "tampered"
        assert command.body["text"] == "Hello"
        await sdk.submit(command)
        await runner.tick(first.id)
        completed = await sdk.wait(first.id, timeout=1)
        assert completed.answer == "First answer."
        with pytest.raises(RevisionConflict):
            await sdk.submit(sdk.prepare_turn(first, "Stale"))
        await sdk.submit(sdk.prepare_turn(completed, "And next?"))
        await runner.tick(first.id)
        assert (await sdk.session(first.id)).answer == "Follow-up answer."
        assert len(await sdk.events(first.id)) >= 5


def test_sync_retry_preserves_envelope_no_automatic_write_and_redacts_error():
    calls = []
    identifier = uuid4()

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            raise httpx2.ReadError("lost connection", request=request)
        return httpx2.Response(201, json=snapshot(identifier))

    with Client(
        "http://127.0.0.1", "private-token", transport=httpx2.MockTransport(handler)
    ) as sdk:
        command = sdk.create("assistant", title="private-title")
        with pytest.raises(TransportError) as error:
            sdk.submit(command)
        assert len(calls) == 1 and error.value.command is command
        assert "private" not in str(error.value) and "private-title" not in repr(command)
        assert sdk.submit(PreparedCommand.loads(command.dumps())).id == identifier
        assert calls[0] == calls[1]
        with pytest.raises(ValueError):
            sdk.get("https://evil.invalid/v1/sessions")
        with pytest.raises(ValueError):
            sdk.capabilities("../secret")
    with Client(
        "http://127.0.0.1",
        "token",
        transport=httpx2.MockTransport(lambda _: httpx2.Response(403, json={"detail": "PRIVATE"})),
    ) as sdk:
        with pytest.raises(PermissionDenied) as error:
            sdk.agents()
        assert error.value.status_code == 403 and "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_stream_reconnect_cursor_dedupe_refresh_and_complete(asynchronous):
    identifier = uuid4()
    cursors = []
    tokens = []
    counter = iter(range(20))

    def handler(request):
        tokens.append(request.headers["Authorization"])
        if not request.url.path.endswith("/stream"):
            return httpx2.Response(200, json=snapshot(identifier))
        cursors.append(int(request.url.params["after"]))
        data = (
            (wire(1), wire(2)[:20])
            if len(cursors) == 1
            else (wire(1), wire(2), wire(3, "model.completed"))
        )
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Chunks(*data)
        )

    options = {"transport": httpx2.MockTransport(handler)}
    if asynchronous:
        async with AsyncClient(
            "http://127.0.0.1", lambda: "token-" + str(next(counter)), **options
        ) as sdk:
            result = [e.revision async for e in sdk.stream(identifier, timeout=2)]
    else:
        with Client("http://127.0.0.1", lambda: "token-" + str(next(counter)), **options) as sdk:
            result = [e.revision for e in sdk.stream(identifier, timeout=2)]
    assert result == [1, 2, 3] and cursors == [0, 1]
    assert len(set(tokens)) == len(tokens)


async def test_wait_timeout_and_access_change_never_cancel_or_retry_writes():
    identifier = uuid4()
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.url.path.endswith("/stream"):
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"event: access.closed\ndata: {}\n\n",
            )
        return httpx2.Response(200, json=snapshot(identifier, "running"))

    async with AsyncClient(
        "http://127.0.0.1", "token", transport=httpx2.MockTransport(handler)
    ) as sdk:
        with pytest.raises(WaitTimeout):
            await sdk.wait(identifier, timeout=0.03, poll_interval=0.01)
        with pytest.raises(StreamClosed) as error:
            async for _ in sdk.stream(identifier):
                pass
        assert error.value.after == 0
    assert set(calls) == {"GET"}
