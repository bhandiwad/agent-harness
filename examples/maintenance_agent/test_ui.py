"""The local convenience identity must never be available to a cross-origin page."""

import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest

sys.path.insert(0, str(Path(__file__).parent))

spec = importlib.util.spec_from_file_location(
    "maintenance_ui", Path(__file__).with_name("ui_server.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.asyncio
async def test_local_gateway_origin_host_and_credential_boundary():
    app = module.create_app(lambda: "PRIVATE_JWT", uuid4(), "test-model")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8094"
    ) as client:
        home = await client.get("/")
        assert (
            home.status_code == 200 and "frame-ancestors" in home.headers["Content-Security-Policy"]
        )
        assert (await client.get("/sample/bootstrap")).status_code == 403
        headers = {"X-Sample-Client": "1", "Origin": "http://localhost:8094"}
        result = await client.get("/sample/bootstrap", headers=headers)
        assert result.status_code == 200 and result.json()["agent"] == "maintenance"
        assert "PRIVATE_JWT" not in result.text
        assert (
            await client.get(
                "/sample/bootstrap", headers={**headers, "Origin": "https://other.invalid"}
            )
        ).status_code == 403
        assert (
            await client.post(
                "/v1/sessions", headers={**headers, "Sec-Fetch-Site": "cross-site"}, json={}
            )
        ).status_code == 403
        assert (
            await client.get(
                "/sample/bootstrap", headers={**headers, "Host": "rebind.invalid:8094"}
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_gateway_forwards_only_server_identity_and_bounds_requests(monkeypatch):
    seen = []

    def upstream(request):
        seen.append(request)
        if request.url.path.endswith("/stream"):
            return httpx2.Response(
                200,
                content=b'event: run.completed\ndata: {"revision":2}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(
            200,
            json={"id": "sample"},
            headers={"authorization": "must-not-reach-browser"},
        )

    app = module.create_app(lambda: "PRIVATE_JWT", uuid4(), "test-model")
    client_type = httpx2.AsyncClient
    async with client_type(
        transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8094"
    ) as client:
        monkeypatch.setattr(
            module.httpx2,
            "AsyncClient",
            lambda **kwargs: client_type(transport=httpx2.MockTransport(upstream), **kwargs),
        )
        headers = {"X-Sample-Client": "1", "Authorization": "Bearer untrusted"}
        response = await client.post(
            "/v1/sessions", headers=headers, json={"agent_id": "maintenance"}
        )
        assert response.status_code == 200 and response.json() == {"id": "sample"}
        assert "authorization" not in response.headers and "PRIVATE_JWT" not in response.text
        assert seen[0].headers["authorization"] == "Bearer PRIVATE_JWT"
        assert str(seen[0].url) == "http://127.0.0.1:8093/v1/sessions"
        assert seen[0].read() == b'{"agent_id":"maintenance"}'
        response = await client.get("/v1/sessions/sample/stream?after=1", headers=headers)
        assert response.status_code == 200 and "run.completed" in response.text
        assert seen[1].url.query == b"after=1"
        response = await client.post("/v1/sessions", headers=headers, content=b"x" * 65537)
        assert response.status_code == 413 and len(seen) == 2


@pytest.mark.asyncio
async def test_profile_validation_and_setup_keep_credentials_out_of_responses():
    from model_setup import SetupError

    class Setup:
        def snapshot(self):
            return {"version": "0.3.4"}

        def apply(self, data):
            raise SetupError("Finish active conversations first.", 409)

    app = module.create_app(lambda: "PRIVATE_JWT", uuid4(), "test-model", setup=Setup())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8094"
    ) as client:
        headers = {"X-Sample-Client": "1"}
        model = {
            "model_ref": "qwen-custom",
            "model_name": "my-qwen",
            "base_url": "https://example.invalid/v1",
            "api_key_env": "MODEL_API_KEY",
            "execution": {"id": "custom", "family": "qwen3_8"},
        }
        good = await client.post("/sample/model-validate", headers=headers, json=model)
        assert good.status_code == 200 and good.json()["execution"]["enable_thinking"] is True
        bad = await client.post(
            "/sample/model-validate",
            headers=headers,
            json={**model, "api_key": "PRIVATE_MODEL_KEY"},
        )
        assert bad.status_code == 400 and "PRIVATE_MODEL_KEY" not in bad.text
        assert (await client.post("/sample/model-setup", json={})).status_code == 403
        conflict = await client.post(
            "/sample/model-setup", headers=headers, json={"api_key": "PRIVATE_MODEL_KEY"}
        )
        assert conflict.status_code == 409 and "PRIVATE_MODEL_KEY" not in conflict.text
        assert (
            await client.post("/sample/model-setup", headers=headers, content=b"x" * 65537)
        ).status_code == 413


@pytest.mark.asyncio
async def test_gateway_closes_interrupted_stream_for_cursor_reconnect(monkeypatch):
    class Broken(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: model.started\ndata: {"revision":3}\n\n'
            raise httpx2.ReadError("Synthetic worker restart")

    client_type = httpx2.AsyncClient

    def upstream(request):
        return httpx2.Response(200, stream=Broken(), headers={"content-type": "text/event-stream"})

    app = module.create_app(lambda: "PRIVATE_JWT", uuid4(), "fixture")
    async with client_type(
        transport=httpx2.ASGITransport(app=app), base_url="http://localhost:8094"
    ) as client:
        monkeypatch.setattr(
            module.httpx2,
            "AsyncClient",
            lambda **kwargs: client_type(transport=httpx2.MockTransport(upstream), **kwargs),
        )
        result = await client.get("/v1/sessions/fixture/stream", headers={"X-Sample-Client": "1"})
        assert result.status_code == 200 and '"revision":3' in result.text
        assert "PRIVATE_JWT" not in result.text
