# Model execution profiles

Version 0.3.4 includes optional, validated vLLM request settings to `Model` in JSON configuration and the Python SDK. There is no new service or router dependency. Existing models without an `execution` object retain their request behavior, with the existing 120-second request timeout now configurable.

## Ready-to-edit examples

The `config/model-profiles/` folder contains complete Model objects:

| File | Model | Purpose |
| --- | --- | --- |
| `gemma4-31b.json` | `google/gemma-4-31B-it` | Gemma 4 server model |
| `gemma4-12b.json` | `google/gemma-4-12B-it` | Smaller Gemma 4 option |
| `qwen38-27b.json` | `Qwen/Qwen3.8-27B` | Qwen 3.8 dense model |
| `qwen38-flash-next-preview.json` | `Qwen/Qwen3.8-Flash-Next` | Experimental architecture preview; qualify separately |

Checked against official model documentation on 25 October 2026, using vLLM 0.32.0 as the documentation baseline. These are configuration examples, not measured quality recommendations or GPU sizing. Model names are explicit rather than a floating `latest` alias; pin the weights revision and serving image separately in your inference deployment.

The files use a deliberately unusable HTTPS endpoint. Copy one, set `base_url` to your vLLM endpoint including `/v1`, and ensure `model_name` matches its served model ID. Credentials stay in the environment variable named by `api_key_env`; never put a key in the JSON. Plain HTTP is allowed only for loopback addresses under the existing endpoint policy. Remote inference needs HTTPS. A container's loopback is its own network namespace, not the host's.

Run the existing maintenance agent with a selected profile, from `examples/maintenance_agent/`:

```sh
python3 run.py --model-env /private/path/model.env \
  --model-config /absolute/path/my-qwen.json --image pandita-agent:0.4.0 --ui
```

Build that image from this repository first: `docker build -t pandita-agent:0.4.0 .`. The private environment file needs `MODEL_API_KEY`. `--model-config` overrides `DEMO_MODEL` and `DEMO_MODEL_URL`; this sample only forwards the `MODEL_API_KEY` credential variable to its worker. The launcher mounts the profile read-only and uses an isolated disposable database. Omit `--ui` to run the five-turn acceptance scenario and independently check its generated schedule. The sample requires a real model endpoint; the shipped tests use synthetic responses.

The sample also offers **Model setup** in its browser UI: select a preset, configure endpoint/credentials, or import a custom Model JSON, then apply and start a fresh chat. See the [sample guide](examples/maintenance_agent/README.md#choose-a-model-in-the-chat-ui). Credentials stay separate from exported profiles.

For your own SDK agent:

```python
from pathlib import Path
from pandita_agent.agent.sdk import Model

model = Model.model_validate_json(Path("my-qwen.json").read_bytes())
# Put model in Settings.models and use model.model_ref in Agent.model.
# ExecutionProfile and Sampling are also public SDK types.
```

The entire configuration participates in the existing session digest. A profile or timeout change requires a new conversation; finish or reconcile pending actions with the old configuration before upgrading. Do not rewrite old session digests.

## Available request controls

| Configuration | Default / allowed values | Behavior |
| --- | --- | --- |
| `model_name`, `base_url`, `api_key_env` | Explicit operator settings | Select deployment and credential reference |
| `protocol` | `openai_chat` for these profiles | Other protocols reject vLLM execution profiles |
| `stream` | Existing default false; examples true | Incremental answer text and assembled tool calls |
| `request_timeout_seconds` | 120; integer 1–600 | Capped further by remaining agent run time |
| `execution.id` | Required when using a profile | Operator's profile identifier, recorded in model events |
| `execution.backend` | `vllm` | Declares the request contract, not server discovery |
| `execution.family` | `gemma4` or `qwen3_8` | Declares family-specific controls |
| `execution.sampling.temperature` | Optional; 0–2 | Sampling randomness |
| `execution.sampling.top_p` | Optional; greater than 0 through 1 | Probability mass cutoff |
| `execution.sampling.top_k` | Optional; integer at least -1 | Candidate cutoff; -1/0 disable in vLLM |
| `execution.sampling.min_p` | Optional; 0–1 | Relative probability cutoff |
| `execution.sampling.repetition_penalty` | Optional; greater than 0 through 2 | Repetition adjustment |
| `execution.sampling.presence_penalty`, `frequency_penalty` | Optional; -2–2 | Token repetition penalties |
| `execution.sampling.seed` | Optional signed 64-bit integer | Sampling seed; not a cross-hardware determinism guarantee |
| `execution.sampling.stop` | Optional; up to four nonempty strings, 128 characters each | Additional stop strings; avoid truncating tool syntax |
| `execution.enable_thinking` | Boolean; defaults true; explicit false disables generation | Sent as `chat_template_kwargs.enable_thinking` |
| `execution.reasoning_effort` | Optional `low`, `medium`, `xhigh` | Requires Qwen 3.8 and explicitly enabled thinking |

Omitted or null sampling controls are omitted from the request, allowing serving defaults. Zero and false remain explicit values. Unknown fields and non-finite numbers are rejected. The raw HTTP adapter sends vLLM extensions at the request top level, not inside the OpenAI SDK's `extra_body` wrapper. Profiles cannot replace messages, tools, credentials or the output-token limit. Each `model.started` event records the configured model, profile, output limit and effective request timeout, without credentials. This records requested settings, not proof that a remote server honored them.

The Gemma examples use temperature 1.0, top-p 0.95 and top-k 64. Qwen examples use the documented thinking starting settings: temperature 1.0, top-p 0.95, top-k 20, min-p 0, presence penalty 0 and repetition penalty 1. These are editable starting values, not tuned maintenance-agent results. See the official [Gemma model card](https://huggingface.co/google/gemma-4-31B-it) and [Qwen model card](https://huggingface.co/Qwen/Qwen3.8-27B).

## Thinking and context

Thinking is on by default for both execution-profile families. Set `execution.enable_thinking` to false to opt out. Models without an execution profile retain their prior behavior. The sample gives thinking-enabled profiles an 8,192-token output allowance; other profiles keep 2,048. This is a bounded sample choice, not a model maximum or quality guarantee. The allowance covers reasoning plus the final answer, and existing context/run/time budgets still apply. Edit the agent capacity for your workload. Truncation stops the run without accepting partial reasoning or a partial tool call.

The adapter captures the provider's `reasoning_content` or `reasoning` field, including streamed fragments, and saves completed reasoning privately alongside its assistant message. Alias fields must agree if both are supplied. Tool reasoning stays associated with the original provider call ID and matching result across persistence/restarts. Each new request applies the family's policy:

- **Qwen 3.8:** replay reasoning from all assistant messages in retained context, including previous user turns.
- **Gemma 4:** replay tool-call reasoning, including earlier tool-call turns; omit earlier non-tool answer reasoning before a later user message. Current-turn reasoning remains available inside the tool loop.

The request explicitly sets `chat_template_kwargs.preserve_thinking=true`. For Gemma, the harness first filters out earlier non-tool thoughts, so the serving template only preserves permitted thoughts. Use vLLM's matching reasoning parser and compatible template (Gemma's documented tool template accepts this flag). The harness sends structured `reasoning_content`; it does not splice model-specific thought tokens into ordinary answer text. A serving endpoint that returns raw thought tags in `content` rather than parsed reasoning is misconfigured for this contract.

Reasoning is excluded from session API responses, answer deltas, event payloads and the `harness_history` tool. It remains in the tenant-scoped database with existing permissions and storage limits; it is not a user-editable memory fact or tool authorization. Completion checks evaluate the final answer and artifacts, not private reasoning. Reasoning counts toward context selection and provider-reported output usage. Old whole turns may be omitted under the existing context budget; retained thought blocks are not cut midway or silently summarized. Active-context overflow fails explicitly. Each response allows up to 65,536 reasoning characters, and the existing total session storage cap is 2 MiB. These are bounded retention rules, not unlimited memory or provider-native compaction.

The recommendation follows the official [Gemma multi-turn guidance](https://huggingface.co/google/gemma-4-31B-it#3-multi-turn-conversations), [Qwen preserved-thinking guidance](https://huggingface.co/Qwen/Qwen3.8-27B#disable-preserved-thinking) and [vLLM Gemma template](https://github.com/vllm-project/vllm/blob/v0.32.0/examples/tool_chat_template_gemma4.jinja). This does not add image/audio inputs or claim to reproduce private Codex internals. Live endpoint quality and parser qualification remain separate from contract tests.

## Harness, gateway and vLLM responsibilities

| Layer | Configuration it owns |
| --- | --- |
| Harness | Agent instructions/skills/tools, permissions, approvals, completion checks, context selection/compaction policy, context/output safety margins, run steps/tokens/time and tool-repair budget; selects model and per-request sampling |
| Optional external LiteLLM gateway | Deployment aliases, shared provider credentials, routing/load balancing and gateway observability when actually needed |
| vLLM serving | Weights/revision, tokenizer and chat template, tool/reasoning parsers, served model name, maximum model length, tensor/pipeline parallelism, GPU memory allocation, batching/concurrency, KV/prefix cache and quantization |

The harness sends `max_tokens` based on the agent's output limit and remaining token budget. vLLM enforces its own context capacity and generation limits. Align the harness capacity with the deployed `max_model_len`; the sample currently assumes 64,000 tokens. Request controls such as temperature are selected by the harness but executed by vLLM. Server generation defaults apply only where no request override is sent. Prefix caching reuses inference work; it does not shorten conversation context.

Configure vLLM tool parsing explicitly. The official Qwen recipes use `--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3`. Gemma 4's recipe uses `gemma4` parsers and a compatible tool chat template. Verify the recipe against your exact serving version/checkpoint before launch; GPU allocation and parallelism depend on your hardware and are intentionally not prescribed here. This package does not install or manage vLLM.

LiteLLM remains outside the harness. If introduced, point `base_url` at its compatible endpoint and qualify forwarding of vLLM extension fields, streaming, usage and tool calls. Start with one explicit route and no automatic cross-model fallback or compounded retries. No LiteLLM deployment or integration qualification is included in this change.

## Verification boundary and sources

Local regression tests exercise reasoning capture and family-specific replay, streaming/non-streaming responses, incomplete output rejection, private-state filtering, capacity limits, validated settings, bounded timeouts and configuration conflicts. Real API/worker/PostgreSQL process tests cover follow-ups, approvals and restarts using synthetic HTTP model responses that verify received reasoning. They do not measure Gemma/Qwen intelligence, actual vLLM parser compatibility or deployment performance. Run the sample against your deployment before calling a profile qualified.

Primary deployment references: [Gemma family overview](https://ai.google.dev/gemma/docs/core), [Gemma vLLM recipe](https://docs.vllm.ai/projects/recipes/en/stable/Google/Gemma4.html), [Qwen 3.8-27B recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-27B), [Flash-Next preview](https://huggingface.co/Qwen/Qwen3.8-Flash-Next), [Flash-Next serving recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-Flash-Next), [vLLM 0.32.0](https://github.com/vllm-project/vllm/releases/tag/v0.32.0).
