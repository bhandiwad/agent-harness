import json
import os
from time import time
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, text

from pandita_agent.agent.contracts import Settings
from pandita_agent.agent.storage import Store, migrate


@pytest.fixture(scope="session")
def generic_database():
    url = os.environ.get("AGENT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set AGENT_TEST_DATABASE_URL to an EMPTY disposable PostgreSQL database")
    admin = create_engine(url, hide_parameters=True)
    with admin.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"
                )
            ).scalar_one()
            == 0
        )
    migrate(admin)
    migrate(admin)
    with admin.begin() as conn:
        conn.execute(
            text("CREATE ROLE generic_test LOGIN PASSWORD 'fixture-only' NOSUPERUSER NOBYPASSRLS")
        )
        conn.execute(text("GRANT USAGE ON SCHEMA agent TO generic_test"))
        conn.execute(text("GRANT SELECT ON agent.schema_version TO generic_test"))
        conn.execute(text("GRANT SELECT,INSERT,UPDATE ON agent.sessions TO generic_test"))
        conn.execute(
            text(
                "GRANT SELECT,INSERT ON agent.events,agent.commands TO generic_test"
            )
        )
    engine = create_engine(
        admin.url.set(username="generic_test", password="fixture-only"), hide_parameters=True
    )
    yield engine
    engine.dispose()
    with admin.begin() as conn:
        conn.execute(text("DROP SCHEMA agent CASCADE"))
        conn.execute(text("DROP ROLE generic_test"))
    admin.dispose()


@pytest.fixture
def settings(tmp_path):
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    return Settings.model_validate_json(
        json.dumps(
            {
                "tenant_id": str(uuid4()),
                "workspace_id": str(uuid4()),
                "issuer": "https://fixture.invalid",
                "audience": "generic-test",
                "jwks_file": str(tmp_path / "jwks.json"),
                "policy_file": str(tmp_path / "policy.csv"),
                "models": [
                    {
                        "model_ref": "local",
                        "model_name": "fixture",
                        "base_url": "http://127.0.0.1:9999/v1",
                        "api_key_env": "FIXTURE_KEY",
                    }
                ],
                "agents": [
                    {
                        "id": "assistant",
                        "instructions": "Help the user. Use context_lookup for source facts.",
                        "model": "local",
                        "tools": ["context_lookup", "create_case"],
                        "delegates": ["specialist"],
                        "capacity": {
                            "profile": "fixture",
                            "context_tokens": 64000,
                            "output_tokens": 2048,
                        },
                    },
                    {
                        "id": "specialist",
                        "instructions": "Answer the assigned subtask.",
                        "model": "local",
                        "tools": ["context_lookup"],
                        "capacity": {
                            "profile": "fixture",
                            "context_tokens": 64000,
                            "output_tokens": 2048,
                        },
                    },
                ],
                "connections": [
                    {
                        "tool": {
                            "name": name,
                            "version": "v1",
                            "owner": "operator",
                            "transport": "rest",
                            "effect": effect,
                            "permission": "tool:" + effect,
                            "input_schema": schema,
                            "output_schema": {"type": "object"},
                            "approval_required": effect == "write",
                        },
                        "binding": {
                            "endpoint": "http://127.0.0.1:9998",
                            "operations": {name: "/invoke"},
                            "status_path": "/receipt",
                        },
                    }
                    for name, effect in (("context_lookup", "read"), ("create_case", "write"))
                ],
            }
        )
    )


@pytest.fixture
def identity(settings):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="fixture", alg="RS256", use="sig")
    settings.jwks_file.write_text(json.dumps({"keys": [public]}))
    domain = f"{settings.tenant_id}/{settings.workspace_id}"
    lines = [
        f"p, operator, {domain}, {permission}"
        for permission in (
            "session:create",
            "session:read",
            "session:cancel",
            "session:approve",
            "agent:assistant",
            "agent:specialist",
            "tool:read",
            "tool:write",
        )
    ]
    lines += [f"g, {actor}, operator, {domain}" for actor in ("alice", "bob")]
    lines += [
        f"p, reviewer, {domain}, {permission}" for permission in ("session:read", "session:approve")
    ]
    lines.append(f"g, carol, reviewer, {domain}")
    settings.policy_file.write_text("\n".join(lines))

    def token(subject="alice", **changes):
        claims = {
            "sub": subject,
            "tenant_id": str(settings.tenant_id),
            "iss": settings.issuer,
            "aud": settings.audience,
            "iat": int(time()),
            "exp": int(time()) + 600,
            **changes,
        }
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture"})

    return token


@pytest.fixture
def store(generic_database, settings, identity):
    return Store(generic_database, settings)
