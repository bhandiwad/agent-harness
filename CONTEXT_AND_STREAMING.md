# Provider-native context and streaming

The OpenAI Responses adapter retains opaque reasoning items and supports native automatic compaction. This is separate from visible conversation history, user-pinned facts, retrieval, and prompt caching. It is not a text summarizer and does not recreate provider reasoning for other model families. Public references: [reasoning retention](https://developers.openai.com/api/docs/guides/reasoning), [native compaction](https://developers.openai.com/api/docs/guides/compaction), [token counting](https://developers.openai.com/api/docs/guides/token-counting).

## Configure

Replace the model entry with the values in `config/models.responses.example.json`, supplying a real model name and its verified serving capacity. `reasoning_context: all_turns` requests cross-turn retention; the adapter requires the response to confirm that setting. Support is model-specific. `auto` leaves the choice to the provider; `current_turn` restricts retention accordingly. Unsupported settings fail, without switching models or discarding history. The Responses endpoint must also implement `/responses/input_tokens`.

`compact_threshold` enables the provider's `context_management` compaction. Choose a threshold below the verified context limit, leaving room for the largest tool result, instructions, output reservation and safety margin. The configured example is illustrative, not a model capacity claim. Each generation receives an exact native token-count preflight; an oversized request stops before generation. That preflight adds a network round trip. This implementation uses automatic compaction in Responses, not the separate `/responses/compact` endpoint.

## What survives a loop and restart

The harness sends `store: false`, requests encrypted reasoning content and replays returned output items verbatim, including tool-call IDs and message phase. It stores opaque provider state separately from the visible transcript in the same tenant/workspace-protected PostgreSQL session. After automatic compaction, it retains the newest compaction item and later items for the next request. Full visible history remains available for audit/history retrieval. Opaque state is not returned through session APIs, events or the chat UI; database operators must still protect backups and access.

New turns append to that native context. Specialist frames have separate native state; the parent receives their final result. Current system instructions, pinned skills, plan and user facts are supplied each time. Configuration changes require a new session, preventing cross-model reuse of incompatible encrypted state. A lost provider response is marked interrupted and is not silently regenerated. Context retention is not unlimited persistence: session storage remains bounded to 2 MiB, including transcript/files/native state, with a 1 MiB native-state bound.

For `openai_chat` and `anthropic_messages`, the existing bounded transcript strategy remains available: older complete turns can leave the prompt while remaining in history. This is not native reasoning retention. Claude extended-thinking replay and Claude-native compaction are not implemented by this adapter.

## Streaming and accounting

Set `stream: true` for OpenAI Chat Completions, OpenAI Responses or Anthropic Messages. Provider text deltas become authenticated, durable `model.delta` events and provisional chat text. The UI reconnects by revision and reloads stored state. Partial tool arguments never execute. A complete provider response with usage accounting is required before committing the final answer or dispatching a tool. Cancellation stops at the next delta or request timeout; it cannot retract tokens already generated upstream. A broken stream remains an incomplete draft.

Input/output token totals, cached input tokens and reasoning output tokens are recorded separately when supplied by the provider. Cached tokens are a subset of input usage; reasoning tokens are a subset of output usage, not additional totals. Native compaction events and effective reasoning-context mode appear in the trace. Prompt caching is provider behavior; recording its counters does not guarantee a cache hit.

Validate actual savings with the same tasks, model/settings and tool results, comparing full-history, retained-reasoning and retained-reasoning-plus-compaction runs. Measure total billed usage, new reasoning tokens, cached input, wall time and task correctness. No fixed reduction factor is claimed: fixtures verify state handling, not model quality or efficiency. No paid model call is made automatically by the package.
