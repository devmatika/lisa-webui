"""HTTP API for Lisa Enterprise: first-run setup, login, password links, user administration.

Dispatched from ``api.routes`` for ``/api/enterprise/*`` and the ``/login`` + ``/set-password``
pages when enterprise mode is on. Admin endpoints check permissions here; Phase 2 adds the
route-wide permission gate for every other WebUI route.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select

from api.enterprise import db as edb
from api.enterprise import directory, knowledge, provisioner, sessions, telegram
from api.enterprise.directory import Actor, DirectoryError
from api.enterprise.gate import current_user
from api.enterprise.models import AuditEvent, Role, User
from api.enterprise.security import PasswordPolicyError

logger = logging.getLogger(__name__)
_PAGE = Path(__file__).with_name("login.html")
_ADMIN_PAGE = Path(__file__).with_name("admin.html")


def _ip(handler) -> str:
    from api.routes import _login_client_ip
    return _login_client_ip(handler)


def _cookie(handler, token: str | None) -> str:
    from api.auth import _is_secure_context
    secure = "; Secure" if _is_secure_context(handler) else ""
    if token is None:
        return f"{sessions.COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}"
    age = int(sessions.SESSION_TTL.total_seconds())
    return f"{sessions.COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{secure}"


def _html(handler, body: str, status: int = 200) -> bool:
    raw = body.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)
    return True


def _user_view(user: User) -> dict:
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "status": user.status,
            "roles": sorted(r.key for r in user.roles), "hermes_profile": user.hermes_profile,
            "has_password": bool(user.password_hash), "created_at": user.created_at.isoformat()}


def _provision(user_id: str) -> str:
    """Create/refresh the user's profile from their roles and store the link."""
    with edb.session() as db:
        user = directory.get_user(db, user_id)
        toolsets, permissions, email = directory.toolsets_of(user), directory.permissions_of(user), user.email
    name = provisioner.provision(user_id, toolsets, permissions, email)
    with edb.session() as db:
        directory.get_user(db, user_id).hermes_profile = name
    return name


# ── public ─────────────────────────────────────────────────────────────────────────────────────
def _setup_status(handler, _body):
    from api.helpers import j
    with edb.session() as db:
        return j(handler, {"needs_setup": directory.needs_setup(db)})


def _setup(handler, body):
    from api.helpers import bad, j
    ip = _ip(handler)
    try:
        with edb.session() as db:
            user = directory.first_run_setup(db, org_name=body.get("org_name", ""), email=body.get("email", ""),
                                             password=body.get("password", ""),
                                             display_name=body.get("display_name", ""), ip=ip)
            user_id = user.id
    except (DirectoryError, PasswordPolicyError) as exc:
        return bad(handler, str(exc), 400)
    _provision(user_id)
    token = sessions.start_session_for(user_id, ip)
    return j(handler, {"ok": True}, extra_headers={"Set-Cookie": _cookie(handler, token)})


def _login(handler, body):
    from api.helpers import j
    try:
        token, user = sessions.login(body.get("email", ""), body.get("password", ""), _ip(handler))
    except sessions.LoginError as exc:
        return j(handler, {"error": str(exc)}, status=exc.status)
    return j(handler, {"ok": True, "user": {"email": user.email, "roles": list(user.roles)}},
             extra_headers={"Set-Cookie": _cookie(handler, token)})


def _set_password(handler, body):
    from api.helpers import bad, j
    ip = _ip(handler)
    try:
        with edb.session() as db:
            user = directory.redeem_password_link(db, body.get("token", ""), body.get("password", ""), ip)
            user_id, has_profile = user.id, bool(user.hermes_profile)
    except (DirectoryError, PasswordPolicyError) as exc:
        return bad(handler, str(exc), 400)
    if not has_profile:
        _provision(user_id)
    return j(handler, {"ok": True}, extra_headers={"Set-Cookie": _cookie(handler, sessions.start_session_for(user_id, ip))})


# ── signed in ──────────────────────────────────────────────────────────────────────────────────
def _logout(handler, _body):
    from api.auth import parse_cookie
    from api.helpers import j
    sessions.logout(parse_cookie(handler))
    return j(handler, {"ok": True}, extra_headers={"Set-Cookie": _cookie(handler, None)})


def _me(handler, _body):
    from api.helpers import j
    u = current_user(handler)
    return j(handler, {"id": u.user_id, "email": u.email, "display_name": u.display_name,
                       "roles": list(u.roles), "permissions": sorted(u.permissions),
                       "hermes_profile": u.hermes_profile})


# ── administration ─────────────────────────────────────────────────────────────────────────────
def _actor(handler) -> Actor:
    return Actor(current_user(handler).user_id, _ip(handler))


def _users(handler, _body):
    from api.helpers import j
    with edb.session() as db:
        return j(handler, {"users": [_user_view(u) for u in db.scalars(select(User).order_by(User.email))]})


def _create_user(handler, body):
    from api.helpers import bad, j
    try:
        with edb.session() as db:
            user = directory.create_user(db, _actor(handler), email=body.get("email", ""),
                                         display_name=body.get("display_name", ""),
                                         role_keys=list(body.get("roles") or ["staff"]),
                                         department=body.get("department") or None)
            user_id = user.id
            link = directory.issue_password_link(db, _actor(handler), user_id)
    except DirectoryError as exc:
        return bad(handler, str(exc), 400)
    profile = _provision(user_id)
    return j(handler, {"ok": True, "user_id": user_id, "hermes_profile": profile,
                       "password_link": f"/set-password?token={link}"})


def _set_status(handler, body):
    from api.helpers import bad, j
    status = body.get("status", "")
    try:
        with edb.session() as db:
            user = directory.set_status(db, _actor(handler), body.get("user_id", ""), status)
            profile = user.hermes_profile
            revoked = sessions.revoke_all(db, user.id) if status == "disabled" else 0
            unlinked = telegram.unlink(db, _actor(handler), user.id) if status == "disabled" else False
    except DirectoryError as exc:
        return bad(handler, str(exc), 400)
    if profile:  # park first: it cuts every channel at once, the route rewrite follows
        (provisioner.park if status == "disabled" else provisioner.unpark)(profile)
    if unlinked:
        with edb.session() as db:
            telegram.apply(db)
    return j(handler, {"ok": True, "sessions_revoked": revoked, "telegram_unlinked": unlinked})


def _set_roles(handler, body):
    from api.helpers import bad, j
    try:
        with edb.session() as db:
            user = directory.set_roles(db, _actor(handler), body.get("user_id", ""), list(body.get("roles") or []))
            user_id, toolsets = user.id, directory.toolsets_of(user)
    except DirectoryError as exc:
        return bad(handler, str(exc), 400)
    _provision(user_id)  # full re-provision: integration grants follow the new roles exactly
    return j(handler, {"ok": True, "toolsets": toolsets})


def _password_link(handler, body):
    from api.helpers import bad, j
    try:
        with edb.session() as db:
            token = directory.issue_password_link(db, _actor(handler), body.get("user_id", ""))
    except DirectoryError as exc:
        return bad(handler, str(exc), 400)
    return j(handler, {"ok": True, "password_link": f"/set-password?token={token}"})


def _provision_route(handler, body):
    from api.helpers import bad, j
    try:
        return j(handler, {"ok": True, "hermes_profile": _provision(body.get("user_id", ""))})
    except DirectoryError as exc:
        return bad(handler, str(exc), 400)


def _roles(handler, _body):
    from api.helpers import j
    with edb.session() as db:
        roles = db.scalars(select(Role).order_by(Role.key))
        return j(handler, {"roles": [{"key": r.key, "name": r.name, "builtin": r.builtin,
                                      "permissions": sorted(p.permission for p in r.permissions),
                                      "toolsets": list(r.template.toolsets) if r.template else []} for r in roles],
                           "catalog": directory.PERMISSIONS, "toolsets": list(directory.SAFE_TOOLSETS)})


def _audit(handler, _body):
    from api.helpers import j
    with edb.session() as db:
        rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200))
        return j(handler, {"events": [{"at": e.at.isoformat(), "actor_id": e.actor_id, "action": e.action,
                                       "target": e.target, "detail": e.detail, "ip": e.ip} for e in rows]})


def _save_role(handler, body):
    from api.helpers import bad, j
    try:
        with edb.session() as db:
            role = directory.save_role(db, _actor(handler), key=body.get("key", ""), name=body.get("name", ""),
                                       permissions=list(body.get("permissions") or []),
                                       toolsets=list(body.get("toolsets") or []))
            affected = [u.id for u in directory.users_with_role(db, role.key) if u.hermes_profile]
    except DirectoryError as exc:
        return bad(handler, str(exc), 400)
    for user_id in affected:  # role edits reach every holder's profile, integrations included
        _provision(user_id)
    return j(handler, {"ok": True, "profiles_updated": len(affected)})


def _delete_role(handler, body):
    from api.helpers import bad, j
    try:
        with edb.session() as db:
            directory.delete_role(db, _actor(handler), body.get("key", ""))
    except DirectoryError as exc:
        return bad(handler, str(exc), 400)
    return j(handler, {"ok": True})


# ── Telegram ───────────────────────────────────────────────────────────────────────────────────
def _link_view(row) -> dict:
    return {"linked": row is not None, "telegram_user_id": row.telegram_user_id if row else None,
            "telegram_name": row.telegram_name if row else None,
            "linked_at": row.linked_at.isoformat() if row else None}


def _telegram_status(handler, _body):
    from api.helpers import j
    with edb.session() as db:
        return j(handler, _link_view(telegram.active_link(db, current_user(handler).user_id)))


def _telegram_link(handler, body):
    from api.helpers import bad, j
    try:
        with edb.session() as db:
            row = telegram.link(db, _actor(handler), current_user(handler).user_id, str(body.get("code", "")))
            view = _link_view(row)
    except DirectoryError as exc:
        return bad(handler, str(exc), 400)
    with edb.session() as db:
        applied = telegram.apply(db)
    return j(handler, {"ok": True, **view, "gateway_restarting": bool(applied.get("routes_changed"))})


def _telegram_unlink(handler, _body):
    from api.helpers import j
    with edb.session() as db:
        unlinked = telegram.unlink(db, _actor(handler), current_user(handler).user_id)
    if unlinked:
        with edb.session() as db:
            telegram.apply(db)
    return j(handler, {"ok": True, "unlinked": unlinked})


# ── Knowledge ──────────────────────────────────────────────────────────────────────────────────
def _query(handler, name: str) -> str:
    from urllib.parse import parse_qs
    return (parse_qs(handler._lisa_parsed.query).get(name) or [""])[0]


def _me_user(db, handler):
    return directory.get_user(db, current_user(handler).user_id)


def _knowledge_call(handler, fn):
    from api.helpers import bad, j
    try:
        with edb.session() as db:
            return j(handler, fn(db))
    except DirectoryError as exc:
        return bad(handler, str(exc), 404 if "not found" in str(exc).lower() else 403)


def _k_search(handler, _body):
    return _knowledge_call(handler, lambda db: knowledge.search(db, _me_user(db, handler), _query(handler, "q")))


def _k_doc(handler, _body):
    return _knowledge_call(handler, lambda db: knowledge.read(db, _me_user(db, handler), _query(handler, "path")))


def _k_areas(handler, _body):
    def run(db):
        me = _me_user(db, handler)
        levels = knowledge.access_for(db, me).levels
        out = {"mine": {k: {1: "read", 2: "contribute", 3: "approve"}[v] for k, v in levels.items() if v}}
        if current_user(handler).can("knowledge.manage"):
            out["all"] = knowledge.areas_view(db)
        return out
    return _knowledge_call(handler, run)


def _k_save_area(handler, body):
    return _knowledge_call(handler, lambda db: {"ok": True, "key": knowledge.save_area(
        db, _actor(handler), body.get("key", ""), body.get("name", ""), list(body.get("grants") or [])).key})


def _k_submit(handler, body):
    return _knowledge_call(handler, lambda db: {"ok": True, "draft_id": knowledge.submit_draft(
        db, _actor(handler), _me_user(db, handler), body.get("area", ""), body.get("title", ""),
        body.get("body", "")).id})


def _k_pending(handler, _body):
    return _knowledge_call(handler, lambda db: {"drafts": [
        {"id": d.id, "area": d.area_key, "title": d.title, "body": d.body, "author_id": d.author_id,
         "created_at": d.created_at.isoformat()} for d in knowledge.pending_for(db, _me_user(db, handler))]})


def _k_decide(handler, body):
    def run(db):
        d = knowledge.decide(db, _actor(handler), _me_user(db, handler), body.get("id", ""), bool(body.get("approve")))
        return {"ok": True, "status": d.status, "published_path": d.published_path}
    return _knowledge_call(handler, run)


def _k_gaps(handler, _body):
    from api.enterprise.models import KnowledgeGap
    return _knowledge_call(handler, lambda db: {"gaps": [
        {"question": g.question, "at": g.at.isoformat()}
        for g in db.scalars(select(KnowledgeGap).order_by(KnowledgeGap.id.desc()).limit(200))]})


def _agent_user(db, handler) -> User:
    """The Lisa user behind an agent call: the calling profile proves itself with its own API key."""
    import hmac
    profile = (handler.headers.get("X-Lisa-Profile") or "").strip()
    key = (handler.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    user = db.scalar(select(User).where(User.hermes_profile == profile)) if profile else None
    expected = provisioner._read_env(provisioner._home(profile) / ".env").get("API_SERVER_KEY", "") if user else ""
    if user is None or user.status != "active" or not expected or not hmac.compare_digest(key, expected):
        raise DirectoryError("Not allowed")
    return user


def _agent(fn):
    def run_route(handler, body):
        return _knowledge_call(handler, lambda db: fn(db, _agent_user(db, handler), body))
    return run_route


_a_search = _agent(lambda db, u, b: knowledge.search(db, u, str(b.get("query", ""))))
_a_read = _agent(lambda db, u, b: knowledge.read(db, u, str(b.get("path", ""))))
_a_draft = _agent(lambda db, u, b: {"ok": True, "draft_id": knowledge.submit_draft(
    db, Actor(u.id, "agent"), u, str(b.get("area", "")), str(b.get("title", "")), str(b.get("body", ""))).id,
    "note": "Submitted for approval; it becomes searchable once an approver publishes it."})


# (method, path) -> (handler, required permission or None for public / any signed-in user)
ROUTES = {
    ("GET", "/api/enterprise/setup/status"): (_setup_status, None),
    ("POST", "/api/enterprise/setup"): (_setup, None),
    ("POST", "/api/enterprise/login"): (_login, None),
    ("POST", "/api/enterprise/password"): (_set_password, None),
    ("POST", "/api/enterprise/logout"): (_logout, "signed-in"),
    ("GET", "/api/enterprise/me"): (_me, "signed-in"),
    ("GET", "/api/enterprise/users"): (_users, "org.users.manage"),
    ("POST", "/api/enterprise/users"): (_create_user, "org.users.manage"),
    ("POST", "/api/enterprise/users/status"): (_set_status, "org.users.manage"),
    ("POST", "/api/enterprise/users/roles"): (_set_roles, "org.users.manage"),
    ("POST", "/api/enterprise/users/password-link"): (_password_link, "org.users.manage"),
    ("POST", "/api/enterprise/users/provision"): (_provision_route, "org.users.manage"),
    ("GET", "/api/enterprise/roles"): (_roles, "org.users.manage"),
    ("POST", "/api/enterprise/roles"): (_save_role, "org.roles.manage"),
    ("POST", "/api/enterprise/roles/delete"): (_delete_role, "org.roles.manage"),
    ("GET", "/api/enterprise/audit"): (_audit, "org.audit.read"),
    ("GET", "/api/enterprise/telegram"): (_telegram_status, "chat.use"),
    ("POST", "/api/enterprise/telegram/link"): (_telegram_link, "chat.use"),
    ("POST", "/api/enterprise/telegram/unlink"): (_telegram_unlink, "chat.use"),
    ("GET", "/api/enterprise/knowledge/areas"): (_k_areas, "knowledge.read"),
    ("GET", "/api/enterprise/knowledge/search"): (_k_search, "knowledge.read"),
    ("GET", "/api/enterprise/knowledge/doc"): (_k_doc, "knowledge.read"),
    ("POST", "/api/enterprise/knowledge/drafts"): (_k_submit, "knowledge.contribute"),
    ("GET", "/api/enterprise/knowledge/drafts"): (_k_pending, "knowledge.approve"),
    ("POST", "/api/enterprise/knowledge/drafts/decide"): (_k_decide, "knowledge.approve"),
    ("POST", "/api/enterprise/knowledge/areas"): (_k_save_area, "knowledge.manage"),
    ("GET", "/api/enterprise/knowledge/gaps"): (_k_gaps, "knowledge.manage"),
    # Called by the org-authz plugin's knowledge tools; authenticated by the profile's own key.
    ("POST", "/api/enterprise/knowledge/agent/search"): (_a_search, None),
    ("POST", "/api/enterprise/knowledge/agent/read"): (_a_read, None),
    ("POST", "/api/enterprise/knowledge/agent/draft"): (_a_draft, None),
}


def handle(handler, parsed, method: str) -> bool:
    """Serve an enterprise route; False when the path is not one of ours."""
    from api.helpers import bad, read_body
    if method == "GET" and parsed.path in ("/login", "/set-password"):
        return _html(handler, _PAGE.read_text())
    if method == "GET" and parsed.path == "/lisa":
        import json as _json
        from api.auth import csrf_token_for_session, parse_cookie
        token = csrf_token_for_session(parse_cookie(handler)) or ""
        page = _ADMIN_PAGE.read_text().replace("<script>\n(function () {",
                                              f"<script>window.__csrf={_json.dumps(token)};\n(function () {{", 1)
        return _html(handler, page)
    handler._lisa_parsed = parsed
    entry = ROUTES.get((method, parsed.path))
    if entry is None:
        return False
    fn, need = entry
    user = current_user(handler)
    if need is not None and user is None:
        return bad(handler, "Authentication required", 401)
    if need not in (None, "signed-in") and not user.can(need):
        return bad(handler, "Not allowed", 403)
    if method == "POST" and need is not None:
        from api.routes import _check_csrf  # same-origin + session-bound token, like every WebUI write
        if not _check_csrf(handler):
            return bad(handler, "Cross-site request refused", 403)
    body = read_body(handler) if method == "POST" else {}
    return fn(handler, body) or True
