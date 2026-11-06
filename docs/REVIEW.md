# Standalone harness review — 5 November 2026

## Scope and boundary

Reviewed the generic runtime execution and recovery paths, API/SDK, JWT/policy/RLS boundaries, context/reasoning handling, native tools and skills, REST/MCP/OpenConnector adapters, isolated execution, sample launcher/UI, packaging and deployment defaults. Review used current source and executable regressions. This is an engineering review, not a penetration-test certification or proof of model quality.

Version 0.4.0 is a standalone source repository. Studio authoring/trials/releases/jobs/schedules/data stores, fixed Slack/business-action callbacks and their mandatory database migrations were removed from this distribution. Generic approvals, reconciliation, delegation, sequential teams, coded agents, SDK authoring and pluggable context/tools remain. The original application checkout and live deployments were not changed.

## Findings and fixes

| Severity | Finding | Fix and evidence |
|---|---|---|
| High | Session snapshots exposed raw file contents even when direct downloads denied workspace-read permission. | Apply the same permission to file content in snapshots; regression tests deny both paths. Conversation transcripts remain session-readable and can contain previously quoted text. |
| High | Coded agents bypassed final-answer schemas and completion callbacks. | Share completion validation with the model loop, fail deterministic code on rejection, stage files until validation passes, and reject missing callbacks before dispatch. |
| Medium | A coded delegate completed the entire session instead of returning to its parent. | Share finalization logic with model agents; regression verifies the parent consumes the child result and produces the final answer. |
| Medium | Unexpired clarification/approval waits filled the oldest 100-row work page and starved queued sessions. | Poll only runnable or expired waiting sessions. Reproduced with 120 old waiters and a newer queued run. |
| Medium | One unhandled session task failure stopped the whole worker and cancelled unrelated tasks. | Isolate task exceptions, retain durable work, back off failed processing and recover polling after storage failure without logging sensitive exception text. |
| Medium | SSE framing rejected a valid coalesced network chunk as one oversized line; multiline size checks repeatedly scanned all prior lines. | Frame before applying line limits and count event size incrementally. Preserve total/event/count bounds and strict UTF-8 handling. |
| Medium | SDK and code-service response bounds were checked after eager buffering. | Stream and stop at the byte limit; sync/async SDK tests verify unread excess chunks. Code-service endpoints also require the normal verified-HTTPS/loopback policy. |
| Medium | Code execution could outlive the remaining run deadline. | Wrap code dispatch in the remaining run timeout; regression cancels a slow executor and records failure. |
| Medium | Generated team IDs could shadow installed agents; team members could name unconfigured tools. | Validate the expanded catalogue before installation. |
| Medium | Package-build endpoint read an unbounded request body before checking its 8 KiB limit; code broker logged full exception traces. | Incrementally reject oversized input and emit a generic failure log. Tests verify early read termination and absence of exception contents in logs. |
| Medium | Sample subprocesses could load a different installed harness than their parent process. | Pass the resolved trusted runtime path explicitly; real API/worker restart and idempotent model-profile retry are tested. |
| Low | Standalone export included stale image tags, old schema grants and a test path pointing inside the monorepo. | Use version 0.4.0, core-only grants, standalone paths and an automated dependency/API/schema boundary test. |

## Performance evidence

A synthetic 4,001-element multiline JSON SSE event, measured for 15 iterations on the same Linux ARM64 test environment, reduced median parser time from **499.770 ms to 4.232 ms** (about 118× for this deliberately adverse framing case). This is a parser microbenchmark, not overall agent latency or an inference speed claim. The queue regression verifies progress behind 120 waiting sessions; it does not establish a production throughput SLA.

## Validation

- Clean standalone Python wheel and non-root Docker image built successfully using the repository Dockerfile and pinned dependency list.
- Installed-package run: **200 passed, 6 skipped, 1 deselected**. The deselected final broker log-redaction regression was validated against the updated source separately; both broker boundary tests passed. The six skipped tests require a host-side OCI qualification configuration.
- Real Linux ARM64 container checks: **6 passed** — non-root UID/dropped capabilities/no-new-privileges, network blocking, read-only root and Landlock; shell input/output; timeout; output limit; symlink rejection; and offline browser DOM interaction. These use explicit runc opt-in with required Landlock, not gVisor. The browser used the existing 30-second default. A first attempt during concurrent image builds hit startup/test deadlines; the completed checks followed image export.
- Static correctness checks (`ruff check --select F,E9`) passed across source, tests, examples and sandbox code.
- `pip-audit` checked the pinned runtime dependencies: **no known vulnerabilities found** on 5 November 2026. No dependency versions were changed merely to clear the audit.
- Credential-pattern scan found no candidates in the publication files; examples use placeholders or synthetic fixture credentials. Private local configuration and generated credentials are excluded from Git.
- The process drills use real API/worker subprocesses, PostgreSQL and synthetic model/context HTTP services. They cover multi-turn continuation, restart, approval and Gemma/Qwen wire-format reasoning retention. Sample model-profile changes and API/worker rollback/restart were also exercised.
- Final exact-source suite: **201 passed, 6 skipped in 55.81 seconds**. All six skips are opt-in host-side OCI tests; the matching real container boundaries were exercised separately as described above.

## Remaining qualification and deployment responsibilities

- Real Gemma/Qwen inference, model quality, native provider capabilities, live DataFusion and customer connector writes require the actual configured endpoints. Synthetic wire fixtures are clearly labelled.
- gVisor and production Linux hosts require their own qualification. Container/Landlock tests on the local Linux VM do not prove resistance to kernel escapes or production load.
- The deployment owns TLS ingress/rate limits, credentials, network policy, backups, retention, monitoring and multi-node capacity planning. Synchronous trusted completion checks must remain fast and free of I/O.
- There is no automatic migration of AgentFabric/Studio installations. They remain on their existing implementation until their client/adapters are separately migrated.
- Dependency scanning covers known Python advisories at scan time, not every container OS/browser vulnerability. Package builders need operator-controlled supply-chain and network policy.

## AgentFabric integration — 6 November 2026

Version 0.4.1 adds application-neutral subclass hooks for tool preparation,
local tool handlers, specialist construction, context and receipt metadata;
shared snapshot sanitization; transactional session metadata and directory
visibility; usage stamping; and failure-isolated worker maintenance. No Studio
imports, tables, routes or callback endpoints were added. Standalone deployment
and its version 1 schema remain unchanged.

The package environment guard now rejects cached images built against a
superseded configured base. Missing code-service configuration raises the
bounded configuration error before reading its endpoint.

Verified in an isolated Python environment against a disposable PostgreSQL 17
container: **204 passed, 6 skipped**, including actual API/worker subprocess
restart, approvals, model wire fixtures and sample-agent checks. The six skipped
checks require the opt-in real OCI sandbox configuration. Focused static checks
passed. These are fixture and local integration results, not live model,
connector, gVisor or production-load qualification.
