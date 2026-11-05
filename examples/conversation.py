"""Run with AGENT_TOKEN in your environment. No report workflow is involved."""

import os
import time

from pandita_agent.agent.client import Client

client = Client(os.environ.get("AGENT_URL", "http://127.0.0.1:8093"), os.environ["AGENT_TOKEN"])
try:
    creation = client.create("assistant", "My first agent task")
    session = client.execute(creation)  # On transport failure, retry this same creation.
    print("Conversation:", session["id"])
    for question in (
        "What can you help me with?",
        "Remember my project is called Atlas. What did I just tell you?",
    ):
        command = client.prepare(session, "turns", text=question)
        session = client.execute(command)  # On transport failure, retry this same command.
        deadline = time.monotonic() + 310
        while session["state"]["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(1)
            session = client.get("/v1/sessions/" + session["id"])
        print("Status:", session["state"]["status"])
        if session["state"]["status"] != "completed":
            print("Inspect the conversation in the UI for its next action.")
            break
        print(session["state"]["history"][-1]["messages"][-1]["content"])
finally:
    client.close()
