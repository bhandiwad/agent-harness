# Standalone boundary

`pandita-agent` is independently installable and deployable. AgentFabric, AgentStudio and other applications consume its versioned HTTP API or Python SDK. They are not runtime prerequisites.

## Ownership

| Harness | Application | Optional external service |
|---|---|---|
| Sessions, execution loop, budgets, checkpoints, multi-turn history, reasoning retention, context bounds, streaming | Agent catalogue, authoring UI, release audiences, publication and operational dashboards | vLLM/model gateway |
| Tool contracts, permissions, exact approval and reconciliation primitives | Business-specific approval UI and business preconditions | REST/MCP/OpenConnector adapters, DataFusion |
| Scoped skills, project instructions, delegation and sequential teams | Saved source revisions, scheduling and job lifecycle | Isolated shell/Python/browser broker |
| API, SDK, private artifacts and usage events | Rendering, sharing and publishing artifacts | Identity provider and deployment secret manager |

Studio-specific modules and their tables are intentionally absent. Configurations containing removed Studio-only fields are rejected rather than silently ignored. Generic external writes remain available through configured tool adapters with independent approval.

The three runtime tables are `agent.sessions`, `agent.events`, and `agent.commands`, plus a schema-version marker. Migration requires a dedicated owner; API/worker use a different non-owner, non-superuser role with forced RLS. Version 0.4.0 starts a fresh standalone schema lineage at version 1. It refuses Studio schema versions; this is not an in-place Studio migration.

## Integration contract

Applications submit an installed agent ID and authenticated user commands. A configuration digest pins each session. The client cannot supply permissions, endpoint overrides or credentials as task text. Operator configuration installs models, tools and scoped authorization policy. `Runner(authorize=...)` can inject a trusted permission provider without introducing an AgentFabric dependency.

Applications may register an external business action as a REST/MCP tool. The destination owns business constraints and durable receipts. The harness owns exact human approval, dispatch reservation and ambiguous-outcome handling. SDK commands can be retried with the same command ID; external writes cannot be automatically replayed.

DataFusion stays pluggable. `POST /v1/context/access` uses an optional configured read tool to recheck exact source references for the caller. Applications must enforce their own sharing/export policy. No live DataFusion endpoint is bundled or assumed.

## Deployment boundary

API and worker run independently, sharing only their dedicated PostgreSQL database and pinned configuration. Model credentials belong in the worker. Sandbox/code brokers are separately deployed operator services; application containers never receive a Docker socket. The default sandbox requires gVisor and Landlock. Explicit runc opt-in provides lower assurance and needs separate qualification.

The original AgentFabric checkout was not modified by this extraction. It still uses its existing embedded package and Studio runtime. Switching that application to this repository requires a separate adapter migration; no live service has been redeployed here.
