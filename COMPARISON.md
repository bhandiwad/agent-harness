# Comparison with public OpenAI and Claude agent runtimes

Reviewed 23 October 2026. This compares documented public products and SDKs; it does not describe the private implementation of ChatGPT, Codex or Claude. API names and availability can change.

OpenAI distinguishes managed Agents API sessions (OpenAI operates the harness), Agents SDK applications (you host the orchestration), and lower-level Responses integrations (your code owns more runtime behavior). Its documented managed harness covers orchestration, tool routing, context compaction and recovery. [OpenAI agent options](https://developers.openai.com/api/docs/guides/agents), [Agents API overview](https://developers.openai.com/api/docs/guides/agents-api/overview).

Claude's Agent SDK exposes the Claude Code agent loop, built-in tools, context/session management, permission controls and extension mechanisms. Its persisted sessions support resume and fork; session history is distinct from filesystem checkpoints. [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview), [sessions](https://code.claude.com/docs/en/agent-sdk/sessions), [permission controls](https://code.claude.com/docs/en/agent-sdk/permissions).

| Capability | Pandita generic harness now | Practical comparison / gap |
| --- | --- | --- |
| Deploy independently | API + worker + PostgreSQL; source package; no AgentFabric dependency | Application-hosted runtime, closer in deployment responsibility to an SDK application than a managed cloud harness |
| Multi-turn conversations | Durable transcript, follow-ups, clarification waits, reload/restart | Core session behavior exists; no conversation fork yet |
| Agent loop | Model → validated tool → model until answer/wait/budget | Same broad public agent-loop pattern; one tool per response, no parallel tool batch |
| Model choice | OpenAI Chat Completions, Responses and Anthropic Messages adapters | No use of managed Agents API or full Responses feature parity; actual model profiles still need qualification |
| Tools | Native catalog, fixed REST/MCP tools and isolated shell/Python/browser broker | Pinned HTTP MCP only; browser is offline; no full computer-use environment |
| Human decisions | Independent reviewer, exact digest, expiry, current permissions, durable wait | Explicit enterprise action policy; SDK permission callbacks and local interaction patterns vary by product |
| Context | Responses opaque reasoning retention, automatic native compaction, exact token preflight, pinned facts/skills and full visible history | Provider/model dependent; other adapters retain bounded transcript reduction; no Claude-native compaction or cross-model encrypted-state conversion |
| Specialists | Allowlisted bounded delegation, shared budget/depth limit | No parallel teams or dynamic agent spawning |
| Skills | Scoped AGENTS.md, on-demand SKILL.md selection, pinned resources and permission requirements | No automatic skill installation or model-controlled code execution |
| Artifacts | Private text files with versions, digest checks, download | No general host filesystem, binary artifacts or filesystem snapshots |
| Streaming | Provider text deltas plus durable events, cursor/reconnect and provisional/final separation | Chat Completions, Responses and Messages streaming; no execution from partial tool arguments |
| Reliability | PostgreSQL state, optimistic revisions, command deduplication, worker lock, explicit uncertain outcomes | Does not promise exactly-once remote actions; interrupted calls may require a new turn |
| Observability | Stored event trace, usage and elapsed time | No generic OpenTelemetry export, production dashboard or soak qualification yet |
| Evaluations | Runnable synthetic engine/wire/recovery conformance suite | Does not establish real model quality, safety or parity with provider eval platforms |

The important correction is structural: reports are now one application of the harness, not its default interaction model. The new core can handle a normal question, a contextual investigation, an artifact task or a governed external action. The remaining online-browser, stdio MCP, cross-provider native-context and operations gaps are explicit; this is not a claim of complete OpenAI/Claude parity or full original MVP1 acceptance.

The context implementation follows public [OpenAI reasoning retention](https://developers.openai.com/api/docs/guides/reasoning) and [native compaction](https://developers.openai.com/api/docs/guides/compaction) contracts. It is not a reproduction of private Codex internals. Retaining opaque state, compaction and prompt-cache hits are distinct mechanisms; observed usage/correctness must establish savings on the selected model. See [implementation details](CONTEXT_AND_STREAMING.md).
