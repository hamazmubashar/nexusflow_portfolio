"""Authentication: HMAC-SHA256 service signatures (FastAPI <-> n8n) and JWT user RBAC.

Wire format (both directions):
    X-NexusFlow-Timestamp: <unix seconds>
    X-NexusFlow-Signature: sha256=<hex HMAC(secret, f"{timestamp}.{raw_body}")>

The signature covers the exact body bytes, so the receiver must verify before
parsing. The timestamp bounds replay to WEBHOOK_TOLERANCE_SECONDS; replays within
that window are caught by event_id idempotency in automation_events.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
import time
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Literal

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.errors import APIError

TIMESTAMP_HEADER = "X-NexusFlow-Timestamp"
SIGNATURE_HEADER = "X-NexusFlow-Signature"
EVENT_ID_HEADER = "X-NexusFlow-Event-Id"
EVENT_TYPE_HEADER = "X-NexusFlow-Event-Type"


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def sign_headers(secret: str, body: bytes, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(timestamp if timestamp is not None else time.time()))
    return {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: compute_signature(secret, ts, body)}


class SignatureError(Exception):
    pass


def verify_signature(
    secret: str, timestamp: str | None, signature: str | None, body: bytes,
    tolerance_seconds: int, now: float | None = None,
) -> None:
    if not secret:
        raise SignatureError("webhook signing secret is not configured")
    if not timestamp or not signature:
        raise SignatureError("missing signature headers")
    try:
        ts = int(timestamp)
    except ValueError:
        raise SignatureError("invalid timestamp") from None
    if abs((now if now is not None else time.time()) - ts) > tolerance_seconds:
        raise SignatureError("timestamp outside tolerance window")
    if not hmac.compare_digest(compute_signature(secret, timestamp, body), signature):
        raise SignatureError("signature mismatch")


async def require_signature(request: Request) -> bytes:
    """FastAPI dependency: verify the signature over the raw body; returns the body bytes."""
    settings = get_settings()
    body = await request.body()
    try:
        verify_signature(
            settings.webhook_signing_secret,
            request.headers.get(TIMESTAMP_HEADER),
            request.headers.get(SIGNATURE_HEADER),
            body,
            settings.webhook_tolerance_seconds,
        )
    except SignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail={"code": "INVALID_SIGNATURE", "message": str(exc)})
    return body


# =========================================================================== RBAC (Stage 5)
#
# Two kinds of principal:
#   * users    - `Authorization: Bearer <JWT>` (HS256; iss/aud/exp/iat/sub/roles required).
#                Roles: OPERATOR < MANAGER < ADMIN (each includes the ones before it).
#   * services - n8n and other internal callers, authenticated by the HMAC signature above.
#                They get the SERVICE role only, which user roles never imply.
# Tokens are "JWT-ready": swap `decode_jwt` for an IdP's JWKS verification (RS256) without
# touching any route. Issue local dev tokens with:
#     python -m app.core.security issue-token EMP-0082 MANAGER

class Role(StrEnum):
    OPERATOR = "OPERATOR"
    MANAGER = "MANAGER"
    ADMIN = "ADMIN"
    SERVICE = "SERVICE"


USER_ROLES = {Role.OPERATOR, Role.MANAGER, Role.ADMIN}
IMPLIED = {Role.ADMIN: {Role.ADMIN, Role.MANAGER, Role.OPERATOR},
           Role.MANAGER: {Role.MANAGER, Role.OPERATOR},
           Role.OPERATOR: {Role.OPERATOR},
           Role.SERVICE: {Role.SERVICE}}
JWT_ALGORITHM = "HS256"
DEMO_SUBJECT = "DEMO-VISITOR"


class Principal(BaseModel):
    subject: str                       # EMP-XXXX for users, service name for services
    name: str | None = None
    kind: Literal["user", "service"]
    roles: list[Role]                  # as granted
    effective_roles: list[Role]        # after hierarchy expansion
    auth: Literal["jwt", "hmac"]

    def has(self, role: Role) -> bool:
        return role in self.effective_roles


def jwt_key() -> bytes:
    s = get_settings()
    if s.jwt_secret:
        return s.jwt_secret.encode()
    if not s.webhook_signing_secret:
        raise RuntimeError("Set JWT_SECRET (or WEBHOOK_SIGNING_SECRET) to use JWT auth")
    return hmac.new(s.webhook_signing_secret.encode(), b"nexusflow/jwt/v1", hashlib.sha256).digest()


def issue_jwt(subject: str, roles: list[str], name: str | None = None, ttl_minutes: int | None = None,
              now: datetime | None = None) -> str:
    s = get_settings()
    bad = set(roles) - {r.value for r in USER_ROLES}
    if bad or not roles:
        raise ValueError(f"roles must be a non-empty subset of {sorted(r.value for r in USER_ROLES)}; got {roles}")
    now = now or datetime.now(timezone.utc)
    claims = {"iss": s.jwt_issuer, "aud": s.jwt_audience, "sub": subject, "roles": sorted(set(roles)),
              "iat": int(now.timestamp()),
              "exp": int((now + timedelta(minutes=ttl_minutes or s.jwt_ttl_minutes)).timestamp())}
    if name:
        claims["name"] = name
    return jwt.encode(claims, jwt_key(), algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    s = get_settings()
    try:
        return jwt.decode(token, jwt_key(), algorithms=[JWT_ALGORITHM], audience=s.jwt_audience, issuer=s.jwt_issuer,
                          options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.ExpiredSignatureError:
        raise APIError(401, "TOKEN_EXPIRED", "access token has expired") from None
    except jwt.InvalidTokenError as exc:
        raise APIError(401, "INVALID_TOKEN", f"access token is invalid: {exc}") from None


def _expand(roles: list[Role]) -> list[Role]:
    out: set[Role] = set()
    for r in roles:
        out |= IMPLIED[r]
    return sorted(out)


# These only feed the OpenAPI security schemes; verification happens in get_current_user
_bearer = HTTPBearer(auto_error=False, scheme_name="bearerAuth",
                     description="User JWT (HS256). Roles: OPERATOR < MANAGER < ADMIN.")
_hmac = APIKeyHeader(name=SIGNATURE_HEADER, auto_error=False, scheme_name="hmacSignature",
                     description="Service auth: sha256=HMAC(secret, '<X-NexusFlow-Timestamp>.<raw body>'); "
                                 "also send X-NexusFlow-Timestamp.")


hmac_scheme = _hmac  # for routers that are HMAC-only (documents the scheme, enforcement is require_signature)


async def get_current_user(
    request: Request,
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer),
    signature: str | None = Depends(_hmac),
) -> Principal:
    if bearer is not None:
        claims = decode_jwt(bearer.credentials)
        try:
            roles = [Role(r) for r in claims.get("roles", [])]
        except ValueError:
            raise APIError(401, "INVALID_TOKEN", "access token carries an unknown role") from None
        if not roles or not set(roles) <= USER_ROLES:
            raise APIError(401, "INVALID_TOKEN", "access token must carry at least one user role")
        return Principal(subject=claims["sub"], name=claims.get("name"), kind="user", roles=roles,
                         effective_roles=_expand(roles), auth="jwt")
    if signature is not None or request.headers.get(TIMESTAMP_HEADER):
        await require_signature(request)  # raises 401 problem on a bad/stale signature
        return Principal(subject="n8n", kind="service", roles=[Role.SERVICE],
                         effective_roles=[Role.SERVICE], auth="hmac")
    raise APIError(401, "AUTHENTICATION_REQUIRED", "send a Bearer token or a signed service request")


def require_role(*roles: Role):
    """Dependency factory: the principal must hold at least one of `roles` (after hierarchy expansion)."""
    allowed = set(roles)

    async def guard(principal: Principal = Depends(get_current_user)) -> Principal:
        if not allowed & set(principal.effective_roles):
            raise APIError(403, "INSUFFICIENT_ROLE",
                           f"requires one of {sorted(r.value for r in allowed)}; "
                           f"{principal.subject} has {[r.value for r in principal.roles]}",
                           required_roles=sorted(r.value for r in allowed))
        return principal

    return guard


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.core.security")
    sub = parser.add_subparsers(dest="cmd", required=True)
    issue = sub.add_parser("issue-token", help="issue a local development JWT")
    issue.add_argument("employee_id")
    issue.add_argument("roles", nargs="+", choices=[r.value for r in USER_ROLES])
    issue.add_argument("--ttl-minutes", type=int, default=None)
    demo = sub.add_parser("issue-demo-token",
                          help="issue a read-only OPERATOR JWT for the console's 'Explore the demo' entry")
    demo.add_argument("--ttl-minutes", type=int, default=None)
    args = parser.parse_args(argv)
    if args.cmd == "issue-demo-token":
        # Not an employee: a visitor can browse every view but can't decide approvals (no MANAGER role,
        # and approvals are bound to the assigned approver's employee id)
        print(issue_jwt(DEMO_SUBJECT, [Role.OPERATOR.value], name="Demo visitor", ttl_minutes=args.ttl_minutes))
        return 0

    from app.core.db import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT first_name || ' ' || last_name AS name, role, status FROM employees WHERE employee_id = %s",
                    (args.employee_id,))
        emp = cur.fetchone()
    if emp is None or emp["status"] != "ACTIVE":
        print(f"{args.employee_id} is not an ACTIVE employee", file=sys.stderr)
        return 1
    print(issue_jwt(args.employee_id, args.roles, name=emp["name"], ttl_minutes=args.ttl_minutes))
    print(f"# {emp['name']} ({emp['role']}) as {' '.join(args.roles)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
