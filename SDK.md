# Python SDK

Install this package and import `pandita_agent.agent.sdk`. The service remains independently deployed: this is a Python service client and operator authoring SDK. It is not an embedded replacement for the API/worker, and there is no TypeScript SDK in this version.

```python
from pathlib import Path
from pandita_agent.agent.sdk import Client, RevisionConflict, TransportError

# The callable rereads a refreshed short-lived token on each HTTP request.
with Client('http://127.0.0.1:8093',
            lambda: Path('/private/runtime/user.jwt').read_text().strip()) as sdk:
    session = sdk.create_session('assistant', 'Asset investigation')
    command = sdk.prepare_turn(session, 'Which pumps belong to A17? Cite sources.')
    session = sdk.submit(command)
    for event in sdk.stream(session.id, after=session.revision):
        if event.kind == 'model.delta':
            print(event.data.get('delta', ''), end='', flush=True)
    session = sdk.session(session.id)
    print(session.status, session.answer)
    # Once completed, a follow-up uses the SAME session:
    if session.status == 'completed':
        sdk.submit(sdk.prepare_turn(session, 'What maintenance interval applies?'))
```

Use `AsyncClient` with `async with`, `await` for requests and `async for` for streaming. Both clients expose `agents`, `capabilities`, `create_session`, `session`, `events`, `download`, `wait`, `stream` and `submit`. Author commands with `create`, `prepare_turn`, `prepare_cancel`, `prepare_upload`, `prepare_decision`, or `prepare(session, operation, **fields)` for the remaining supported commands. `prepare_decision` binds the currently pending digest; the server still requires an independent authorized reviewer. `prepare_upload` accepts the prior file's `expected_digest` for replacement.

Sessions and events have typed Pydantic contracts. `Session.status` reflects the authoritative state; `Session.answer` is available for completed runs. `wait` and `stream` stop at a completed or paused state by default, including clarification and approval. Inspect the status and act accordingly. A waiting approval is not a completed answer. Text delta events are provisional; use the committed final answer as the result.

Streams reconnect a bounded number of times, pass the last delivered revision, discard replayed duplicates and refresh the bearer token on every connection. `StreamClosed.after` and `WaitTimeout.after` retain the resume cursor. Access revocation stops the stream. Deadlines are checked between reads and constrain request timeouts; they do not cancel the remote run. Use an explicit cancel command if that is intended. Poll `wait` if streaming is unnecessary.

Writes never retry automatically. Keep the prepared command until its outcome is known:

```python
from pandita_agent.agent.sdk import PreparedCommand, TransportError
try:
    result = sdk.submit(command)
except TransportError:
    # Preserve this exact envelope in your application's protected state.
    serialized = command.dumps()
    same_command = PreparedCommand.loads(serialized)
    # Retry later using sdk.submit(same_command), not a fresh command ID.
```

A real `RevisionConflict` means reload and review the latest state before preparing a new command. Typed exceptions include `AuthenticationError`, `PermissionDenied`, `NotFound`, `InvalidRequest`, `Unavailable`, `RevisionConflict`, `TransportError`, `WaitTimeout` and `StreamClosed`. Error messages omit server response bodies. Prepared command payloads may contain private user data; they are not encrypted by the SDK.

The original dictionary-based `get`, `create`, `prepare` and `execute` methods remain available. A prepared command now owns an immutable serialized envelope; changing a returned `.body` dictionary does not alter it.

Authoring exports include `Agent`, `Model`, `Settings`, `Capacity`, `Connection`, `ToolSpec`, `pin_skill`, `pin_instructions`, `rest_tool`, `mcp_tool`, `load_settings` and `write_config`. See `examples/author_agent.py` and `CAPABILITIES.md`. Installing a capability, mounting credentials and granting access remain operator decisions.

Version 0.3.1 returns a `workspace_conflict` tool result and `native.rejected` event when a model proposes a stale local file edit. The file remains unchanged. The model can read it and propose a new digest-checked edit within the same run budget. This does not retry external writes or relax approval, permission or concurrency controls.
