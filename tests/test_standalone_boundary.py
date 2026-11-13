"""Fail if the runtime gains an application dependency or Studio control-plane API."""

import ast
from importlib.metadata import version
from pathlib import Path

from sqlalchemy import text
from pandita_agent.agent.api import create_app
from pandita_agent.agent.runner import Runner
from .test_runner import Model


def test_no_application_imports():
    root = Path(__file__).resolve().parents[1] / "src"
    forbidden = {"flask", "celery", "services", "pandita_harness"}
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert all(n.split(".")[0] not in forbidden for n in names), path


def test_only_core_schema_and_routes(store):
    with store.transaction() as conn:
        tables = set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='agent' ")
            ).scalars()
        )
    assert tables == {"sessions", "events", "commands", "schema_version"}
    schema = create_app(Runner(store, Model())).openapi()
    assert schema["info"]["version"] == version("pandita-agent")
    paths = schema["paths"]
    assert "/v1/sessions" in paths
    assert not any(
        p.startswith(("/v1/authoring", "/v1/operations", "/v1/data-stores"))
        for p in paths
    )
