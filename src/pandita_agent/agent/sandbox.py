"""Host-side sandbox broker. Only the broker may contact the operator-owned OCI daemon."""

import argparse
import asyncio
import json
import os
import sqlite3
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request

from pandita_agent.agent.sandbox_contracts import RESULT_SCHEMA, BrokerSettings, Job, JobScope
from pandita_agent.contracts import Contract, canonical, content_digest
from pandita_agent.files import read_bounded
from pandita_agent.governance.tools import validate_value
from pandita_agent.runtime.auth import Access, Forbidden, Unauthorized


class Receipt(Contract):
    version: int
    operation_id: UUID
    scope: JobScope
    arguments: dict


def outcome(job, status="unknown", **values):
    return {
        "operation_id": str(job.operation_id),
        "status": status,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "files": {},
        "isolation": {},
        **values,
    }


class Ledger:
    def __init__(self, directory):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ValueError("Sandbox state directory must be private (0700).")
        self.path = directory / "jobs.sqlite"
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, digest TEXT NOT NULL, scope TEXT NOT NULL, result TEXT NOT NULL)"
            )
        self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def reserve(self, job):
        digest = content_digest(job.model_dump(mode="json"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT digest,result FROM jobs WHERE id=?", (str(job.operation_id),)
            ).fetchone()
            if existing:
                if existing[0] != digest:
                    raise HTTPException(
                        409, "Operation identifier already used for different content."
                    )
                return json.loads(existing[1])
            db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?)",
                (
                    str(job.operation_id),
                    digest,
                    canonical(job.scope.model_dump(mode="json")),
                    canonical(outcome(job)),
                ),
            )
        return None

    def complete(self, job, result):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET result=? WHERE id=?", (canonical(result), str(job.operation_id))
            )

    def get(self, receipt):
        with self.connect() as db:
            row = db.execute(
                "SELECT scope,result FROM jobs WHERE id=?", (str(receipt.operation_id),)
            ).fetchone()
        if not row or row[0] != canonical(receipt.scope.model_dump(mode="json")):
            raise HTTPException(404, "Receipt unavailable.")
        return json.loads(row[1])


class DockerExecutor:
    def __init__(self, settings):
        self.settings = settings
        self.env = {"PATH": "/usr/bin:/bin", "DOCKER_HOST": settings.docker_host}

    def command(self, job):
        cfg = self.settings
        return [
            str(cfg.docker_binary),
            "run",
            "--pull=never",
            "--rm",
            "--init",
            "--name",
            "pandita-sandbox-" + str(job.operation_id),
            "--label",
            "com.pandita.sandbox=1",
            "--runtime",
            cfg.runtime,
            "--user",
            "65532:65532",
            "--network=none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--memory",
            f"{cfg.memory_mb}m",
            "--memory-swap",
            f"{cfg.memory_mb}m",
            "--pids-limit",
            str(cfg.pids),
            "--cpus=1",
            "--shm-size=128m",
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,noexec,size=8m,mode=0700,uid=65532,gid=65532",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777",
            "--workdir",
            "/workspace",
            "-i",
            cfg.images[job.arguments.kind],
        ]

    async def cleanup(self, job):
        process = await asyncio.create_subprocess_exec(
            str(self.settings.docker_binary),
            "rm",
            "-f",
            "pandita-sandbox-" + str(job.operation_id),
            env=self.env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def execute(self, job):
        cfg = self.settings
        payload = {
            **job.arguments.model_dump(mode="json"),
            "require_landlock": cfg.require_landlock,
            "max_seconds": cfg.max_seconds,
        }
        process = await asyncio.create_subprocess_exec(
            *self.command(job),
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read(stream, limit):
            raw = bytearray()
            while chunk := await stream.read(4096):
                raw.extend(chunk)
                if len(raw) > limit:
                    raise ValueError("Sandbox process output exceeded its bound.")
            return bytes(raw)

        async def send():
            process.stdin.write(canonical(payload).encode())
            await process.stdin.drain()
            process.stdin.close()

        try:
            async with asyncio.timeout(cfg.max_seconds + 15):
                async with asyncio.TaskGroup() as group:
                    group.create_task(send())
                    output = group.create_task(read(process.stdout, 98304))
                    group.create_task(read(process.stderr, 8192))
                raw = output.result()
                await process.wait()
                if process.returncode:
                    raise ValueError("Sandbox runtime unavailable or failed.")
                result = json.loads(raw)
                if (
                    set(result) != {"exit_code", "stdout", "stderr", "files"}
                    or type(result["exit_code"]) is not int
                ):
                    raise ValueError("Sandbox returned invalid output.")
                result = outcome(
                    job,
                    "completed" if result["exit_code"] == 0 else "failed",
                    **result,
                    isolation={
                        "runtime": cfg.runtime,
                        "landlock_required": cfg.require_landlock,
                        "network": "none",
                        "host_mounts": False,
                    },
                )
                validate_value(result, RESULT_SCHEMA)
                if (
                    len(result["stdout"].encode()) > 16384
                    or len(result["stderr"].encode()) > 8192
                    or sum(len(v.encode()) for v in result["files"].values()) > 32768
                ):
                    raise ValueError("Sandbox output exceeded its bound.")
                if result["status"] == "completed" and set(result["files"]) != set(
                    job.arguments.request["output_files"]
                ):
                    raise ValueError("Sandbox output inventory mismatch.")
                if result["status"] != "completed":
                    result["files"] = {}
                return result
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await asyncio.shield(self.cleanup(job))


def create_broker(settings, *, executor=None):
    app = FastAPI(title="Pandita isolated execution broker", docs_url=None, redoc_url=None)
    ledger = Ledger(settings.state_dir)
    executor = executor or DockerExecutor(settings)
    active = 0

    async def parse(request, cls):
        auth = request.headers.get("authorization", "")
        try:
            access = Access(settings)
            principal = access.authenticate(
                auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
            )
            access.require(principal, "sandbox:execute")
            if principal.draft_digest is not None:
                raise Forbidden()
        except Unauthorized as exc:
            raise HTTPException(401, "Authentication required.") from exc
        except Forbidden as exc:
            raise HTTPException(403, "Permission required.") from exc
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 196608:
                raise HTTPException(413, "Sandbox request exceeds its limit.")
        try:
            job = cls.model_validate_json(raw)
        except ValueError as exc:
            raise HTTPException(422, "Invalid sandbox request.") from exc
        if (
            job.version != 1
            or job.scope.subject != principal.subject
            or job.scope.tenant_id != settings.tenant_id
            or job.scope.workspace_id != settings.workspace_id
            or request.headers.get("idempotency-key") != str(job.operation_id)
        ):
            raise HTTPException(403, "Invocation scope mismatch.")
        return job, access, principal

    @app.post("/v1/jobs")
    async def execute(request: Request):
        nonlocal active
        job, access, principal = await parse(request, Job)
        try:
            access.require(principal, "sandbox:" + job.arguments.kind)
        except Forbidden as exc:
            raise HTTPException(403, "Sandbox capability not permitted.") from exc
        if job.arguments.kind not in settings.images:
            raise HTTPException(503, "Sandbox image not installed.")
        # Single process admission before durable reservation; never queue arbitrary jobs.
        if active >= settings.concurrency:
            raise HTTPException(429, "Sandbox capacity reached.")
        previous = ledger.reserve(job)
        if previous is not None:
            return previous
        active += 1
        try:
            result = await executor.execute(job)
            ledger.complete(job, result)
            return result
        except (Exception, asyncio.CancelledError):
            # A crash/disconnect is ambiguous. The persisted unknown receipt forbids replay.
            result = outcome(job)
            ledger.complete(job, result)
            return result
        finally:
            active -= 1

    @app.post("/v1/receipts")
    async def receipt(request: Request):
        job, _, _ = await parse(request, Receipt)
        if job.arguments != {"operation_id": str(job.operation_id)}:
            raise HTTPException(422, "Invalid receipt query.")
        return ledger.get(job)

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    settings = BrokerSettings.model_validate_json(read_bounded(args.config, 65536))
    os.umask(0o077)
    uvicorn.run(create_broker(settings), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
