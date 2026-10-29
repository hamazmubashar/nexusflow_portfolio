"""Signed, single-use HITL approval tokens.

Format: "v1.<base64url(json claims)>.<base64url(HMAC-SHA256(key, "v1." + claims_b64))>"
Claims bind the token to one approval, one request and one approver, with an expiry, a random
nonce and a kind:
  * link    - issued once when the approval opens and delivered via n8n. Only sha256(token) is
              stored (approval_requests.token_hash); the hash lookup proves it is the current one.
  * session - minted on demand (<= 10 min) for the assigned approver after JWT sign-in
              (POST /approvals/{id}/decision-token), never stored; bound by approval_id + approver.
Either way the signature proves we issued it, and the PENDING/expiry check in the UPDATE makes
every decision single-use.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import Settings, get_settings

VERSION = "v1"


class TokenError(Exception):
    def __init__(self, code: str, message: str, claims: "ApprovalClaims | None" = None):
        super().__init__(message)
        # claims are only attached once the signature has been verified (TOKEN_EXPIRED)
        self.code, self.message, self.claims = code, message, claims


@dataclass(frozen=True)
class ApprovalClaims:
    approval_id: str
    request_id: str
    approver_id: str
    expires_at: datetime
    nonce: str
    kind: str = "link"   # "link": sent once via n8n, hash stored; "session": minted for a signed-in approver


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def signing_key(settings: Settings | None = None) -> bytes:
    s = settings or get_settings()
    if s.approval_token_secret:
        return s.approval_token_secret.encode()
    if not s.webhook_signing_secret:
        raise RuntimeError("Set APPROVAL_TOKEN_SECRET or WEBHOOK_SIGNING_SECRET to issue approval tokens")
    # Domain-separated derivation: a webhook signature can never double as an approval token
    return hmac.new(s.webhook_signing_secret.encode(), b"nexusflow/approval-token/v1", hashlib.sha256).digest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(approval_id: str, request_id: str, approver_id: str, expires_at: datetime,
          key: bytes | None = None, kind: str = "link") -> str:
    if kind not in ("link", "session"):
        raise ValueError(f"unknown token kind {kind!r}")
    claims = {"aid": approval_id, "rid": request_id, "apr": approver_id,
              "exp": int(expires_at.timestamp()), "n": secrets.token_urlsafe(12), "k": kind}
    body = f"{VERSION}.{_b64(json.dumps(claims, separators=(',', ':')).encode())}"
    sig = hmac.new(key or signing_key(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def verify(token: str, key: bytes | None = None, now: datetime | None = None) -> ApprovalClaims:
    """Check format + signature + expiry. Raises TokenError; never returns unverified claims."""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise TokenError("TOKEN_MALFORMED", "approval token is malformed")
    body = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(key or signing_key(), body.encode(), hashlib.sha256).digest()
    try:
        given = _unb64(parts[2])
        claims = json.loads(_unb64(parts[1]))
    except (ValueError, json.JSONDecodeError):
        raise TokenError("TOKEN_MALFORMED", "approval token is malformed") from None
    if not hmac.compare_digest(expected, given):
        raise TokenError("TOKEN_INVALID_SIGNATURE", "approval token signature is invalid")
    verified = ApprovalClaims(approval_id=claims["aid"], request_id=claims["rid"], approver_id=claims["apr"],
                              expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc),
                              nonce=claims["n"], kind=claims.get("k", "link"))
    if (now or datetime.now(timezone.utc)) >= verified.expires_at:
        raise TokenError("TOKEN_EXPIRED", f"approval token expired at {verified.expires_at.isoformat()}", verified)
    return verified
