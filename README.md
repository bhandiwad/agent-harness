# Pandita generic agent harness

Gemma 4 and Qwen 3.8 request profiles, sample selection and vLLM/gateway ownership are documented in [Model profiles](MODEL_PROFILES.md). Version 0.4.0 separates the generic runtime from Studio lifecycle code, retaining private reasoning history with family-specific replay and thinking on by default; live model qualification still requires your endpoint.


This Linux service runs general agents independently of AgentFabric and independently of the evidence-report workflow. It needs Python 3.12/3.13, PostgreSQL 17, a configured model endpoint and an RS256 identity provider. It does not need LangGraph, Temporal, Celery, Redis or the AgentFabric application. Reports remain an optional application workflow in the original repository, not the required output of this service.

A question starts one run inside a durable conversation. The model can answer immediately, use allowed tools, ask a clarification, delegate a bounded subtask or request approval for an external write. A completed response ends that run, not the conversation. The next question uses retained history. Pending clarification resumes the same run. There is one active run per conversation; send another turn after completion, or cancel first. There is no five-turn limit. Each session is bounded to 2 MiB of stored JSON. Original user requests and clarification answers have a separate 128-message / 32 KiB bound and are retained verbatim when old history is removed from context; exceeding a bound rejects the new turn without silently dropping requirements. Start another conversation with explicit requirements when it fills.

## Install and configure

1. Clone this repository and choose a reviewed commit: `git clone https://github.com/bhandiwad/agent-harness.git`. This repository is the standalone source of truth.
2. Create a virtual environment: `python3.12 -m venv .venv`. Install runtime dependencies with `.venv/bin/pip install --no-deps -r requirements.lock`, then `.venv/bin/pip install --no-deps .`. The lock contains the dependency versions used in the Linux validation environment. See [the review record](docs/REVIEW.md) for current validation and limits.
3. Copy `.env.example` to `.env` and `.worker.env.example` to `.worker.env`. Keep them private and outside source control. Configure the PostgreSQL URL and worker model key. External PostgreSQL requires `sslmode=verify-full`; mount the trusted CA where required by your driver configuration. Loopback is permitted for isolated development only.
4. Replace `config/agent.json` tenant/workspace IDs, identity issuer, model name/endpoint and actual serving capacity. Replace `config/jwks.json` with pinned public RS256 keys. Empty keys intentionally fail closed. Keep configuration at the same absolute path across API/worker/restarts because the configuration pin includes resolved paths.
5. Update the Casbin domain in `config/policy.csv` to `tenant_id/workspace_id`. Add `g, YOUR_IDP_SUBJECT, operator, TENANT/WORKSPACE` and a different reviewer's subject with role `reviewer`. Use the exact JWT subject, not a display name. Required JWT claims: `sub`, `tenant_id`, `iss`, `aud`, `iat`, `exp`; optional `workspace_id` must match. Report-bound JWTs are rejected. The service does not create identity accounts.
6. Provision an empty database and a separate migration owner. With that owner's URL in `AGENT_DATABASE_URL`, run `.venv/bin/pandita-agent migrate --config config/agent.json`. Provision `agent_runtime LOGIN NOSUPERUSER NOBYPASSRLS` with a secret managed outside this package, then apply `deploy/grants.sql` as the administrator. The runtime refuses a superuser, bypass-RLS or table-owner connection. Use a fresh database dedicated to this standalone runtime; existing Studio databases are deliberately rejected. Do not run migrations with runtime credentials.
7. Supply the runtime URL and start the API: `.venv/bin/pandita-agent api --config config/agent.json`. In a second process, supply the runtime URL plus model key and run `.venv/bin/pandita-agent worker --config config/agent.json`. For containers, `docker compose up --build -d` starts those two services after provisioning. Compose binds the API to host loopback port 8093; expose it through your authenticated TLS ingress when deploying. A Docker socket is never mounted.
8. Open `http://127.0.0.1:8093`, enter a short-lived user token, connect and create a conversation. Tokens remain in page memory and must be re-entered after reload. The model key belongs only in the worker. `/healthz` establishes API liveness, not model/worker readiness; `/v1/status` explicitly reports these as unprobed.

Do not put real tokens into commands pasted into chat, configuration examples or repository files. Apply your deployment's database backup, secret rotation, retention and operational policies before production use. This package has no automatic retention deletion, conversation fork, cross-session semantic memory or signed-release promotion UI.

## Modular capabilities and Python SDK

Version 0.3 adds pinned scoped `AGENTS.md`, on-demand skill selection and bounded resource loading. See [capability authoring](CAPABILITIES.md) and the [sync/async Python SDK](SDK.md). The DataFusion kit includes an explicit read-only connector probe. Upgrade into new conversations after completing or reconciling old runs under their original configuration.

## Try a live multi-turn sample

[Maintenance agent walkthrough](examples/maintenance_agent/README.md) uses the Python SDK, a pinned skill and synthetic asset data with a live model. It tests clarification, changing constraints, saved artifacts and a worker restart. The optional `--chat` mode lets you continue asking questions. Version 0.3.1 also lets the agent recover from rejected local file edits by reading the current digest and proposing a new edit; permissions, concurrency checks and run budgets still apply.

## Write an agent

Add an entry to `agents` in `config/agent.json`. Specify a unique `id`, instructions, a configured `model` reference, native tools, external tool names and explicit context/output/run budgets. Add `agent:YOUR_ID` to the user's Casbin role. Optional `output_schema` validates the final JSON result locally. Optional `skills` are `{path,digest}` pins to reviewed SKILL.md files; instructions are rechecked against their digest before each model call. Optional `delegates` list installed specialist IDs; each child also needs an `agent:ID` permission. Delegates share the parent's total budget and session workspace, have a separate task prompt, and are limited to three nested frames.

Start a new conversation after changing configuration; existing sessions refuse to run with a changed pin. Authorization policy and signing keys are refreshed independently. Use `openai_chat`, `anthropic_messages`, or `openai_responses` for the corresponding provider API (base URL ending `/v1`). Responses adds opaque reasoning retention and native automatic compaction; see [context and streaming](CONTEXT_AND_STREAMING.md). These are wire adapters, not claims of support for every provider/model. One tool call is accepted per model response. No automatic paid model call or silent model fallback occurs at installation.

`examples/conversation.py` shows the Python client. `POST /v1/sessions` creates a conversation; `POST /v1/sessions/{id}/turns` submits text. All mutations require a UUID `command_id` and, after creation, `expected_revision`. Reuse the exact command after a lost response. Changed content under an old ID is rejected. Reload on a genuine revision conflict. Poll the session or consume `/stream?after=REVISION` for durable events, including provisional model text deltas when the model profile has `stream: true`. The stream rechecks identity/permissions and reconnects after one minute. Full contracts are visible at `/docs`.

## MCP and isolated execution

See [execution setup and boundaries](EXECUTION.md) for pinned MCP Streamable HTTP tools and the separate Linux shell/Python/browser broker. Capabilities require explicit agent configuration, permissions and delegated credentials; none are enabled by default. The browser profile is offline.

For optional Python/JavaScript projects, see [coded agents](CODE_AGENTS.md).

## Connect DataFusion later

DataFusion is your context layer. The harness does not create a substitute index, metadata store, SQL engine or DataFusion business model. Start with `DATAFUSION_TEST.md`, map its example to your actual API, and register only fixed operations. No DataFusion connection is enabled by default. Operator configuration controls endpoints, schemas, credentials and permissions; the model cannot invent a connection or credential.

## Approval and recovery

All configured external write tools require approval from a different subject. Approval binds the exact arguments, call/session/run IDs, configuration and expiry. Permissions are checked again immediately before dispatch. The destination must enforce the operation ID, approved arguments and business preconditions. A timeout or ambiguous receipt moves the run to `outcome_unknown`. The harness checks the same operation's receipt; it never repeats an ambiguous external write. If the destination has no reliable receipts, the action stays unresolved for operator investigation.

Restarts preserve conversation, pending decisions, plans, memory, workspace and trace. An interrupted model/read call is marked interrupted, not silently replayed. Cancellation during an in-flight external write still requires resolving its receipt. This is controlled recovery, not an exactly-once guarantee across arbitrary third-party systems. Native file writes and their results commit in one database transaction; digest checks prevent overwriting a changed file.

## Validation

`tests/` contains generic engine, provider-wire, identity, API, workspace, concurrency and recovery checks. Install the test extra in a disposable environment, set `AGENT_TEST_DATABASE_URL` to an EMPTY throwaway PostgreSQL database with administrative test credentials, and run `python -m pytest -q`. The fixture refuses a database containing application tables and cleans up its own schema/role. Do not use customer/application databases. Synthetic provider/context tests validate contracts; they do not establish model quality, live DataFusion integration or production readiness.

See `NATIVE_TOOLS.md` for the native/extension boundary and `COMPARISON.md` for the documented OpenAI/Claude comparison and remaining gaps.

## Optional completion checks (0.3.2)

Configure `Agent(completion_check="my-check-v1", ...)` and register that exact name in your trusted worker:

```python
from pandita_agent.agent.sdk import CompletionContext, CompletionResult
from pandita_agent.agent.runner import Runner

def check_result(context: CompletionContext) -> CompletionResult:
    if "result.json" not in context.files:
        return CompletionResult(issues=("Save the required result.json artifact.",))
    return CompletionResult()

runner = Runner(store, provider, completion_checks={"my-check-v1": check_result})
```

The API process can use the ordinary runner; execute sessions using your custom worker. The maintenance sample shows a complete worker. A configured check missing from the worker stops the run before a model call. Check exceptions or invalid results also stop it. The callback receives a detached snapshot of the proposed answer, retained user requests, pinned facts and workspace files; it cannot modify session state through that snapshot. Reading files for a check requires the owner's current workspace-read permission. Check results and completion state are saved atomically in `model.completed` events. Rejected answers are drafts, not completed outputs.

Checks run synchronously in the trusted application worker and must be fast, deterministic and free of I/O or side effects. They are not sandboxed model tools. Return at most eight short actionable issues (512 characters each); no issues means the configured checks passed. Version the registration name when behavior changes, deploy the same callbacks to all workers, and start fresh sessions when upgrading configuration. This feature is optional; applications choose what constitutes a correct deliverable.

`max_repairs` (default 3, range 0–8) bounds corrections for rejected tool arguments, safe native input errors and rejected final answers, shared across delegated work in one run. Corrections still consume the existing step, token and time budgets. Argument validation occurs before dispatch; a corrected external write still requires approval for its exact new proposal. Permission failures, unsafe paths, changed skill pins and uncertain external outcomes stop or pause execution rather than entering this correction loop. Existing local edit-conflict recovery remains bounded by the run's step/token/time budgets.

The harness does not semantically compress or rewrite the retained user requests. They remain ordered data for the root agent; applications can also pin structured facts through the existing memory API. This preserves inputs, not a guarantee that every model will interpret them correctly. Large retained requests can exhaust a small model context, which fails explicitly instead of silently deleting constraints.

Application integration and source-access boundaries: [ARCHITECTURE.md](ARCHITECTURE.md).

## Nested Sequential Teams

Opt in with `team_execution: "nested"`. Members may contain teams and select a
configured `model_ref`; absent values inherit the parent model. Calls share the
root's token, time and step limits. Up to three nested levels and 24 total members
are accepted. Each handoff rechecks the child permission and persists its frame.
Existing configurations retain legacy execution and their serialization digest.
Applications may validate or materialize output in `completion_extension`, invoked
only after core schema and configured completion checks pass. The execution loop
and approval state machine remain shared.
