# Security boundaries

The harness enforces authenticated tenant/workspace scope, local policy, exact independent approval for external writes, bounded execution, revision checks and durable unknown outcomes. Source, tool output, skills and model text cannot grant capabilities.

## Trust model

- Configuration, tool schemas, local policy files, signing keys and trusted completion callbacks are operator-controlled. Keep them read-only to application users and model processes.
- The worker receives model and delegated tool credentials. The API and browser must not receive model credentials. JWTs are short-lived; private signing keys stay with your identity provider.
- The database runtime role must be a non-owner without superuser or BYPASSRLS. Use verified TLS for remote databases/endpoints. Reviewers with `session:approve` and `session:read` can inspect other sessions in their configured workspace; assign that role deliberately.
- `workspace:read` controls direct file retrieval and file-content fields in session snapshots. Conversation messages may contain text previously read or quoted by the agent; it is not retroactive content classification/redaction.
- Tool endpoints and credential mappings are fixed by the operator. Redirects, ambient proxy credentials and automatic write retries are disabled. A destination must implement its own business authorization, idempotency and durable receipts.
- The sample UI is a local operator demo with host/origin checks, not a multi-user identity service. Deploy the real API behind your TLS ingress and identity provider.

## Isolation

Shell/Python/browser execution runs through a separate operator-owned broker and digest-pinned OCI images. Jobs have no network or host mounts, use a non-root UID, read-only root, dropped capabilities, no-new-privileges and bounded CPU/memory/process/time/output. Landlock fails closed when required. Browser execution is offline.

The default broker requires gVisor (`runsc`). `runc` requires explicit lower-assurance opt-in. Linux host/kernel, container daemon, gVisor, browser and image patching remain deployment responsibilities. These are not guarantees against kernel exploits. Run the broker separately; never mount its Docker socket into the API/worker/application container.

Optional package builds contact public package registries using fixed recipes. Python accepts wheels only; JavaScript install scripts are disabled. Package builds belong to a dedicated operator builder with no application credentials. Exact versions are not a substitute for dependency review, registry controls and locked artifact provenance.

## Operational responsibilities

The deployment owns ingress rate limits and connection limits, network egress policy, secrets, database backups/encryption, log access, retention and monitoring. The runtime bounds individual sessions and tool/model work; it does not implement a cluster-wide user quota or billing ceiling. Synchronous trusted completion callbacks must be bounded and free of I/O.

Report suspected vulnerabilities privately to the repository owner. Do not include real secrets, customer data or working production exploit credentials in public issues.

See [the review record](docs/REVIEW.md) for tested cases, fixes and remaining qualification limits. An automated dependency audit is only a check against known advisories at that time.
