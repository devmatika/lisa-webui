"""Per-user integration credentials: each employee connects their OWN accounts.

Google: the organisation registers one OAuth client (``google_client_secret.json`` in the default
Hermes profile, as for single-operator Lisa). A user with ``integration.google`` connects their own
Google account (code flow + PKCE, offline access); the resulting ``authorized_user`` token is written
only into that user's profile (``google_token.json``, 0600), where the Google tools read it.

ClickUp: a user with ``integration.clickup`` stores their personal API token; it goes only into
their profile's ``.env`` (``CLICKUP_API_TOKEN``).

Removing the grant deletes these credentials on the next provisioning (``provisioner._build``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request

from api.enterprise import provisioner
from api.enterprise.directory import DirectoryError

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/documents",
]
GOOGLE_TOKEN = "google_token.json"
CLICKUP_KEY = "CLICKUP_API_TOKEN"
_PENDING: dict[str, tuple[str, str, float]] = {}  # state -> (user_id, code_verifier, expires)
_LOCK = threading.Lock()
_CLICKUP_TOKEN = re.compile(r"^pk_[A-Za-z0-9_]{10,200}$")


def _google_client() -> dict:
    """The organisation's OAuth client: a client JSON saved in the default profile (by IT or the
    setup wizard), or the client id + secret the wizard stored in its .env."""
    from api.customer_wizard import _find_google_client_secret
    root = provisioner.root_home()
    path = _find_google_client_secret(root)
    if path is not None:
        data = json.loads(path.read_text())
        client = data.get("web") or data.get("installed") or {}
    else:
        env = provisioner._read_env(root / ".env")
        client = {"client_id": env.get("GOOGLE_CLIENT_ID"), "client_secret": env.get("GOOGLE_CLIENT_SECRET")}
        if not client["client_id"]:
            raise DirectoryError("Google is not set up for this organisation yet (no OAuth client). Ask IT.")
    if not client.get("client_id") or not client.get("client_secret"):
        raise DirectoryError("The organisation's Google OAuth client is incomplete. Ask IT.")
    return client


def google_redirect_uri() -> str:
    base = os.environ.get("LISA_PUBLIC_URL", "").rstrip("/")
    if not base:
        raise DirectoryError("LISA_PUBLIC_URL is not configured; Google cannot send you back to Lisa.")
    return f"{base}/api/enterprise/google/callback"


def google_start(user_id: str) -> str:
    client = _google_client()
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    with _LOCK:
        now = time.time()
        for key in [k for k, v in _PENDING.items() if v[2] < now]:
            _PENDING.pop(key, None)
        _PENDING[state] = (user_id, verifier, now + 600)
    params = {"client_id": client["client_id"], "redirect_uri": google_redirect_uri(), "response_type": "code",
              "scope": " ".join(GOOGLE_SCOPES), "access_type": "offline", "prompt": "consent",
              "include_granted_scopes": "true", "state": state, "code_challenge": challenge,
              "code_challenge_method": "S256"}
    return client.get("auth_uri", "https://accounts.google.com/o/oauth2/auth") + "?" + urllib.parse.urlencode(params)


def google_finish(user_id: str, profile: str, state: str, code: str) -> None:
    """Exchange the code; the state must have been issued to this same signed-in user."""
    with _LOCK:
        pending = _PENDING.pop(state or "", None)
    if pending is None or pending[0] != user_id or pending[2] < time.time() or not code:
        raise DirectoryError("This Google sign-in was not started from your Lisa account, or it expired.")
    client = _google_client()
    form = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                   "redirect_uri": google_redirect_uri(), "client_id": client["client_id"],
                                   "client_secret": client["client_secret"], "code_verifier": pending[1]}).encode()
    token_uri = client.get("token_uri", "https://oauth2.googleapis.com/token")
    req = urllib.request.Request(token_uri, data=form, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        tokens = json.loads(r.read())
    if not tokens.get("refresh_token"):
        raise DirectoryError("Google did not grant offline access; remove Lisa in your Google account and retry.")
    granted = str(tokens.get("scope") or " ".join(GOOGLE_SCOPES)).split()
    payload = {"type": "authorized_user", "client_id": client["client_id"], "client_secret": client["client_secret"],
               "refresh_token": tokens["refresh_token"], "token": tokens.get("access_token", ""),
               "token_uri": token_uri, "scopes": granted}
    path = provisioner._home(profile) / GOOGLE_TOKEN
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def google_connected(profile: str) -> bool:
    return (provisioner._home(profile) / GOOGLE_TOKEN).is_file()


def google_disconnect(profile: str) -> None:
    (provisioner._home(profile) / GOOGLE_TOKEN).unlink(missing_ok=True)


def clickup_set(profile: str, token: str | None) -> None:
    """Store (or with None remove) the user's personal ClickUp token in their own profile."""
    if token is not None and not _CLICKUP_TOKEN.match(token.strip()):
        raise DirectoryError("That does not look like a ClickUp personal token (it starts with pk_).")
    path = provisioner._home(profile) / ".env"
    lines = [ln for ln in (path.read_text().splitlines() if path.is_file() else [])
             if not ln.startswith(f"{CLICKUP_KEY}=")]
    if token is not None:
        lines.append(f"{CLICKUP_KEY}={token.strip()}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def clickup_connected(profile: str) -> bool:
    return bool(provisioner._read_env(provisioner._home(profile) / ".env").get(CLICKUP_KEY))


def status(profile: str) -> dict:
    root_env = provisioner._read_env(provisioner.root_home() / ".env")
    return {"google": google_connected(profile), "clickup": clickup_connected(profile),
            "clickup_company": bool(root_env.get("CLICKUP_API_KEY") or root_env.get("CLICKUP_COMPANY_TOKEN")),
            "google_ready": _google_ready()}


def _google_ready() -> bool:
    try:
        _google_client()
    except DirectoryError:
        return False
    return True


__all__ = ["google_start", "google_finish", "google_disconnect", "clickup_set", "status"]
