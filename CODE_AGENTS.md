# Optional coded agents

The core can execute reviewed Python or JavaScript projects through a separate code broker. It does not need Studio. Configure an agent's `code` field with a `CodeProject` (language, entrypoint and bounded source files), keeping the same model, tool allowlist, permissions, output schema and completion-check settings as other agents.

Python entrypoint:

```python
def run(input, ctx):
    answer = ctx.generate(input)
    ctx.write_file("answer.md", answer)
    return answer
```

Enable `harness_write_file` and the owner's workspace-write permission to save that output. `ctx.tool(name, arguments)` dispatches only a configured tool; external writes still pause for independent approval. `ctx.generate(prompt)` uses the configured model and shared run budget. Each isolated execution replays the source with previously resolved effects. Source must be deterministic up to those calls; changed replay requests fail. No network, provider key or application credential reaches the project container.

The completed answer passes the same output schema and trusted completion callback as model-generated answers. Rejected deterministic code fails the run; it cannot rewrite its own source. Proposed files are applied only after those checks pass. A coded delegate returns its result to the parent.

## Operator setup

Build the project executor using `docker build -f sandbox/Dockerfile.project -t pandita-project:local sandbox`, then pin its immutable image ID as the `shell` image in a dedicated broker configuration. Follow [the isolation configuration](EXECUTION.md); do not mount the Docker socket into application/API/worker containers.

Run the broker on the operator host:

```sh
python -m pandita_agent.agent.code_broker \
  --config /private/config/code-broker.json \
  --token-file /private/secrets/code-transport \
  --cert /private/tls/broker.crt --key /private/tls/broker.key
```

Supply a strong private transport key (at least 32 characters) and verified TLS. Configure `AGENT_CODE_URL`, `AGENT_CODE_TOKEN` and, for a private CA, `AGENT_CODE_CA` in the worker only. The broker validates its configured tenant/workspace scope and limits concurrent execution.

Package images are optional. The operator-authenticated broker exposes `/v1/environments` to build exact registry package versions from fixed recipes. Pin the returned environment identifier in `CodeProject.environment`. Applications should provide their own authorized operator UI; there is no Studio authoring endpoint in the core.
