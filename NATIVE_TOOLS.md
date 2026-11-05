# Native tools and responsibility boundaries

The harness should own execution mechanics. DataFusion should own governed context retrieval. Business systems should own domain actions. Tool availability is an explicit agent configuration plus a current permission check; an agent prompt does not grant access.

## Implemented native tools

| Tool | Purpose | Authority and limits |
| --- | --- | --- |
| `harness_ask_user` | Ask a focused question and wait durably | User reply resumes the current run; wait expires |
| `harness_plan` | Save/revise a concise task plan | Task outline only; no hidden reasoning exposed; not a grant to execute its steps |
| `harness_history` | Retrieve archived conversation turns | Same session; at most five turns and 32 KiB per call |
| `harness_delegate` | Assign a bounded task to an installed specialist | Explicit allowlist plus agent permission; shared total budget; depth 3; no arbitrary agent creation |
| `harness_calculate` | Decimal addition/subtraction/multiplication/division | Bounded inputs, precision 28, explicit unit label; no `eval`, code execution or implicit conversion |
| `harness_time` | Read current UTC time | No local timezone or scheduling side effect |
| `harness_list_files` | Inventory the session workspace | Requires `workspace:read`; returns paths, sizes, versions, digests |
| `harness_read_file` | Read numbered text lines | Session-owned virtual file; bounded 200-line slice |
| `harness_search_files` | Literal text search | Session workspace only; at most 100 matches; truncation is explicit |
| `harness_write_file` | Create/update a private text artifact | Requires `workspace:write`; compare expected digest; 32 files, 32 KiB each; not publication |

The first three tools are part of the loop. Delegation is offered only to configured agents. Arithmetic/time are enabled by default; file tools must be configured. Every native operation records a durable event. Native workspace files are database records, never arbitrary host paths. The API also lets an authorized owner upload/download files between runs. PDF, images, OCR and binary artifacts remain deferred.

## Controls owned by the runtime, not model tools

Authentication, permissions, exact-action approval, cancellation, budgets, configuration/skill pins, command idempotency, checkpoints, event delivery and outcome reconciliation are server controls. A model cannot call an `approve_myself`, `grant_permission` or `change_budget` tool. Pinned facts are user-owned through the memory API; the model does not silently write lasting personal memory. Responses retains opaque reasoning state and supports native automatic compaction; other adapters retain bounded transcript reduction. See [context behavior](CONTEXT_AND_STREAMING.md).

## Pluggable capabilities and isolated executor boundary

| Capability | Intended contract | Current implementation |
| --- | --- | --- |
| DataFusion context | Search/fetch governed source material using delegated identity; return source references, versions and permission decisions | Fixed REST adapter and injectable Python adapter, conformance fixture; real API mapping pending |
| Business actions | Typed domain operation with preconditions, approval digest, operation ID and receipt lookup | Generic REST invocation/receipt protocol implemented; destination unconfigured |
| Web search/fetch | Operator-owned search service, citation-bearing result, allowed destinations, explicit egress policy | Can be a configured REST tool; no bundled online crawler; the isolated browser profile is offline |
| Shell/Python | Ephemeral per-run sandbox, non-root, CPU/RAM/time/output bounds, bounded session-file transfer without host mounts, egress deny by default, no host secrets or Docker socket | Implemented through a separate authenticated OCI broker, no host mounts, network disabled, pinned images, gVisor default and optional required Landlock; see EXECUTION.md |
| Browser interaction | Isolated browser session, destination restrictions, controlled downloads, write/submit approval | Implemented offline local HTML/JavaScript and bounded Playwright actions; live website access and binary downloads remain unavailable |
| MCP | Reviewed tool discovery, pinned schemas, server identity, scoped auth, cancellation and reconnect semantics | Pinned Streamable HTTP client with schema checks, delegated auth, JSON/SSE, write approval and receipt reconciliation; no stdio or server sampling |
| Scheduling/background jobs | Explicit schedule ownership, cancellation, recurrence limits and durable queue | Worker resumes queued runs; no recurring scheduler or proactive memory agent |
| Cross-session knowledge | User/tenant governed memory with source provenance, consent and retention | DataFusion extension boundary; only per-session pinned facts/history exist now |

Prioritize a sandbox executor and MCP transport when the first task actually needs code, browser interaction or existing MCP servers. They need dedicated acceptance tests, not a shell wrapper labelled as a sandbox. The generic conversation/tool/approval core is usable without them. A business-domain toolkit should remain a separately versioned configuration, not hard-coded into the harness.

## Threat and information boundaries

Tool results, files and retrieved context are untrusted data. Instructions tell the model this, while permissions, fixed endpoints, schemas and approvals are enforced outside the model. Prompt-injection resistance is not established merely by those instructions. A permitted model can still produce a poor answer, and user-visible citations are not a universal verifier. Evidence-report verification remains an optional stronger workflow.

Saved transcripts retain source snapshots. Revoking a source's permission does not retrospectively redact copies already saved in a session. A separate retention/redaction policy is needed if that behavior is required. A reviewer with session review permission can inspect workspace context as part of the full transcript; file download additionally requires `workspace:read`. The service has workspace-scoped reviewer access, not a per-action assigned-reviewer queue. These limits must be included when qualifying a customer deployment.

Version 0.3 adds `harness_select_skills` and `harness_read_skill_resource` for permitted reviewed packages. See `CAPABILITIES.md`. Neither tool executes scripts or changes permissions.
