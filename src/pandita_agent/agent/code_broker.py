"""Operator-hosted code executor. Reuses the sandbox ledger and fixed OCI isolation.

The worker authenticates with a private transport key. User authorization and capability
checks remain in the harness; this service cannot call models, tools or application data.
"""

import argparse
import asyncio
import hmac
import json
import logging
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request

from pandita_agent.agent.code_contracts import CodeProject, PackageRequest
from pandita_agent.agent.code_environments import Environments
from pandita_agent.agent.sandbox import DockerExecutor, Ledger, outcome
from pandita_agent.agent.sandbox_contracts import BrokerSettings, Job


def create_app(settings, token_file):
    token = Path(token_file).read_text().strip()
    if len(token) < 32:
        raise ValueError("A private transport key is required.")
    ledger = Ledger(settings.state_dir)
    app, active = FastAPI(), 0
    environments = Environments(settings)

    def authenticate(request):
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "Worker authentication required.")

    @app.post("/v1/environments")
    async def build_environment(request: Request):
        authenticate(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, "Package request exceeds its limit.")
        try:
            result = environments.start(PackageRequest.model_validate_json(raw))
            return {k: v for k, v in result.items() if k not in {'image', 'lock'}}
        except ValueError as exc:
            raise HTTPException(422, str(exc)[:2000]) from None

    @app.get("/v1/environments/{key}")
    async def environment_status(key: str, request: Request):
        authenticate(request)
        try:
            result = environments.read(key)
            return {k: v for k, v in result.items() if k not in {'image', 'lock'}}
        except ValueError:
            raise HTTPException(422, "Invalid environment.") from None


    @app.post("/v1/code")
    async def execute(request: Request):
        nonlocal active
        if not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "Worker authentication required.")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 131072:
                raise HTTPException(413, "Code input exceeds the limit.")
        try:
            data = json.loads(raw)
            project = CodeProject.model_validate(data["project"])
            scope = data["scope"]
            if (
                UUID(scope["tenant_id"]) != settings.tenant_id
                or UUID(scope["workspace_id"]) != settings.workspace_id
            ):
                raise ValueError("Wrong scope")
            payload = json.dumps(
                {"project": project.model_dump(), "input": data["input"], "replay": data["replay"]}
            )
            command = (
                "python -I /opt/executor/code_project.py"
                if project.language == "python"
                else "node /opt/executor/code_project.js"
            )
            job = Job.model_validate_json(
                json.dumps(
                    {
                        "version": 1,
                        "operation_id": data["operation_id"],
                        "scope": scope,
                        "arguments": {
                            "kind": "shell",
                            "request": {
                                "code": command,
                                "files": ["project.json"],
                                "output_files": ["result.json"],
                            },
                            "files": {"project.json": payload},
                        },
                    }
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(422, "Invalid code request.") from exc
        if active >= settings.concurrency:
            raise HTTPException(429, "Code runner is busy. Try again shortly.")
        previous = ledger.reserve(job)
        if previous is not None:
            return previous
        active += 1
        try:
            try:
                selected = environments.executor_settings(project)
            except ValueError as exc:
                result = outcome(job, "failed", stderr=str(exc))
            else:
                result = await DockerExecutor(selected).execute(job)
        except (Exception, asyncio.CancelledError):
            logging.getLogger(__name__).error("Code sandbox execution failed; outcome remains unknown.")
            result = outcome(job)
        finally:
            active -= 1
        ledger.complete(job, result)
        return result

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--port", type=int, default=8097)
    args = parser.parse_args()
    settings = BrokerSettings.model_validate_json(args.config.read_text())
    uvicorn.run(
        create_app(settings, args.token_file),
        host="127.0.0.1",
        port=args.port,
        ssl_certfile=args.cert,
        ssl_keyfile=args.key,
        access_log=False,
    )


if __name__ == "__main__":
    main()
