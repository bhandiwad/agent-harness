"""Actual Linux API/worker subprocess drill with explicit synthetic wire fixtures."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest

from pandita_agent.agent.contracts import Settings


def wait_for(check, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (httpx2.HTTPError, KeyError):
            pass
        time.sleep(0.1)
    raise AssertionError("Fixture process did not reach the expected state")


class WireFixture(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, body))
        if self.path == "/context":
            assert self.headers["Authorization"] == "Bearer synthetic-context-user"
            result = {
                "sources": [
                    {
                        "reference": "fixture://assets/A17",
                        "version": "v1",
                        "text": "A17 has 8 pumps.",
                    }
                ]
            }
        elif self.path == "/action":
            assert self.headers["Authorization"] == "Bearer synthetic-context-user"
            result = {
                "operation_id": body["operation_id"],
                "status": "confirmed",
                "result": {"case": "FIXTURE-17"},
            }
        else:
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer synthetic-model"
            messages = body["messages"]
            if getattr(self.server, "family", None):
                prior = [m for m in messages if m["role"] == "assistant"]
                for m in prior:
                    expected = (
                        "private-tool-thought" if m.get("tool_calls") else "private-answer-thought"
                    )
                    if self.server.family == "qwen3_8" or m.get("tool_calls"):
                        assert m.get("reasoning_content") == expected
                    else:
                        assert "reasoning_content" not in m
                for i, m in enumerate(messages):
                    if m.get("tool_calls"):
                        assert messages[i + 1]["tool_call_id"] == m["tool_calls"][0]["id"]
            last_user = max(i for i, message in enumerate(messages) if message["role"] == "user")
            current = messages[last_user:]
            query = current[0]["content"].lower()
            tool = None
            answer = "Synthetic fixture answer, not a live model."
            if "open case" in query:
                tool, answer = "create_case", "Synthetic case FIXTURE-17 confirmed."
            elif "asset a17" in query:
                tool, answer = "context_lookup", "A17 has 8 pumps [fixture://assets/A17]."
            elif "how many" in query:
                assert any("fixture://assets/A17" in m.get("content", "") for m in messages)
                answer = "The previous context said 8 pumps."
            message = {"role": "assistant", "content": answer}
            finish = "stop"
            required_results = (
                2 if tool == "context_lookup" and getattr(self.server, "family", None) else 1
            )
            if tool and sum(m["role"] == "tool" for m in current) < required_results:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "fixture-call-" + str(len(messages)),
                            "type": "function",
                            "function": {
                                "name": tool,
                                "arguments": json.dumps({"query": current[0]["content"]}),
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            if getattr(self.server, "family", None):
                message["reasoning_content"] = (
                    "private-tool-thought"
                    if tool and finish == "tool_calls"
                    else "private-answer-thought"
                )
            result = {
                "choices": [{"finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 30},
            }
        if self.path == "/v1/chat/completions" and body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(value):
                self.wfile.write(
                    (
                        "data: " + (json.dumps(value) if value is not None else "[DONE]") + "\n\n"
                    ).encode()
                )
                self.wfile.flush()

            if message.get("reasoning_content"):
                thought = message["reasoning_content"]
                emit({"choices": [{"index": 0, "delta": {"reasoning_content": thought[:8]}}]})
                emit({"choices": [{"index": 0, "delta": {"reasoning": thought[8:]}}]})
            if message.get("tool_calls"):
                emit(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"tool_calls": [{"index": 0, **message["tool_calls"][0]}]},
                            }
                        ]
                    }
                )
            else:
                emit({"choices": [{"index": 0, "delta": {"content": message["content"][:6]}}]})
                time.sleep(getattr(self.server, "stream_delay", 0))
                emit({"choices": [{"index": 0, "delta": {"content": message["content"][6:]}}]})
            emit({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
            emit({"choices": [], "usage": result["usage"]})
            emit(None)
            return
        raw = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.mark.parametrize("family", [None, "gemma4", "qwen3_8"])
def test_linux_processes_context_followup_approval_and_restart(store, identity, tmp_path, family):
    server = ThreadingHTTPServer(("127.0.0.1", 0), WireFixture)
    server.calls = []
    server.family = family
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    token_file = tmp_path / "context.token"
    token_file.write_text("synthetic-context-user")
    raw = store.settings.model_dump(mode="json")
    raw["models"][0]["base_url"] = endpoint + "/v1"
    if family:
        raw["models"][0].update(
            stream=True,
            execution={
                "id": family + "-fixture",
                "family": family,
                "enable_thinking": True,
                "sampling": {"temperature": 0.7, "top_k": 20},
            },
        )
    for connection in raw["connections"]:
        connection["binding"]["endpoint"] = endpoint
        connection["binding"]["operations"] = {
            connection["tool"]["name"]: "/context"
            if connection["tool"]["effect"] == "read"
            else "/action"
        }
        connection["subject_token_files"] = {"alice": str(token_file)}
    config = tmp_path / "agent.json"
    config.write_text(Settings.model_validate_json(json.dumps(raw)).model_dump_json())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Deliberately minimal environment; never inherit host credentials/proxies.
    env = {
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(sys.modules[Settings.__module__].__file__).resolve().parents[2]),
        "AGENT_DATABASE_URL": store.engine.url.render_as_string(hide_password=False),
        "FIXTURE_KEY": "synthetic-model",
    }
    module = Settings.__module__.rsplit(".", 1)[0] + ".main"
    children = []

    def start(command):
        process = subprocess.Popen(
            [sys.executable, "-m", module, command, "--config", str(config), "--port", str(port)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        children.append(process)
        return process

    def stop(process):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    client = httpx2.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": "Bearer " + identity()},
        timeout=5,
        trust_env=False,
    )

    def get(identifier):
        response = client.get("/v1/sessions/" + identifier)
        response.raise_for_status()
        result = response.json()
        assert "private-tool-thought" not in json.dumps(result)
        assert "private-answer-thought" not in json.dumps(result)
        return result

    def post(path, body):
        response = client.post(path, json=body)
        response.raise_for_status()
        return response.json()

    def turn(row, prompt):
        return post(
            "/v1/sessions/" + row["id"] + "/turns",
            {"command_id": str(uuid4()), "expected_revision": row["revision"], "text": prompt},
        )

    def state(identifier, status):
        row = get(identifier)
        return row if row["state"]["status"] == status else None

    try:
        api, worker = start("api"), start("worker")
        wait_for(lambda: client.get("/healthz").status_code == 200)
        row = post("/v1/sessions", {"command_id": str(uuid4()), "agent_id": "assistant"})
        turn(row, "Look up asset A17")
        row = wait_for(lambda: state(row["id"], "completed"))
        assert "fixture://assets/A17" in row["state"]["history"][-1]["messages"][-1]["content"]
        context = [body for path, body in server.calls if path == "/context"][0]
        assert context["scope"]["subject"] == "alice"
        assert context["scope"]["tenant_id"] == str(store.settings.tenant_id)
        stop(api)
        stop(worker)
        api, worker = start("api"), start("worker")
        wait_for(lambda: client.get("/healthz").status_code == 200)
        turn(get(row["id"]), "How many did you say?")
        row = wait_for(lambda: state(row["id"], "completed"))
        assert len(row["state"]["history"]) == 2
        turn(row, "Open case for the pump issue")
        row = wait_for(lambda: state(row["id"], "waiting_approval"))
        assert not any(path == "/action" for path, body in server.calls)
        stop(worker)
        client.headers["Authorization"] = "Bearer " + identity("carol")
        post(
            "/v1/sessions/" + row["id"] + "/decisions",
            {
                "command_id": str(uuid4()),
                "expected_revision": row["revision"],
                "action_digest": row["state"]["run"]["pending"]["digest"],
                "approve": True,
                "reason": "Synthetic independent review",
            },
        )
        worker = start("worker")
        client.headers["Authorization"] = "Bearer " + identity()
        row = wait_for(lambda: state(row["id"], "completed"))
        writes = [body for path, body in server.calls if path == "/action"]
        if family:
            requests = [body for path, body in server.calls if path == "/v1/chat/completions"]
            assert len(requests) >= 5
            for body in requests:
                assert body["temperature"] == 0.7 and body["top_k"] == 20
                assert body["chat_template_kwargs"] == {
                    "enable_thinking": True,
                    "preserve_thinking": True,
                }
                assert body["stream"] is True
        events = client.get("/v1/sessions/" + row["id"] + "/events")
        assert (
            "private-tool-thought" not in events.text
            and "private-answer-thought" not in events.text
        )
        assert len(writes) == 1
        assert writes[0]["arguments"]["approved_arguments"] == {
            "query": "Open case for the pump issue"
        }
        assert len(writes[0]["arguments"]["approval_digest"]) == 64
        assert "FIXTURE-17" in row["state"]["history"][-1]["messages"][-1]["content"]
    finally:
        client.close()
        for process in children:
            if process.poll() is None:
                stop(process)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
