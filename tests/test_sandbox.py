import json
from uuid import uuid4

import httpx2
import pytest

from pandita_agent.agent.contracts import Connection, Upload
from pandita_agent.agent.runner import Runner
from pandita_agent.agent.sandbox import DockerExecutor, create_broker, outcome
from pandita_agent.agent.sandbox_contracts import (
    CODE_SCHEMA,
    RESULT_SCHEMA,
    BrokerSettings,
    Job,
    SandboxBinding,
)
from pandita_agent.agent.storage import Store
from pandita_agent.governance.tools import RestAdapter, RestBinding

from .test_runner import Model, call, new, send


def broker_settings(settings, tmp_path):
    return BrokerSettings.model_validate_json(
        json.dumps(
            {
                **{
                    key: str(getattr(settings, key))
                    for key in (
                        "tenant_id",
                        "workspace_id",
                        "issuer",
                        "audience",
                        "jwks_file",
                        "policy_file",
                    )
                },
                "state_dir": str(tmp_path / "broker"),
                "images": {"python": "sha256:" + "a" * 64},
            }
        )
    )


def job(settings, **arguments):
    return Job.model_validate_json(
        json.dumps(
            {
                "version": 1,
                "operation_id": str(uuid4()),
                "scope": {
                    "tenant_id": str(settings.tenant_id),
                    "workspace_id": str(settings.workspace_id),
                    "subject": "alice",
                    "task_id": str(uuid4()),
                    "plan_revision": 1,
                },
                "arguments": {
                    "kind": "python",
                    "request": {"code": "print(42)", "files": [], "output_files": []},
                    "files": {},
                    **arguments,
                },
            }
        )
    )


class Executor:
    def __init__(self):
        self.calls = []

    async def execute(self, job):
        self.calls.append(job)
        return outcome(
            job,
            "completed",
            exit_code=0,
            stdout="42",
            files={name: "42" for name in job.arguments.request["output_files"]},
        )


def permissions(settings):
    domain = f"{settings.tenant_id}/{settings.workspace_id}"
    with settings.policy_file.open("a") as file:
        file.write(
            "".join(
                f"\np, operator, {domain}, {permission}\n"
                for permission in (
                    "sandbox:execute",
                    "sandbox:python",
                    "workspace:read",
                    "workspace:write",
                )
            )
        )


async def test_broker_auth_scope_replay_collision_and_durable_receipt(settings, identity, tmp_path):
    cfg = broker_settings(settings, tmp_path)
    permissions(settings)
    executor = Executor()
    app = create_broker(cfg, executor=executor)
    request = job(settings)
    body = request.model_dump(mode="json")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://broker"
    ) as client:
        assert (await client.post("/v1/jobs", json=body)).status_code == 401
        headers = {
            "Authorization": "Bearer " + identity(),
            "Idempotency-Key": str(request.operation_id),
        }
        response = await client.post("/v1/jobs", headers=headers, json=body)
        assert response.status_code == 200 and response.json()["stdout"] == "42"
        assert (await client.post("/v1/jobs", headers=headers, json=body)).json() == response.json()
        body["arguments"]["request"]["code"] = "print(43)"
        assert (await client.post("/v1/jobs", headers=headers, json=body)).status_code == 409
        body["scope"]["subject"] = "bob"
        assert (await client.post("/v1/jobs", headers=headers, json=body)).status_code == 403
        assert (
            await client.post(
                "/v1/jobs",
                headers={**headers, "Authorization": "Bearer " + identity("carol")},
                json=body,
            )
        ).status_code == 403
    assert len(executor.calls) == 1
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_broker(cfg, executor=executor)),
        base_url="http://broker",
    ) as client:
        query = {
            **request.model_dump(mode="json"),
            "arguments": {"operation_id": str(request.operation_id)},
        }
        assert (
            await client.post("/v1/receipts", headers=headers, json=query)
        ).json() == response.json()


async def test_crashed_broker_job_never_reexecutes(settings, identity, tmp_path):
    cfg = broker_settings(settings, tmp_path)
    permissions(settings)

    class Crash(Executor):
        async def execute(self, job):
            self.calls.append(job)
            raise TimeoutError()

    executor = Crash()
    request = job(settings)
    for _ in range(2):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=create_broker(cfg, executor=executor)),
            base_url="http://broker",
        ) as client:
            response = await client.post(
                "/v1/jobs",
                json=request.model_dump(mode="json"),
                headers={
                    "Authorization": "Bearer " + identity(),
                    "Idempotency-Key": str(request.operation_id),
                },
            )
            assert response.json()["status"] == "unknown"
    assert len(executor.calls) == 1


def test_sandbox_command_has_no_host_mounts_or_ambient_environment(settings, tmp_path):
    cfg = broker_settings(settings, tmp_path)
    executor = DockerExecutor(cfg)
    command = executor.command(job(settings))
    assert "--network=none" in command and "--read-only" in command and "--cap-drop=ALL" in command
    assert command[command.index("--runtime") + 1] == "runsc"
    assert command[command.index("--user") + 1] == "65532:65532"
    assert not {"-v", "--mount", "--privileged", "--env", "-e"} & set(command)
    assert set(executor.env) == {"PATH", "DOCKER_HOST"}
    with pytest.raises(ValueError):
        BrokerSettings.model_validate_json(
            cfg.model_copy(update={"runtime": "runc"}).model_dump_json()
        )
    with pytest.raises(ValueError):
        job(
            settings,
            request={"code": "print(1)", "files": ["../secret"], "output_files": []},
            files={"../secret": "value"},
        )


async def test_sandbox_full_agent_broker_artifact_flow(store, identity, tmp_path):
    cfg = broker_settings(store.settings, tmp_path)
    permissions(store.settings)
    executor = Executor()
    app = create_broker(cfg, executor=executor)
    tool = store.settings.connections[0].tool.model_copy(
        update={"transport": "sandbox", "input_schema": CODE_SCHEMA, "output_schema": RESULT_SCHEMA}
    )
    conn = Connection(
        tool=tool, binding=SandboxBinding(endpoint="http://127.0.0.1:8091", kind="python")
    )
    settings = store.settings.model_copy(
        update={"connections": (conn, store.settings.connections[1])}
    )
    local = Store(store.engine, settings)

    async def credential(context):
        return identity(context.subject)

    adapter = RestAdapter(
        RestBinding(endpoint="http://127.0.0.1:8091", operations={tool.name: "/v1/jobs"}),
        credential,
        transport=httpx2.ASGITransport(app=app),
    )
    model = Model(
        call(tool.name, code="print(42)", files=["input.txt"], output_files=["answer.txt"]),
        "Saved answer.",
    )
    runner = Runner(local, model, adapters={tool.name: adapter})
    row = new(runner)
    row = runner.upload(
        row["id"],
        Upload(
            command_id=uuid4(), expected_revision=row["revision"], path="input.txt", content="6 * 7"
        ),
        "alice",
    )
    send(runner, row)
    await runner.tick(row["id"])
    state = local.get(row["id"])["state"]
    assert state["status"] == "completed"
    assert state["files"]["answer.txt"]["content"] == "42"
    assert executor.calls[0].arguments.files == {"input.txt": "6 * 7"}
    assert json.loads(model.requests[-1]["messages"][-1]["content"])["isolation"] == {}
