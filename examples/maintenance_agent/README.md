# Run a live multi-turn agent using the SDK and a skill

This sample uses the independent harness 0.4.0 runtime, a real model endpoint, a disposable PostgreSQL database and synthetic pump data. It makes no DataFusion calls or external business changes. The sample creates its own short-lived local test identity; it does not create an application account or reuse production database permissions.

## Chat in the browser

```sh
python3 run.py --model-env /private/path/model.env --image pandita-agent:0.4.0 --ui
```

Open **http://localhost:8094** when the launcher says the UI is ready. The sample starts with the asset CSV loaded and makes no model calls until you send a message. Chat on the left; inspect selected skills, expandable tool arguments/results, generated files and their versions on the right. Preview or download outputs, answer clarification questions, stop a running task, or start a new conversation. Reloading preserves the selected conversation in the current browser tab. A failed run stays visible; inspect its last tool call and send a corrective follow-up. A model can still choose an invalid tool argument: the UI displays the actual harness result, not a scripted success.

This browser mode uses a local-only sample gateway and an ephemeral test identity. The gateway retains the JWT on the server and forwards requests to the existing authenticated harness; the model key remains in the worker. Host/origin checks, a custom client header and restrictive content policy protect the convenience endpoint from cross-origin browser access. Only loopback port 8094 is published. This is a local development convenience, not a production authentication design: do not expose it through a public tunnel or ingress.

Keep the launcher running while you chat. Ctrl+C stops this isolated sample and removes its disposable database. The harness conversation persists across page reloads while the sample is running. Existing CLI `--chat` and five-turn demo modes remain available; choose either `--ui` or `--chat`.

## Scripted five-turn demonstration

1. Ask for an inspection plan. The agent asks for missing crew size and activates `maintenance-planning` before calculating; the order of clarification and skill selection can vary.
2. Answer “two technicians.” It calculates that eight pumps need 150 elapsed minutes and do not fit the 120-minute window.
3. Exclude P07/P08. Six pumps need 120 minutes and fit. Existing plan artifacts are updated with digest checks.
4. The launcher restarts the worker and reconnects the SDK to the same session. Change inspection time to 45 minutes: six pumps now need 165 minutes, exceeding the original window.
5. Extend the window to three hours. The agent saves a feasible final plan, preserving crew size, durations and exclusions.

The harness does not receive these expected totals in the user prompts. The sample independently checks the saved JSON against the rules after each planning turn. Wording can vary. A failed assertion is reported as a failed run, not hidden or retried into a success.

## Prerequisites

- Docker with Compose, Python 3 on the launcher machine and network access to your model provider.
- A built standalone harness 0.4.0 image. In this repository, run `docker build -t pandita-agent:0.4.0 .`.
- A private environment file containing only your chosen model settings:

```dotenv
MODEL_API_KEY=your-key
DEMO_MODEL=gpt-4.1-mini
DEMO_MODEL_URL=https://api.openai.com/v1
```

Protect it with file permissions such as `chmod 600 /private/path/model.env`. Keep it outside this sample and source control. The sample uses the harness's OpenAI-compatible Chat protocol; use a compatible tool-calling/streaming model. Only the worker subprocess receives the model credential. API and database credentials are separate. Calls consume model tokens: each run has bounded steps, time and tokens; this is not a strict currency spending cap.

## Run

From this folder:

```sh
python3 run.py --model-env /private/path/model.env --image pandita-agent:0.4.0
```

The launcher prints the output folder. It creates an isolated Compose project, runs the five-turn task, then removes only that project's containers, network and disposable database. Output files remain. No existing application services or databases are changed. The scripted and terminal modes have no published host ports. Docker's socket is never mounted in the sample container.

To continue asking questions after the scripted demonstration:

```sh
python3 run.py --model-env /private/path/model.env --image pandita-agent:0.4.0 --chat
```

Type `/quit` to finish. You can ask, for example: “What if I only have one technician?” or “Include P07 again but keep the three-hour window.” Extra chat turns use the same session and live model but are not part of the five-turn acceptance checks. After the demo exits, its disposable database is removed; transcripts and downloaded artifacts remain, while resuming that session requires a persistent deployment.

For an explicit empty output folder, add `--output /path/to/new-output`. For a Docker binary outside `PATH`, add `--docker /path/to/docker`. The default image is `pandita-agent:0.4.0`; build it from this package before launching.

## Files to inspect and modify

- `agent.py`: the small SDK agent definition, including its tool permissions, skill and project instruction pins. Start here to write your own agent.
- `ui_server.py` and `ui/`: local browser gateway and chat interface.
- `test_ui.py`: focused local gateway security/proxy checks.
- `demo.py`: configures the model, saves the agent configuration, then uses `Client` for every conversation operation. Its provisioning section is local test infrastructure, not a production identity setup.
- `project/AGENTS.md`: project guidance.
- `skills/maintenance-planning/SKILL.md`: reusable planning behavior, required artifacts and update rules.
- `skills/maintenance-planning/references/planning-rules.md`: details loaded only when needed.
- `data/assets.csv`: uploaded synthetic input.
- `PROMPTS` inside `demo.py`: the five user messages.

The output folder contains `validation.json`, `transcript.json`, `trace.json`, `session.json`, `agent.json`, per-turn Markdown plans and final `plan.md` / `schedule.json`. The configuration uses temporary container paths and is an inspection artifact, not a directly deployable production config. No JWT, private signing key, model key or database password is exported. `validation.json` lists observed token usage and whether every acceptance check passed. `trace.json` shows skill activation, reference reads, calculations, file writes and streamed text events.

To test your own agent, change its SDK definition, skill and prompts, and update the independent assertions for your task. Re-run with a new empty output folder. Editing the skill before a new launch creates a new pin; modifying it during a running conversation is intentionally rejected. The actual runtime remains unmodified by this sample.

The sample requires the 0.3.4 local-edit recovery behavior for reliable revision attempts. A stale write produces a visible `native.rejected` event, leaves the file unchanged, and lets the agent read and correct its proposal within the same budget. A missing artifact or wrong calculation still fails the sample; the runner does not silently repair the model output.

## Completion checks and correction

The sample's `worker.py` explicitly registers `maintenance-v1` from `checks.py`. It validates required files, pump coverage, disjoint exclusions, typed quantities, whole inspection rounds, person-minutes and feasibility before a final answer can complete. A rejected draft and its actionable feedback remain visible in chat; check results appear in activity. The model may correct its artifacts within the same run budget. The scripted demo independently checks the exact expected user constraints after each turn.

The checker validates structured artifacts and that the Markdown plan is present and nonempty. It does not prove every prose statement or infer all natural-language requirements. Checks are trusted, small application functions; do not put network calls or side effects in them. General agents can omit a completion checker. This planning sample expects planning artifacts before completion, so it is not configured as an unrestricted small-talk agent.

The runtime retains original user requests and clarification answers separately from tool history. They remain available after a worker restart or removal of old history from the model context. New conversations have a 128-message / 32 KiB retention limit; exceeding it rejects the new turn without discarding existing requirements. A model must still interpret the requests correctly.

Start a new conversation when upgrading to 0.4.0: agent configuration digests change, and the runtime deliberately does not resume an old conversation under a different configuration. Existing 0.3.1 sessions remain usable with their original worker. Use the standalone database; do not reuse a Studio database.

## Gemma and Qwen on vLLM

Add `--model-config /absolute/path/model.json` to select a validated profile. Copy and edit one of the examples in `../../config/model-profiles/`. This overrides `DEMO_MODEL` and `DEMO_MODEL_URL`; credentials still come from the private `MODEL_API_KEY` environment value. See [model profiles and layer responsibilities](../../MODEL_PROFILES.md) for all controls and reasoning-history behavior.

Thinking-enabled Gemma/Qwen profiles use an 8,192-token output allowance in this sample. Reasoning is retained privately; the UI streams answer text and shows tool activity. Start a new session for the changed profile/configuration.


## Choose a model in the chat UI

Click **Model setup**. The header shows the active runtime version and model. Choose a Gemma or Qwen preset, enter the **served model ID** and the **vLLM endpoint** including `/v1`, and supply the endpoint's API key. If your server has no authentication, explicitly select that option. Current credentials can only be reused at the exact same endpoint; the sample never automatically sends an existing provider's key to a new server.

Click **Apply & start new chat**. The local API/worker restart under the new validated profile, then the UI opens a fresh conversation with `assets.csv` loaded. Applying does not call the model or establish endpoint connectivity. Send a message to test inference. Finish or stop all running/waiting conversations before switching. Previous conversations remain readable; those with a different configuration cannot receive new turns. The changed configuration and private in-memory credential apply to this sample instance only.

Gemma and Qwen presets enable thinking by default and show their reasoning retention policy. **Qwen3.8-Flash-Next is an experimental preview.** The sample increases output allowance to 8,192 tokens for thinking-enabled profiles; turn/run budgets still apply. Model reasoning is private, while answer text, tools, skills and outputs remain visible.

### Add a custom profile

For a custom Gemma/Qwen deployment, select the matching preset, give it a unique **Profile name**, and enter your deployment's served model ID and endpoint. This preserves the right reasoning policy. For another compatible family, choose **Custom compatible model**; it has no assumed Gemma/Qwen reasoning behavior.

Open **Custom profile / advanced JSON** to edit or import a full Model object. Click **Load JSON into form**, review its values, then apply. The server validates fields and rejects unknown families/parameters. For a generic compatible model, omit `execution`. Profiles currently use the Chat Completions protocol in this sample. Use **Download profile JSON** to keep a validated, portable copy; it never includes the separate API-key field. The latest applied non-secret profile is also saved as `active-model.json` in the launcher's output folder. Re-enter credentials after restarting; the sample does not write them to the profile or browser storage.

Example for a separately served Qwen checkpoint or alias:

```json
{
  "model_ref": "my-qwen",
  "model_name": "my-served-qwen",
  "base_url": "https://inference.example.com/v1",
  "api_key_env": "MODEL_API_KEY",
  "protocol": "openai_chat",
  "stream": true,
  "request_timeout_seconds": 120,
  "execution": {
    "id": "my-qwen-thinking-v1",
    "family": "qwen3_8",
    "enable_thinking": true,
    "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0}
  }
}
```

Use the model's actual family; renaming another architecture does not make it Gemma/Qwen compatible. These settings do not install weights or configure vLLM tool/reasoning parsers; see [serving setup](../../MODEL_PROFILES.md).

### Endpoint networking and multiple samples

Remote model URLs must use HTTPS with a trusted certificate. A plain `http://localhost:8000/v1` URL works only when inference is on the same network namespace as the sample process. In this Docker launcher, that is the sample/database container network, not the host laptop. For host or remote inference, expose a reachable HTTPS endpoint. Do not embed keys, credentials, fragments or query strings in the endpoint URL.

To keep an existing chat running while opening a new sample on another port:

```sh
python3 run.py --model-env /private/path/model.env --image pandita-agent:0.4.0 --ui --ui-port 8095
```

The UI is loopback-only. Treat it as local operator access; do not expose it publicly. Profile changes are serialized with chat mutations and rejected for stale setup revisions. Retries reuse the same command while the sample remains running. Startup failure restores the previous setup. No automatic cross-model fallback occurs.
