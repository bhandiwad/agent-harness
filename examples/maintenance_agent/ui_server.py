"""Loopback-only sample UI bridge. The actual harness keeps its normal JWT checks."""

import asyncio
import json
from pathlib import Path

import httpx2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from model_setup import SetupError, validate_model
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parent


def create_app(token, initial_session, model, port=8094, *, setup=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    mutation_lock = asyncio.Lock()
    hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    origins = {"http://" + host for host in hosts}

    @app.middleware("http")
    async def local_only(request, call_next):
        if request.headers.get("host") not in hosts:
            return JSONResponse({"detail": "Use the local sample URL."}, status_code=403)
        if request.url.path.startswith(("/v1/", "/sample/")):
            if (
                request.headers.get("x-sample-client") != "1"
                or request.headers.get("origin") not in origins | {None}
                or request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}
            ):
                return JSONResponse(
                    {"detail": "Same-origin sample client required."}, status_code=403
                )
        if request.method == "POST":
            async with mutation_lock:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                "Referrer-Policy": "no-referrer",
            }
        )
        return response

    @app.get("/")
    def home():
        return FileResponse(ROOT / "ui" / "index.html")

    @app.get("/app.js")
    def script():
        return FileResponse(ROOT / "ui" / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    def style():
        return FileResponse(ROOT / "ui" / "style.css", media_type="text/css")

    @app.get("/sample/bootstrap")
    def bootstrap():
        return {
            "session_id": str(initial_session),
            "agent": "maintenance",
            "model": setup.settings.models[0].model_name if setup else model,
            "assets": (ROOT / "data" / "assets.csv").read_text(),
        }

    @app.get("/sample/model-setup")
    def model_setup():
        if setup is None:
            raise HTTPException(503, "Model setup is unavailable in this installation.")
        return setup.snapshot()

    @app.post("/sample/model-validate")
    async def validate_profile(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "Profile exceeds its limit.")
        try:
            return validate_model(json.loads(body)).model_dump(mode="json", exclude_none=True)
        except (ValueError, SetupError):
            raise HTTPException(
                400,
                "Invalid Model JSON. Use a supported family (gemma4 or qwen3_8), or omit execution for a generic compatible model. Remote endpoints need HTTPS; never include credentials in JSON.",
            ) from None

    @app.post("/sample/model-setup")
    async def apply_model(request: Request):
        if setup is None:
            raise HTTPException(503, "Model setup is unavailable in this installation.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "Profile exceeds its limit.")
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid profile request.") from None
        try:
            return await run_in_threadpool(setup.apply, data)
        except SetupError as error:
            raise HTTPException(error.status, str(error)) from None

    @app.api_route("/v1/{path:path}", methods=["GET", "POST"])
    async def proxy(path: str, request: Request):
        if any(value in path for value in ("..", "\\", "%")):
            raise HTTPException(400, "Invalid path.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "Request exceeds its limit.")
        client = httpx2.AsyncClient(
            base_url="http://127.0.0.1:8093", trust_env=False, follow_redirects=False, timeout=70
        )
        try:
            upstream = await client.send(
                client.build_request(
                    request.method,
                    "/v1/" + path,
                    params=request.query_params,
                    content=bytes(body),
                    headers={
                        "Authorization": "Bearer " + token(),
                        "Content-Type": "application/json",
                    },
                ),
                stream=True,
            )
        except httpx2.HTTPError:
            await client.aclose()
            return JSONResponse({"detail": "Harness connection unavailable."}, status_code=503)
        content_type = upstream.headers.get("content-type", "application/json")
        if content_type.startswith("text/event-stream"):

            async def chunks():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                except httpx2.HTTPError:
                    # Reconfiguration closes the old upstream stream; the UI resumes by cursor.
                    return
                finally:
                    await upstream.aclose()
                    await client.aclose()

            return StreamingResponse(
                chunks(),
                status_code=upstream.status_code,
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no"},
            )
        try:
            raw = bytearray()
            async for chunk in upstream.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 4194304:
                    raise HTTPException(502, "Response exceeds its limit.")
            headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() in {"content-type", "content-disposition", "etag"}
            }
            return Response(bytes(raw), status_code=upstream.status_code, headers=headers)
        finally:
            await upstream.aclose()
            await client.aclose()

    return app
