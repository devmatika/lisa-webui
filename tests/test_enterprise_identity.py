"""Lisa Enterprise identity: first-run setup, login, revocation, password links, roles, request gate.

Runs against ``LISA_TEST_DATABASE_URL`` when set (Postgres in the Docker test stack), else a
temporary SQLite file. Behaviour contracts only: who may sign in, what a session proves, and that
the gate binds every request to the signed-in user's own profile.
"""
from __future__ import annotations

import io
import os
from datetime import timedelta
from urllib.parse import urlparse

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")


@pytest.fixture()
def ent(tmp_path, monkeypatch, request):
    from api.enterprise import db as edb
    from api.enterprise.directory import seed
    from api.enterprise.models import Base

    url = os.environ.get("LISA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'lisa.db'}"
    if url.startswith("postgresql"):
        request.getfixturevalue("allow_outbound_network")  # the suite blocks sockets by default
    monkeypatch.setenv("LISA_ENTERPRISE", "1")
    monkeypatch.setenv("LISA_DATABASE_URL", url)
    edb.reset_for_tests()
    if url.startswith("postgresql"):
        with edb.engine().begin() as conn:  # clean slate per test on the shared test database
            Base.metadata.drop_all(conn)
            conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    edb.migrate()
    with edb.session() as db:
        seed(db)
    yield edb
    edb.reset_for_tests()


def _setup_admin(edb, email="boss@example.com", password="correct horse 1"):
    from api.enterprise import directory
    with edb.session() as db:
        return directory.first_run_setup(db, org_name="KSP", email=email, password=password,
                                         display_name="Boss").id


def _staff(edb, admin_id, email="anna@example.com", roles=("staff",), password="anna password 1"):
    from api.enterprise import directory
    with edb.session() as db:
        uid = directory.create_user(db, directory.Actor(admin_id), email=email, display_name="Anna",
                                    role_keys=list(roles)).id
        token = directory.issue_password_link(db, directory.Actor(admin_id), uid)
    with edb.session() as db:
        directory.redeem_password_link(db, token, password)
    return uid


def test_first_run_setup_creates_one_admin_and_then_closes(ent):
    from api.enterprise import directory
    _setup_admin(ent)
    with ent.session() as db:
        assert not directory.needs_setup(db)
    with pytest.raises(directory.DirectoryError):
        with ent.session() as db:
            directory.first_run_setup(db, org_name="X", email="evil@example.com", password="x" * 12,
                                      display_name="Evil")


def test_login_session_carries_roles_and_permissions(ent):
    from api.enterprise import sessions
    _setup_admin(ent)
    token, user = sessions.login("BOSS@example.com ", "correct horse 1", "10.0.0.1")
    again = sessions.verify(token)
    assert again is not None and again.user_id == user.user_id
    assert {"admin", "operator"} <= set(again.roles)
    assert again.can("org.users.manage") and again.can("operator.terminal")


def test_wrong_password_and_unknown_user_are_refused_alike(ent):
    from api.enterprise import sessions
    _setup_admin(ent)
    for email, pw in (("boss@example.com", "wrong password"), ("nobody@example.com", "whatever 123")):
        with pytest.raises(sessions.LoginError) as err:
            sessions.login(email, pw, "10.0.0.2")
        assert err.value.status == 401 and str(err.value) == "Invalid e-mail or password"


def test_repeated_failures_throttle_the_account(ent):
    from api.enterprise import sessions
    _setup_admin(ent)
    for i in range(sessions.MAX_FAILURES_PER_EMAIL):
        with pytest.raises(sessions.LoginError):
            sessions.login("boss@example.com", f"wrong {i} password", f"10.1.0.{i}")
    with pytest.raises(sessions.LoginError) as err:  # even the right password, from a new address
        sessions.login("boss@example.com", "correct horse 1", "10.9.9.9")
    assert err.value.status == 429


def test_disabling_a_user_kills_existing_sessions_and_blocks_login(ent):
    from api.enterprise import directory, sessions
    admin = _setup_admin(ent)
    anna = _staff(ent, admin)
    token, _ = sessions.login("anna@example.com", "anna password 1", "10.0.0.3")
    with ent.session() as db:
        directory.set_status(db, directory.Actor(admin), anna, "disabled")
        sessions.revoke_all(db, anna)
    assert sessions.verify(token) is None
    with pytest.raises(sessions.LoginError):
        sessions.login("anna@example.com", "anna password 1", "10.0.0.3")


def test_disabled_user_rejected_even_with_unrevoked_session(ent):
    """Status is re-read on every request: disabling alone is enough, revocation is belt and braces."""
    from api.enterprise import directory, sessions
    admin = _setup_admin(ent)
    anna = _staff(ent, admin)
    token, _ = sessions.login("anna@example.com", "anna password 1", "10.0.0.4")
    with ent.session() as db:
        directory.set_status(db, directory.Actor(admin), anna, "disabled")
    assert sessions.verify(token) is None


def test_password_link_is_single_use_and_expires(ent):
    from api.enterprise import directory
    from api.enterprise.models import OneTimeToken, utcnow
    admin = _setup_admin(ent)
    with ent.session() as db:
        uid = directory.create_user(db, directory.Actor(admin), email="ben@example.com", display_name="Ben",
                                    role_keys=["staff"]).id
        first = directory.issue_password_link(db, directory.Actor(admin), uid)
        second = directory.issue_password_link(db, directory.Actor(admin), uid)
    with pytest.raises(directory.DirectoryError):  # a newer link voids the older one
        with ent.session() as db:
            directory.redeem_password_link(db, first, "ben password 1")
    with ent.session() as db:
        directory.redeem_password_link(db, second, "ben password 1")
    with pytest.raises(directory.DirectoryError):
        with ent.session() as db:
            directory.redeem_password_link(db, second, "ben password 2")
    with ent.session() as db:
        third = directory.issue_password_link(db, directory.Actor(admin), uid)
        from api.enterprise.security import token_hash
        db.get(OneTimeToken, token_hash(third)).expires_at = utcnow() - timedelta(minutes=1)
    with pytest.raises(directory.DirectoryError):
        with ent.session() as db:
            directory.redeem_password_link(db, third, "ben password 3")


def test_admin_role_manages_people_but_is_not_operator(ent):
    from api.enterprise import sessions
    admin = _setup_admin(ent)
    _staff(ent, admin, email="hr@example.com", roles=("admin",), password="hr password 1")
    _, hr = sessions.login("hr@example.com", "hr password 1", "10.0.0.5")
    assert hr.can("org.users.manage")
    assert not any(p.startswith("operator.") for p in hr.permissions)


def test_staff_has_no_admin_or_operator_permission(ent):
    from api.enterprise import sessions
    admin = _setup_admin(ent)
    _staff(ent, admin)
    _, anna = sessions.login("anna@example.com", "anna password 1", "10.0.0.6")
    assert anna.can("chat.use")
    assert not any(p.startswith(("org.", "operator.")) for p in anna.permissions)


def test_no_builtin_role_template_grants_host_access_tools(ent):
    """Phase 0: a profile is not an OS boundary, so no built-in role may reach files, shell, code or browser."""
    from sqlalchemy import select
    from api.enterprise.models import Role
    dangerous = {"terminal", "file", "code_execution", "browser", "computer_use", "process"}
    with ent.session() as db:
        for role in db.scalars(select(Role)):
            assert not dangerous & set(role.template.toolsets), role.key


def test_seed_keeps_admin_edits_to_builtin_roles(ent):
    from sqlalchemy import select
    from api.enterprise.directory import seed
    from api.enterprise.models import Role, RolePermission
    with ent.session() as db:
        staff = db.scalar(select(Role).where(Role.key == "staff"))
        staff.permissions = [RolePermission(permission="chat.use")]
    with ent.session() as db:
        seed(db)
    with ent.session() as db:
        staff = db.scalar(select(Role).where(Role.key == "staff"))
        assert [p.permission for p in staff.permissions] == ["chat.use"]


def test_render_config_sets_every_surface_and_media_isolation():
    from api.enterprise.provisioner import SURFACES, render_config
    cfg = render_config({"model": {"default": "m"}, "platforms": {"telegram": {"token": "x"}},
                         "command_allowlist": ["rm -rf"]}, ["memory", "web"])
    assert cfg["model"] == {"default": "m"}
    assert "platforms" not in cfg and "command_allowlist" not in cfg  # never inherit channels or approvals
    assert all(cfg["platform_toolsets"][s] == ["memory", "web"] for s in SURFACES)
    assert cfg["gateway"]["profile_isolation"] is True and cfg["gateway"]["trust_recent_files"] is False
    assert "org-authz" in cfg["plugins"]["enabled"]
    assert "webui_gateway_base_url" not in cfg  # only set when the profile name is known
    named = render_config({}, ["memory"], "u-anna")
    assert named["webui_gateway_base_url"].endswith("/p/u-anna")


def test_only_model_provider_keys_are_copied_into_user_profiles():
    from api.enterprise.provisioner import PROVIDER_KEY
    assert PROVIDER_KEY.match("OPENROUTER_API_KEY") and PROVIDER_KEY.match("ANTHROPIC_API_KEY")
    for secret in ("TELEGRAM_BOT_TOKEN", "API_SERVER_KEY", "CLICKUP_API_TOKEN", "GOOGLE_CLIENT_SECRET",
                   "HIMALAYA_PASSWORD", "TELEGRAM_ALLOWED_USERS"):
        assert not PROVIDER_KEY.match(secret)


# ── request gate ──────────────────────────────────────────────────────────────────────────────
class _Handler:
    def __init__(self, cookie: str = ""):
        self.headers = {"Cookie": cookie} if cookie else {}
        self.status, self.sent_headers, self.wfile = None, {}, io.BytesIO()
        self.client_address = ("10.0.0.9", 1)
        self.command = "GET"

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.sent_headers[k] = v

    def end_headers(self):
        pass


def _gate(path: str, cookie: str = ""):
    from api.enterprise.gate import check
    h = _Handler(cookie)
    return check(h, urlparse(path)), h


def test_gate_requires_a_session_for_api_and_pages(ent):
    ok, h = _gate("/api/sessions")
    assert not ok and h.status == 401
    ok, h = _gate("/")
    assert not ok and h.status == 302 and h.sent_headers["Location"] == "/login"
    ok, h = _gate("/api/sessions", "lisa_session=forged-token")
    assert not ok and h.status == 401


def test_gate_lets_public_paths_through(ent):
    for path in ("/login", "/api/enterprise/login", "/api/enterprise/setup/status", "/static/app.js", "/health"):
        assert _gate(path)[0], path


def test_gate_binds_request_to_the_users_own_profile(ent, monkeypatch):
    from api.enterprise import sessions
    from api.enterprise.models import User
    bound = []
    monkeypatch.setattr("api.profiles.bind_request_profile", bound.append)
    admin = _setup_admin(ent)
    anna = _staff(ent, admin)
    with ent.session() as db:
        db.get(User, anna).hermes_profile = "u-anna"
    token, _ = sessions.login("anna@example.com", "anna password 1", "10.0.0.7")
    ok, h = _gate("/api/sessions", f"hermes_profile=u-boss; lisa_session={token}")
    assert ok and bound == ["u-anna"] and h._lisa_user.user_id == anna


def test_gate_refuses_user_without_profile(ent):
    from api.enterprise import sessions
    admin = _setup_admin(ent)
    _staff(ent, admin)
    token, _ = sessions.login("anna@example.com", "anna password 1", "10.0.0.8")
    ok, h = _gate("/api/sessions", f"lisa_session={token}")
    assert not ok and h.status == 403


def test_gate_fails_closed_when_database_is_down(ent, monkeypatch):
    from api.enterprise import sessions

    def boom(_token):
        raise ConnectionError("db down")
    monkeypatch.setattr(sessions, "verify", boom)
    ok, h = _gate("/api/sessions", "lisa_session=anything")
    assert not ok and h.status == 503


def test_enterprise_off_leaves_single_operator_auth_untouched(monkeypatch):
    monkeypatch.delenv("LISA_ENTERPRISE", raising=False)
    from api import auth
    assert auth._enterprise_on() is False
