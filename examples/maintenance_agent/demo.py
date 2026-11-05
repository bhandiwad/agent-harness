"""Live multi-turn sample. Uses the public SDK and a disposable local service installation."""

import argparse
import json
import math
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import jwt
from agent import make_agent
from cryptography.hazmat.primitives.asymmetric import rsa
from pandita_agent.agent.sdk import (
    Client,
    Model,
    Settings,
    StreamClosed,
    TransportError,
    write_config,
)
from pandita_agent.agent.storage import migrate
from psycopg import sql
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent
PROMPTS = [
    "Plan inspections for all pumps in assets.csv within a two-hour maintenance window. Use the maintenance-planning skill and ask me for missing crew information before planning.",
    "Two technicians are available. Use the standard inspection duration and setup from the skill. Tell me whether all pumps fit. Save BOTH plan.md and schedule.json before finishing.",
    "Exclude P07 and P08. Keep all other constraints unchanged. Recalculate and update BOTH plan.md and schedule.json before finishing.",
    "Inspection time has changed to 45 minutes per pump. Keep the same crew, exclusions and two-hour window. Recalculate and update BOTH plan.md and schedule.json before finishing. Do not add people or drop pumps.",
    "Extend the window to three hours. Keep the rest unchanged. Save the final plan.md and schedule.json, and summarize the result.",
]


def load_model(env):
    selected = env.get("DEMO_MODEL_CONFIG")
    if selected:
        from pandita_agent.files import read_bounded

        model = Model.model_validate_json(read_bounded(Path(selected), 262144))
        if model.api_key_env != "MODEL_API_KEY":
            raise ValueError("The sample passes only MODEL_API_KEY to its worker.")
    else:
        model = Model(
            model_ref="demo",
            model_name=env.get("DEMO_MODEL", "gpt-4.1-mini"),
            base_url=env.get("DEMO_MODEL_URL", "https://api.openai.com/v1"),
            api_key_env="MODEL_API_KEY",
            stream=True,
        )
    return model


def provision(folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="local-sample", alg="RS256", use="sig")
    (folder / "jwks.json").write_text(json.dumps({"keys": [public]}))
    tenant, workspace = uuid4(), uuid4()
    permissions = [
        "session:create",
        "session:read",
        "session:cancel",
        "agent:maintenance",
        "workspace:read",
        "workspace:write",
        "capability:maintenance",
    ]
    (folder / "policy.csv").write_text(
        "\n".join(
            [f"p, sample, {tenant}/{workspace}, {p}" for p in permissions]
            + [f"g, sample-user, sample, {tenant}/{workspace}"]
        )
    )
    model = load_model(os.environ)
    agent = make_agent().model_copy(update={"model": model.model_ref})
    if model.execution and model.execution.enable_thinking:
        agent = agent.model_copy(
            update={
                "capacity": agent.capacity.model_copy(update={"output_tokens": 8192}),
            }
        )
    settings = Settings(
        tenant_id=tenant,
        workspace_id=workspace,
        issuer="https://sample.identity.invalid",
        audience="pandita-agent-sample",
        jwks_file=folder / "jwks.json",
        policy_file=folder / "policy.csv",
        agents=(agent,),
        models=(model,),
    )
    config = write_config(settings, folder / "agent.json")
    admin = create_engine(os.environ["DEMO_ADMIN_DATABASE_URL"], hide_parameters=True)
    with admin.connect() as conn:
        if conn.execute(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"
            )
        ).scalar_one():
            raise ValueError("Use an EMPTY disposable database. Existing data will not be changed.")
    migrate(admin)
    password = secrets.token_urlsafe(32)
    with admin.begin() as conn:
        statement = sql.SQL(
            "CREATE ROLE sample_runtime LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS"
        ).format(sql.Literal(password))
        conn.exec_driver_sql(statement.as_string(conn.connection.driver_connection))
        for grant in [
            "GRANT USAGE ON SCHEMA agent TO sample_runtime",
            "GRANT SELECT ON agent.schema_version TO sample_runtime",
            "GRANT SELECT,INSERT,UPDATE ON agent.sessions TO sample_runtime",
            "GRANT SELECT,INSERT ON agent.events,agent.commands TO sample_runtime",
        ]:
            conn.exec_driver_sql(grant)
    runtime_url = admin.url.set(username="sample_runtime", password=password).render_as_string(
        hide_password=False
    )
    admin.dispose()

    def token():
        now = int(time.time())
        return jwt.encode(
            {
                "sub": "sample-user",
                "tenant_id": str(tenant),
                "workspace_id": str(workspace),
                "iss": settings.issuer,
                "aud": settings.audience,
                "iat": now,
                "exp": now + 1800,
            },
            key,
            algorithm="RS256",
            headers={"kid": "local-sample"},
        )

    return config, settings, runtime_url, token


class Services:
    def __init__(self, config, database_url):
        self.config = config
        self.model_key = os.environ["MODEL_API_KEY"]
        self.env = {
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(Path(sys.modules[Settings.__module__].__file__).resolve().parents[2]),
            "AGENT_DATABASE_URL": database_url,
        }
        self.children = []
        self.worker = None

    def start(self, kind):
        env = dict(self.env)
        if kind == "worker":
            env["MODEL_API_KEY"] = self.model_key
        command = (
            [sys.executable, str(ROOT / "worker.py"), str(self.config)]
            if kind == "worker"
            else [
                sys.executable,
                "-m",
                "pandita_agent.agent.main",
                "api",
                "--config",
                str(self.config),
                "--port",
                "8093",
            ]
        )
        process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.children.append(process)
        if kind == "worker":
            self.worker = process
        return process

    @staticmethod
    def stop(process):
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def restart_worker(self):
        self.stop(self.worker)
        self.start("worker")

    def close(self):
        for process in reversed(self.children):
            self.stop(process)
        self.children = []


def all_events(sdk, identifier):
    result = []
    cursor = 0
    while batch := sdk.events(identifier, after=cursor):
        result.extend(batch)
        cursor = batch[-1].revision
    return result


def turn(sdk, row, prompt):
    print("\nUSER: " + prompt, flush=True)
    queued = sdk.submit(sdk.prepare_turn(row, prompt))
    cursor = queued.revision
    try:
        for event in sdk.stream(row.id, after=cursor, timeout=260, reconnects=6):
            cursor = event.revision
            if event.kind == "skills.selected":
                print("[skill selected]", flush=True)
            elif event.kind == "skill.resource_read":
                print("[skill reference loaded]", flush=True)
            elif event.kind == "native.rejected":
                print(
                    "[edit rejected safely; agent must read the current digest before retrying]",
                    flush=True,
                )
            elif event.kind == "native.completed":
                print("[tool: " + event.data["tool"] + "]", flush=True)
            elif event.kind == "model.delta":
                print(event.data.get("delta", ""), end="", flush=True)
    except StreamClosed:
        # State remains authoritative even if an event connection exhausts its retries.
        sdk.wait(row.id, timeout=30)
    row = sdk.session(row.id)
    print("\nSTATUS: " + row.status, flush=True)
    if row.status == "waiting_input":
        print("QUESTION: " + row.state["run"]["pending"]["arguments"]["question"], flush=True)
    elif row.status != "completed":
        raise RuntimeError(
            "Run stopped in " + row.status + "; inspect trace.json, no success was assumed."
        )
    return row


def verify_schedule(sdk, row, *, count, inspection, window):
    data = json.loads(sdk.download(row.id, "schedule.json"))
    expected = {
        "included_pumps": [f"P{i:02d}" for i in range(1, count + 1)],
        "excluded_pumps": [f"P{i:02d}" for i in range(count + 1, 9)],
        "technicians": 2,
        "inspection_minutes_per_pump": inspection,
        "setup_minutes": 30,
        "window_minutes": window,
        "total_inspection_person_minutes": count * inspection,
        "required_elapsed_minutes": 30 + math.ceil(count / 2) * inspection,
        "fits_window": 30 + math.ceil(count / 2) * inspection <= window,
    }
    for name, value in expected.items():
        actual = sorted(data[name]) if isinstance(value, list) else data[name]
        if actual != value:
            raise AssertionError(f"Saved schedule failed the independent check for {name}.")
    if not isinstance(data.get("source"), str) or "assets.csv" not in data["source"]:
        raise AssertionError("Saved schedule must retain its source.")
    return data


def execute(args, output, folder):
    config, settings, url, token = provision(folder)
    (output / "agent.json").write_text(settings.model_dump_json(indent=2))
    services = Services(config, url)
    transcript = []
    row = None
    sdk = None
    summary = {
        "status": "failed",
        "live_model": settings.models[0].model_name,
        "synthetic_assets": True,
        "datafusion_used": False,
    }
    try:
        api = services.start("api")
        services.start("worker")
        sdk = Client("http://127.0.0.1:8093", token, timeout=10)
        deadline = time.monotonic() + 20
        while True:
            try:
                sdk.agents()
                break
            except TransportError:
                if api.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        print("Live model: " + settings.models[0].model_name, flush=True)
        row = sdk.create_session("maintenance", "SDK multi-turn maintenance sample")
        row = sdk.submit(
            sdk.prepare_upload(row, "assets.csv", (ROOT / "data" / "assets.csv").read_text())
        )
        if args.ui:
            import uvicorn
            from model_setup import ModelSetup
            from ui_server import create_app

            print(
                "Sample UI ready on port 8094. No model call occurs until you send a message.",
                flush=True,
            )
            uvicorn.run(
                create_app(
                    token,
                    row.id,
                    settings.models[0].model_name,
                    port=int(os.environ.get("DEMO_UI_PORT", "8094")),
                    setup=ModelSetup(settings, services, url, token, output),
                ),
                host="0.0.0.0",
                port=8094,
                access_log=False,
                log_level="warning",
            )
            summary.update(status="interactive_ui_stopped", session_id=str(row.id))
            return
        for index, prompt in enumerate(PROMPTS):
            if index == 3:
                before = row.revision
                services.restart_worker()
                sdk.close()
                sdk = Client("http://127.0.0.1:8093", token, timeout=10)
                row = sdk.session(row.id)
                assert row.revision == before
                assert row.state["run"]["frames"][0]["active_skills"] == ["maintenance-planning"]
                print(
                    "\n[worker restarted; SDK reconnected to the same session and selected skill]",
                    flush=True,
                )
            row = turn(sdk, row, prompt)
            entry = {
                "turn": index + 1,
                "prompt": prompt,
                "status": row.status,
                "revision": row.revision,
                "answer": row.answer,
            }
            if index == 0:
                assert row.status == "waiting_input", (
                    "Expected a clarification request for crew size."
                )
                entry["question"] = row.state["run"]["pending"]["arguments"]["question"]
            else:
                entry["schedule"] = verify_schedule(
                    sdk,
                    row,
                    count=8 if index == 1 else 6,
                    inspection=30 if index < 3 else 45,
                    window=180 if index == 4 else 120,
                )
                (output / f"turn-{index + 1}-plan.md").write_bytes(sdk.download(row.id, "plan.md"))
            transcript.append(entry)
            (output / "transcript.json").write_text(json.dumps(transcript, indent=2))
        trace = all_events(sdk, row.id)
        assert any(e.kind == "skills.selected" for e in trace)
        assert any(e.kind == "skill.resource_read" for e in trace)
        assert any(e.kind == "model.delta" for e in trace)
        assert sum(e.kind == "turn.submitted" for e in trace) == 5
        passed_checks = sum(
            e.kind == "model.completed" and e.data.get("completion", {}).get("status") == "passed"
            for e in trace
        )
        assert passed_checks == 4
        assert row.state["requests"] == list(PROMPTS)
        assert row.state["files"]["schedule.json"]["version"] >= 4
        assert row.state["files"]["plan.md"]["version"] >= 4
        for name in ("schedule.json", "plan.md"):
            (output / name).write_bytes(sdk.download(row.id, name))
        summary.update(
            status="passed",
            session_id=str(row.id),
            user_turns=5,
            worker_restart_verified=True,
            completion_checks_passed=passed_checks,
            tool_input_rejections=sum(e.kind == "tool.rejected" for e in trace),
            completion_rejections=sum(
                e.kind == "model.completed"
                and e.data.get("completion", {}).get("status") == "rejected"
                for e in trace
            ),
            artifact_versions={
                name: row.state["files"][name]["version"] for name in ("schedule.json", "plan.md")
            },
            input_tokens=sum(
                e.data.get("input_tokens", 0) for e in trace if e.kind == "model.completed"
            ),
            output_tokens=sum(
                e.data.get("output_tokens", 0) for e in trace if e.kind == "model.completed"
            ),
            checks=[
                "clarification pause and resume",
                "on-demand skill and reference",
                "persisted user exclusions",
                "updated timing calculation",
                "digest-checked artifact replacements",
                "worker restart and SDK reconnect",
                "live streamed text",
                "independently checked saved schedules",
            ],
        )
        print(
            "\nPASS: five user turns, live model, persisted skills, restart and validated artifacts.",
            flush=True,
        )
        if args.chat:
            print(
                "Continue this conversation. Type /quit to stop. Each turn uses the live model.",
                flush=True,
            )
            while True:
                try:
                    prompt = input("\nYou: ").strip()
                except EOFError:
                    break
                if prompt == "/quit":
                    break
                if prompt:
                    row = turn(sdk, row, prompt)
    finally:
        if sdk and row:
            try:
                row = sdk.session(row.id)
                (output / "trace.json").write_text(
                    json.dumps(
                        [e.model_dump(mode="json") for e in all_events(sdk, row.id)], indent=2
                    )
                )
                (output / "session.json").write_text(row.model_dump_json(indent=2))
            except Exception:
                pass
        (output / "validation.json").write_text(json.dumps(summary, indent=2))
        if sdk:
            sdk.close()
        services.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", action="store_true")
    parser.add_argument("--ui", action="store_true")
    args = parser.parse_args()
    output = Path(os.environ.get("DEMO_OUTPUT", "/outputs"))
    with tempfile.TemporaryDirectory(prefix="maintenance-agent-") as directory:
        execute(args, output, Path(directory))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"Sample failed ({type(exc).__name__}). Inspect validation.json, session.json and trace.json; no success was assumed.",
            flush=True,
        )
        raise SystemExit(1) from None
