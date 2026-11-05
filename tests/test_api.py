from uuid import uuid4

import httpx2

from pandita_agent.agent.api import create_app
from pandita_agent.agent.runner import Runner

from .test_runner import Model


async def test_authenticated_generic_question_reload_and_trace(store, identity):
    runner = Runner(store, Model("Hello. How can I help?", "You greeted me earlier."))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(runner)), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/agents")).status_code == 401
        client.headers["Authorization"] = "Bearer " + identity()
        assert (await client.get("/v1/agents")).status_code == 200
        row = (
            await client.post(
                "/v1/sessions", json={"command_id": str(uuid4()), "agent_id": "assistant"}
            )
        ).json()
        assert row["state"]["status"] == "idle"
        path = "/v1/sessions/" + row["id"]
        response = await client.post(
            path + "/turns",
            json={
                "command_id": str(uuid4()),
                "expected_revision": row["revision"],
                "text": "Hello",
            },
        )
        assert response.status_code == 200
        await runner.tick(row["id"])
        read = (await client.get(path)).json()
        assert read["state"]["history"][0]["messages"][-1]["content"] == "Hello. How can I help?"
        assert len((await client.get(path + "/events")).json()["events"]) >= 4
        client.headers["Authorization"] = "Bearer " + identity(tenant_id=str(uuid4()))
        assert (await client.get(path)).status_code == 401
        client.headers["Authorization"] = "Bearer " + identity(draft_digest="0" * 64)
        assert (await client.get(path)).status_code == 401


async def test_input_validation_does_not_echo_private_payload(store, identity):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(Runner(store, Model()))),
        base_url="http://test",
        headers={"Authorization": "Bearer " + identity()},
    ) as client:
        response = await client.post("/v1/sessions", json={"secret": "PRIVATE-VALUE"})
        assert response.status_code == 422 and "PRIVATE-VALUE" not in response.text
