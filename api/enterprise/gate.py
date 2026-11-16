"""Enterprise request gate: who is this request, and which Hermes profile may it touch.

Runs instead of the single-operator auth check when ``LISA_ENTERPRISE`` is on. A request either
matches a public path, or carries a valid ``lisa_session`` cookie; the request profile is then
forced to the user's own profile (``users.hermes_profile``), whatever profile cookie the browser
sent. Database trouble answers 503: never let a request through unverified.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)
ORG_SETUP_PREFIX = "/api/customer-wizard/"

PUBLIC_EXACT = frozenset({
    "/login", "/health", "/favicon.ico", "/sw.js", "/manifest.json", "/manifest.webmanifest",
    "/session/manifest.json", "/session/manifest.webmanifest", "/api/csp-report",
    "/api/enterprise/login", "/api/enterprise/setup", "/api/enterprise/setup/status",
    "/api/enterprise/password", "/api/enterprise/password/forgot", "/set-password", "/api/auth/status",
    "/api/auth/oidc/start", "/api/auth/oidc/callback",
})
PUBLIC_PREFIXES = ("/static/", "/session/static/", "/share/")


# Per-user request limits (per rolling minute). Expensive endpoints get their own, tighter budget.
GENERAL_PER_MINUTE = 600
EXPENSIVE = {"/api/chat/start": 30, "/api/upload": 30, "/api/transcribe": 30,
             "/api/enterprise/knowledge/search": 60}
_hits: dict[tuple[str, str], deque] = {}
_denied_seen: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


def _within_limit(user_id: str, path: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    checks = [("*", GENERAL_PER_MINUTE)] + ([(path, EXPENSIVE[path])] if path in EXPENSIVE else [])
    with _lock:
        for bucket, limit in checks:
            q = _hits.setdefault((user_id, bucket), deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                return False
        for bucket, _limit in checks:
            _hits[(user_id, bucket)].append(now)
    return True


def _audit_denied(user, method: str, path: str) -> None:
    """One audit row per user+route per 5 minutes, so a looping client cannot flood the log."""
    key = (user.user_id, path)
    now = time.monotonic()
    with _lock:
        if now - _denied_seen.get(key, -1e9) < 300:
            return
        _denied_seen[key] = now
    try:
        from api.enterprise import db as edb
        from api.enterprise.directory import Actor, audit
        with edb.session() as db:
            audit(db, Actor(user.user_id), "authz.denied", path, method=method)
    except Exception:
        logger.warning("Could not audit a denied request", exc_info=True)


def is_public(path: str) -> bool:
    if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES):
        return True
    if path.startswith(("/api/enterprise/knowledge/agent/", "/api/enterprise/mail/agent/")):
        return True  # no browser session: each call proves its profile with that profile's API key
    # Read-only shared transcripts by token; creating/revoking a share needs a session.
    return path.startswith("/api/share/") and path not in ("/api/share/create", "/api/share/revoke")


def _send(handler, status: int, payload: dict, headers: dict | None = None) -> bool:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)
    return False


def _redirect_login(handler) -> bool:
    handler.send_response(302)
    handler.send_header("Location", "/login")
    handler.send_header("Content-Length", "0")
    handler.end_headers()
    return False


def current_user(handler):
    """The verified ``SessionUser`` of this request, or None (set by :func:`check`)."""
    return getattr(handler, "_lisa_user", None)


def check(handler, parsed) -> bool:
    from api.enterprise import sessions
    from api.auth import read_cookie

    handler._lisa_user = None
    if is_public(parsed.path):
        return True
    try:
        user = sessions.verify(read_cookie(handler, sessions.COOKIE_NAME))
    except Exception:
        logger.exception("Enterprise session check failed; denying")
        return _send(handler, 503, {"error": "Authentication service unavailable"})
    if user is None:
        if parsed.path.startswith(("/api/", "/embed/voice-api/")):
            return _send(handler, 401, {"error": "Authentication required"})
        return _redirect_login(handler)
    if not user.hermes_profile:
        return _send(handler, 403, {"error": "Your account has no assistant profile yet; ask an administrator"})
    from api.enterprise.policy import allowed
    method = getattr(handler, "command", "GET")
    if not allowed(user, method, parsed.path):
        _audit_denied(user, method, parsed.path)
        return _send(handler, 403, {"error": "You do not have access to this feature"})
    if not _within_limit(user.user_id, parsed.path):
        return _send(handler, 429, {"error": "Too many requests; slow down a little"}, {"Retry-After": "30"})
    from api.profiles import bind_request_profile
    # The setup wizard configures the organisation: the default profile every assistant is built
    # from (model, provider keys, Telegram bot, Google / Microsoft app credentials). Only
    # operator.setup reaches these routes; everything else stays on the user's own profile.
    bind_request_profile("default" if parsed.path.startswith(ORG_SETUP_PREFIX) else user.hermes_profile)
    handler._lisa_user = user
    return True
