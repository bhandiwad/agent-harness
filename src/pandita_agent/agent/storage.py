"""Independent PostgreSQL session ledger; no report tables or report installation required."""

from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from pandita_agent.contracts import canonical, content_digest


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


def database(settings, environ):
    raw = environ.get(settings.database_url_env, "")
    url = make_url(raw)
    if url.drivername != "postgresql+psycopg" or not url.username:
        raise ValueError("Configure PostgreSQL with the psycopg driver.")
    if (
        url.host not in {"localhost", "127.0.0.1", "::1"}
        and url.query.get("sslmode") != "verify-full"
    ):
        raise ValueError("Remote PostgreSQL requires verified TLS.")
    return create_engine(
        url,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=3,
        connect_args={"connect_timeout": 5},
    )


def migrate(engine):
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(781931339)"))
        exists = conn.execute(
            text("SELECT to_regclass('agent.schema_version')")
        ).scalar()
        if exists:
            if (
                conn.execute(
                    text("SELECT version FROM agent.schema_version")
                ).scalar_one()
                != 1
            ):
                raise ValueError(
                    "Use a fresh standalone database; Studio schemas are not compatible."
                )
            return
        conn.execute(text("CREATE SCHEMA agent"))
        conn.execute(
            text("CREATE TABLE agent.schema_version (version integer PRIMARY KEY)")
        )
        conn.execute(text("INSERT INTO agent.schema_version VALUES (1)"))
        conn.execute(
            text("""CREATE TABLE agent.sessions (
            tenant_id uuid NOT NULL, workspace_id uuid NOT NULL, id uuid NOT NULL,
            owner text NOT NULL, revision integer NOT NULL, state jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id,workspace_id,id))""")
        )
        conn.execute(
            text("""CREATE TABLE agent.events (
            tenant_id uuid NOT NULL, workspace_id uuid NOT NULL, session_id uuid NOT NULL,
            revision integer NOT NULL, kind text NOT NULL, actor text NOT NULL, data jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (tenant_id,workspace_id,session_id,revision),
            FOREIGN KEY (tenant_id,workspace_id,session_id) REFERENCES agent.sessions(tenant_id,workspace_id,id))""")
        )
        conn.execute(
            text("""CREATE TABLE agent.commands (
            tenant_id uuid NOT NULL, workspace_id uuid NOT NULL, id uuid NOT NULL,
            session_id uuid NOT NULL, digest text NOT NULL,
            PRIMARY KEY (tenant_id,workspace_id,id))""")
        )
        for table in ("sessions", "events", "commands"):
            conn.execute(text(f"ALTER TABLE agent.{table} ENABLE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE agent.{table} FORCE ROW LEVEL SECURITY"))
            conn.execute(
                text(f"""CREATE POLICY scope ON agent.{table}
                USING (tenant_id=nullif(current_setting('agent.tenant_id',true),'')::uuid
                   AND workspace_id=nullif(current_setting('agent.workspace_id',true),'')::uuid)
                WITH CHECK (tenant_id=nullif(current_setting('agent.tenant_id',true),'')::uuid
                   AND workspace_id=nullif(current_setting('agent.workspace_id',true),'')::uuid)""")
            )
        conn.execute(
            text("""CREATE FUNCTION agent.immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Agent audit records are immutable'; END $$""")
        )
        for table in ("events", "commands"):
            conn.execute(
                text(
                    f"CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON agent.{table} FOR EACH ROW EXECUTE FUNCTION agent.immutable()"
                )
            )
        conn.execute(
            text(
                "CREATE INDEX agent_pending ON agent.sessions (tenant_id,workspace_id,updated_at) WHERE state->>'status' IN ('queued','running','cancelling','reconciling')"
            )
        )


def check_role(engine):
    with engine.connect() as conn:
        if any(
            conn.execute(
                text(
                    "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
                )
            ).one()
        ):
            raise ValueError("Runtime role cannot bypass row isolation.")
        if (
            conn.execute(text("SELECT version FROM agent.schema_version")).scalar_one()
            != 1
        ):
            raise ValueError("Unsupported agent schema.")
        rows = conn.execute(
            text("""SELECT c.relrowsecurity,c.relforcerowsecurity,r.rolname=current_user
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_roles r ON r.oid=c.relowner
            WHERE n.nspname='agent' AND c.relname IN ('sessions','events','commands')""")
        ).all()
        if len(rows) != 3 or any(not a or not b or owned for a, b, owned in rows):
            raise ValueError("Use a non-owner role and forced row isolation.")


class Store:
    def __init__(self, engine, settings):
        self.engine, self.settings = engine, settings
        self.scope = {
            "tenant": str(settings.tenant_id),
            "workspace": str(settings.workspace_id),
        }

    @contextmanager
    def transaction(self):
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "SELECT set_config('agent.tenant_id',:tenant,true),set_config('agent.workspace_id',:workspace,true)"
                ),
                self.scope,
            )
            yield conn

    def get(self, session_id, conn=None, lock=False):
        if conn is None:
            with self.transaction() as conn:
                return self.get(session_id, conn)
        row = (
            conn.execute(
                text(
                    "SELECT * FROM agent.sessions WHERE id=:id"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"id": session_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound("Conversation not found.")
        return dict(row)

    def visibility_clause(self):
        return "TRUE"

    def list(self, owner=None, *, pending=False, viewer=None):
        with self.transaction() as conn:
            query = "SELECT id,owner,revision,state->>'title' AS title,state->>'status' AS status,updated_at FROM agent.sessions"
            where = [self.visibility_clause()] if viewer is not None else []
            if owner is not None:
                where.append("owner=:owner")
            if pending:
                where.append(
                    "(state->>'status' IN ('queued','running','cancelling','reconciling') OR (state->>'status' IN ('waiting_input','waiting_approval') AND (state->'run'->'pending'->>'expires_at')::double precision < extract(epoch FROM clock_timestamp())))"
                )
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        query
                        + (" WHERE " + " AND ".join(where) if where else "")
                        + (
                            " ORDER BY updated_at,id"
                            if pending
                            else " ORDER BY updated_at DESC,id DESC"
                        )
                        + " LIMIT 100"
                    ),
                    {"owner": owner, "viewer": viewer},
                ).mappings()
            ]

    def save(self, conn, row, kind, actor, data=None):
        if len(canonical(row["state"]).encode()) > 2097152:
            raise Conflict("Conversation storage limit reached; start a new session.")
        row["revision"] += 1
        conn.execute(
            text(
                "UPDATE agent.sessions SET state=CAST(:state AS jsonb),revision=:revision,updated_at=clock_timestamp() WHERE id=:id"
            ),
            {
                "id": row["id"],
                "state": canonical(row["state"]),
                "revision": row["revision"],
            },
        )
        self.event(conn, row, kind, actor, data)
        return row

    def usage_stamp(self, row, kind, data):
        from pandita_agent.agent.usage import stamp

        return stamp(self.settings, row, kind, data)

    def budget_policy(self, conn):
        return self.settings.budgets

    def event(self, conn, row, kind, actor, data=None):
        data = self.usage_stamp(row, kind, data or {})
        from pandita_agent.agent.budgets import apply

        data = apply(self, conn, row, kind, data)
        conn.execute(
            text(
                "INSERT INTO agent.events (tenant_id,workspace_id,session_id,revision,kind,actor,data) VALUES (:tenant,:workspace,:id,:revision,:kind,:actor,CAST(:data AS jsonb))"
            ),
            {
                **self.scope,
                "id": row["id"],
                "revision": row["revision"],
                "kind": kind,
                "actor": actor,
                "data": canonical(data or {}),
            },
        )

    def command(self, conn, command_id, digest, session_id):
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": str(command_id)},
        )
        prior = (
            conn.execute(
                text("SELECT * FROM agent.commands WHERE id=:id"), {"id": command_id}
            )
            .mappings()
            .first()
        )
        if prior:
            if prior["digest"] != digest or str(prior["session_id"]) != str(session_id):
                raise Conflict("Command ID already used for different content.")
            return False
        conn.execute(
            text(
                "INSERT INTO agent.commands VALUES (:tenant,:workspace,:id,:session,:digest)"
            ),
            {**self.scope, "id": command_id, "session": session_id, "digest": digest},
        )
        return True

    def create(self, request, owner, *, metadata=None):
        session_id = request.command_id
        identity = {"request": request.model_dump(mode="json"), "owner": owner}
        metadata = dict(metadata or {})
        if set(metadata) & {
            "request",
            "owner",
            "title",
            "agent_id",
            "config_digest",
            "status",
            "history",
            "evidence",
            "memory",
            "run",
            "files",
        }:
            raise ValueError("Session metadata cannot replace runtime state.")
        identity.update(metadata)
        digest = content_digest(identity)
        with self.transaction() as conn:
            if not self.command(conn, request.command_id, digest, session_id):
                return self.get(session_id, conn)
            state = {
                "title": request.title,
                "agent_id": request.agent_id,
                "config_digest": self.settings.digest,
                "status": "idle",
                "history": [],
                "evidence": {"version": 1, "complete": True, "sources": []},
                "memory": {},
                "run": None,
                **metadata,
            }
            conn.execute(
                text(
                    "INSERT INTO agent.sessions (tenant_id,workspace_id,id,owner,revision,state) VALUES (:tenant,:workspace,:id,:owner,1,CAST(:state AS jsonb))"
                ),
                {
                    **self.scope,
                    "id": session_id,
                    "owner": owner,
                    "state": canonical(state),
                },
            )
            row = self.get(session_id, conn)
            self.event(conn, row, "session.created", owner)
            return row

    def events(self, session_id, after=0):
        with self.transaction() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        "SELECT revision,kind,actor,data,created_at FROM agent.events WHERE session_id=:id AND revision>:after ORDER BY revision LIMIT 100"
                    ),
                    {"id": session_id, "after": after},
                ).mappings()
            ]

    @contextmanager
    def writer(self, session_id):
        # Session-level advisory lock survives commits, and is released on connection loss.
        with self.engine.connect() as conn:
            key = int.from_bytes(UUID(str(session_id)).bytes[:8], "big", signed=True)
            owned = conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            ).scalar_one()
            conn.commit()
            try:
                yield owned
            finally:
                if owned:
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                    conn.commit()
