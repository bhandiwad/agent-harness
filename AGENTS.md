# Standalone harness working agreements

- This repository owns the generic runtime, API, SDK, tools and sandbox contracts.
- AgentFabric/AgentStudio are clients. Never import application code, require their database tables, or hard-code their callback routes here. Use registered adapters and injected authorization where needed.
- Preserve tenant/workspace isolation, exact approval binding, idempotency, and unknown external outcomes. Never automatically replay an ambiguous external write.
- Keep credentials out of source, logs, model context and browser storage. Subprocess environments must be explicit.
- Test against an EMPTY disposable PostgreSQL database. Never migrate or test against application/customer databases.
- Run `python -m pytest -q`; real OCI tests additionally require AGENT_SANDBOX_TEST_CONFIG. Record skipped integration tests honestly.
- Keep model, tool, SDK and process-level behavior covered. Prefer simple fixes with regressions over unrelated rewrites.
