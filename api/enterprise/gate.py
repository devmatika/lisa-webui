"""Enterprise request gate: who is this request, and which Hermes profile may it touch.

Runs instead of the single-operator auth check when ``LISA_ENTERPRISE`` is on. A request either
matches a public path, or carries a valid ``lisa_session`` cookie; the request profile is then
forced to the user's own profile (``users.hermes_profile``), whatever profile cookie the browser
sent. Database trouble answers 503: never let a request through unverified.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

PUBLIC_EXACT = frozenset({
    "/login", "/health", "/favicon.ico", "/sw.js", "/manifest.json", "/manifest.webmanifest",
    "/session/manifest.json", "/session/manifest.webmanifest", "/api/csp-report",
    "/api/enterprise/login", "/api/enterprise/setup", "/api/enterprise/setup/status",
    "/api/enterprise/password", "/set-password", "/api/auth/status",
})
PUBLIC_PREFIXES = ("/static/", "/session/static/", "/share/")


def is_public(path: str) -> bool:
    if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES):
        return True
    if path.startswith("/api/enterprise/knowledge/agent/"):
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
        if parsed.path.startswith("/api/"):
            return _send(handler, 401, {"error": "Authentication required"})
        return _redirect_login(handler)
    if not user.hermes_profile:
        return _send(handler, 403, {"error": "Your account has no assistant profile yet; ask an administrator"})
    from api.enterprise.policy import allowed
    if not allowed(user, getattr(handler, "command", "GET"), parsed.path):
        return _send(handler, 403, {"error": "You do not have access to this feature"})
    from api.profiles import bind_request_profile
    bind_request_profile(user.hermes_profile)
    handler._lisa_user = user
    return True
