"""Voice tickets: the WebUI tells lisa-pipecat which profile a voice session belongs to.

The browser only talks to the WebUI's ``/embed/voice/`` proxy. For a signed-in organisation user the
proxy adds ``X-Lisa-Voice-Ticket`` to the upstream request: a 60-second HMAC-SHA256-signed token
naming the user's profile and that profile's own gateway key. The browser never sees it (the proxy
forwards only an allowlist of client headers), and pipecat rejects offers without a valid ticket,
so a voice session can never pick another user's profile. ``LISA_VOICE_SECRET`` is shared with
pipecat; without it, enterprise voice is unavailable (fail closed).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

HEADER = "X-Lisa-Voice-Ticket"
TTL_SECONDS = 60


def _secret() -> bytes:
    secret = os.environ.get("LISA_VOICE_SECRET", "").strip()
    if len(secret) < 32:
        raise RuntimeError("LISA_VOICE_SECRET (32+ characters) is required for voice in Lisa Enterprise")
    return secret.encode()


def mint(profile: str, api_key: str, user_id: str, *, now: float | None = None) -> str:
    payload = {"p": profile, "k": api_key, "u": user_id, "exp": int((time.time() if now is None else now) + TTL_SECONDS)}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify(ticket: str, *, now: float | None = None) -> dict | None:
    """The reference check pipecat mirrors (lisa-pipecat/app/lisa_ticket.py)."""
    try:
        body, sig = (ticket or "").rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()):
        return None
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if int(payload.get("exp", 0)) < (time.time() if now is None else now):
        return None
    return payload


def ticket_for(user) -> str:
    """Ticket for the signed-in user's own profile (gate.current_user)."""
    from api.enterprise.provisioner import _home, _read_env
    key = _read_env(_home(user.hermes_profile) / ".env").get("API_SERVER_KEY", "")
    if not key:
        raise RuntimeError(f"profile {user.hermes_profile} has no API key")
    return mint(user.hermes_profile, key, user.user_id)
