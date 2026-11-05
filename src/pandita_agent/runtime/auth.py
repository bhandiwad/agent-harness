"""Pinned local OIDC trust keys and Casbin domain permissions; no token-directed fetch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import casbin
import jwt
from casbin.persist.adapters import FileAdapter

from pandita_agent.files import read_bounded

if TYPE_CHECKING:
    from pandita_agent.agent.contracts import Settings

MODEL = """
[request_definition]
r = sub, dom, act
[policy_definition]
p = sub, dom, act
[role_definition]
g = _, _, _
[policy_effect]
e = some(where (p.eft == allow))
[matchers]
m = g(r.sub, p.sub, r.dom) && r.dom == p.dom && r.act == p.act
"""


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: UUID
    draft_digest: str | None = None


class Unauthorized(Exception):
    pass


class Forbidden(Exception):
    pass


class Access:
    def __init__(self, settings: Settings):
        self.settings = settings
        keys = jwt.PyJWKSet.from_json(read_bounded(settings.jwks_file, 65536).decode()).keys
        self.keys = {
            key.key_id: key
            for key in keys
            if key.algorithm_name == "RS256" and key.public_key_use in (None, "sig")
        }
        if not self.keys or None in self.keys or len(self.keys) != len(keys):
            raise ValueError("Configure unique, named RS256 signing keys.")
        model = casbin.Model()
        model.load_model_from_text(MODEL)
        self.policy = casbin.Enforcer(
            model, FileAdapter(str(settings.policy_file)), enable_log=False
        )
        self.domain = f"{settings.tenant_id}/{settings.workspace_id}"

    def authenticate(self, token: str) -> Principal:
        try:
            if len(token) > 16384:
                raise ValueError("Token too long")
            header = jwt.get_unverified_header(token)
            key = self.keys[header["kid"]]
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "tenant_id"]},
            )
            subject = claims["sub"]
            if not isinstance(subject, str) or not 1 <= len(subject) <= 256:
                raise ValueError("Invalid subject")
            if not isinstance(claims["tenant_id"], str):
                raise ValueError("Invalid tenant")
            tenant = UUID(claims["tenant_id"])
            if tenant != self.settings.tenant_id:
                raise ValueError("Foreign tenant")
            if "workspace_id" in claims and (
                not isinstance(claims["workspace_id"], str)
                or UUID(claims["workspace_id"]) != self.settings.workspace_id
            ):
                raise ValueError("Foreign workspace")
            digest = claims.get("draft_digest")
            if digest is not None and (not isinstance(digest, str) or len(digest) != 64):
                raise ValueError("Invalid draft binding")
            return Principal(subject, tenant, digest)
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise Unauthorized() from exc

    def require(self, principal: Principal, action: str) -> None:
        if principal.tenant_id != self.settings.tenant_id or not self.policy.enforce(
            principal.subject, self.domain, action
        ):
            raise Forbidden()
