"""Lisa Enterprise accounts: password reset by e-mail, SSO account mapping, rate limits, and each
user's own integration credentials (Google, ClickUp) that disappear when the grant does."""
from __future__ import annotations

import io
import json
import os
from datetime import timedelta

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")


@pytest.fixture()
def ent(tmp_path, monkeypatch, request):
    from api.enterprise import db as edb
    from api.enterprise import directory
    from api.enterprise.models import Base
    url = os.environ.get("LISA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'lisa.db'}"
    if url.startswith("postgresql"):
        request.getfixturevalue("allow_outbound_network")
    monkeypatch.setenv("LISA_ENTERPRISE", "1")
    monkeypatch.setenv("LISA_DATABASE_URL", url)
    edb.reset_for_tests()
    if url.startswith("postgresql"):
        with edb.engine().begin() as conn:
            Base.metadata.drop_all(conn)
            conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    edb.migrate()
    with edb.session() as db:
        directory.seed(db)
        boss = directory.first_run_setup(db, org_name="KSP", email="boss@ksp.example", password="boss password 1",
                                         display_name="Boss")
        anna = directory.create_user(db, directory.Actor(boss.id), email="anna@ksp.example", display_name="Anna",
                                     role_keys=["staff"])
        ids = {"boss": boss.id, "anna": anna.id}
    yield edb, ids
    edb.reset_for_tests()


class _H:
    """Minimal request handler for route functions."""
    def __init__(self, ip="10.0.0.1"):
        self.headers, self.client_address = {}, (ip, 1)
        self.status, self.sent, self.wfile = None, {}, io.BytesIO()

    def send_response(self, s):
        self.status = s

    def send_header(self, k, v):
        self.sent[k] = v

    def end_headers(self):
        pass

    def body(self):
        return json.loads(self.wfile.getvalue() or b"{}")


@pytest.fixture()
def mail(monkeypatch):
    from api.enterprise import mailer
    sent = []
    monkeypatch.setenv("LISA_SMTP_HOST", "smtp.example")
    monkeypatch.setenv("LISA_PUBLIC_URL", "https://lisa.ksp.example")
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject, body)))
    monkeypatch.setattr("api.routes._login_client_ip", lambda h: h.client_address[0])
    return sent


def test_password_reset_mails_a_one_hour_single_use_link_only_to_real_active_accounts(ent, mail):
    from api.enterprise import directory, routes
    edb, ids = ent
    for email in ("anna@ksp.example", "nobody@ksp.example"):
        h = _H()
        routes._forgot_password(h, {"email": email})
        assert h.status == 200 and "If this address" in h.body()["message"]  # same answer either way
    assert [m[0] for m in mail] == ["anna@ksp.example"]
    link = mail[0][2].split("https://lisa.ksp.example/set-password?token=")[1].split()[0]
    with edb.session() as db:
        directory.redeem_password_link(db, link, "new anna password")
    with pytest.raises(directory.DirectoryError):
        with edb.session() as db:
            directory.redeem_password_link(db, link, "again anna password")
    from api.enterprise.models import OneTimeToken, utcnow
    with edb.session() as db:
        latest = max(directory._aware(t.expires_at) for t in db.query(OneTimeToken).all())
    assert latest - utcnow() <= timedelta(hours=1)  # reset links live one hour, not the 48 h of invitations


def test_password_reset_is_rate_limited(ent, mail):
    from api.enterprise import routes
    for _ in range(6):
        routes._forgot_password(_H(), {"email": "anna@ksp.example"})
    assert len(mail) == 3


def test_disabled_accounts_get_no_reset_mail(ent, mail):
    from api.enterprise import directory, routes
    edb, ids = ent
    with edb.session() as db:
        directory.set_status(db, directory.Actor(ids["boss"]), ids["anna"], "disabled")
    routes._forgot_password(_H(), {"email": "anna@ksp.example"})
    assert mail == []


def test_sso_signs_in_only_existing_active_users_and_binds_the_subject(ent):
    from api.enterprise import directory, routes
    from api.enterprise.models import User
    edb, ids = ent
    claims = {"iss": "https://login.microsoftonline.com/t/v2.0", "sub": "abc", "email": "Anna@KSP.example"}
    with edb.session() as db:
        assert routes.oidc_user_for(db, claims, "1.1.1.1").id == ids["anna"]
    with edb.session() as db:
        assert db.get(User, ids["anna"]).oidc_subject == "https://login.microsoftonline.com/t/v2.0|abc"
        # the bound subject now wins even if the e-mail claim changes
        assert routes.oidc_user_for(db, {**claims, "email": "other@x.example"}, "1.1.1.1").id == ids["anna"]
        assert routes.oidc_user_for(db, {"iss": "i", "sub": "new", "email": "stranger@ksp.example"}, "1") is None
        assert routes.oidc_user_for(db, {"iss": "i", "sub": "n2", "email": "boss@ksp.example",
                                         "email_verified": False}, "1") is None
    with edb.session() as db:
        directory.set_status(db, directory.Actor(ids["boss"]), ids["anna"], "disabled")
    with edb.session() as db:
        assert routes.oidc_user_for(db, claims, "1.1.1.1") is None


def test_rate_limits_per_user_and_for_expensive_routes():
    from api.enterprise import gate
    gate._hits.clear()
    assert all(gate._within_limit("u1", "/api/chat/start", now=100.0) for _ in range(30))
    assert not gate._within_limit("u1", "/api/chat/start", now=100.0)
    assert gate._within_limit("u2", "/api/chat/start", now=100.0)          # per user
    assert gate._within_limit("u1", "/api/chat/start", now=161.0)          # window rolls
    gate._hits.clear()


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    from api.enterprise import provisioner
    root = tmp_path / ".hermes"
    for p in ("u-anna", "u-ben"):
        (root / "profiles" / p).mkdir(parents=True)
        (root / "profiles" / p / ".env").write_text(f"API_SERVER_KEY=key-{p}\n")
    (root / "google_client_secret.json").write_text(json.dumps({"web": {
        "client_id": "cid", "client_secret": "csecret", "auth_uri": "https://auth.example/o",
        "token_uri": "https://token.example/t"}}))
    monkeypatch.setattr(provisioner, "root_home", lambda: root)
    monkeypatch.setenv("LISA_PUBLIC_URL", "https://lisa.ksp.example")
    return root


def test_google_connect_binds_state_to_the_user_and_writes_only_their_profile(homes, monkeypatch):
    from urllib.parse import parse_qs, urlparse
    from api.enterprise import integrations
    calls = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        calls.append(parse_qs(req.data.decode()))
        return _Resp(json.dumps({"access_token": "at", "refresh_token": "rt", "scope": "s1 s2"}).encode())
    monkeypatch.setattr(integrations.urllib.request, "urlopen", fake_urlopen)
    url = integrations.google_start("anna-id")
    q = parse_qs(urlparse(url).query)
    assert q["redirect_uri"] == ["https://lisa.ksp.example/api/enterprise/google/callback"]
    assert q["access_type"] == ["offline"] and q["code_challenge_method"] == ["S256"]
    with pytest.raises(integrations.DirectoryError):  # someone else's session cannot use anna's state
        integrations.google_finish("ben-id", "u-ben", q["state"][0], "code")
    url = integrations.google_start("anna-id")
    state = parse_qs(urlparse(url).query)["state"][0]
    integrations.google_finish("anna-id", "u-anna", state, "code")
    token = json.loads((homes / "profiles" / "u-anna" / "google_token.json").read_text())
    assert token["refresh_token"] == "rt" and token["type"] == "authorized_user"
    assert calls[-1]["code_verifier"] and not (homes / "profiles" / "u-ben" / "google_token.json").exists()
    with pytest.raises(integrations.DirectoryError):  # a state works once
        integrations.google_finish("anna-id", "u-anna", state, "code")


def test_clickup_token_is_validated_and_stored_only_for_the_user(homes):
    from api.enterprise import integrations
    with pytest.raises(integrations.DirectoryError):
        integrations.clickup_set("u-anna", "not-a-token")
    integrations.clickup_set("u-anna", "pk_1234567890abcdef")
    assert integrations.status("u-anna") == {"google": False, "clickup": True}
    assert integrations.status("u-ben")["clickup"] is False
    integrations.clickup_set("u-anna", None)
    assert integrations.status("u-anna")["clickup"] is False


def test_removing_a_grant_deletes_the_users_own_credentials(homes, monkeypatch):
    from api.enterprise import integrations, provisioner
    monkeypatch.setattr(provisioner, "_install_plugins", lambda home, integ=(): None)
    monkeypatch.setattr(provisioner, "allowed_tools", lambda ts: [])
    monkeypatch.setattr(provisioner, "rescan", lambda: None)
    monkeypatch.setattr(provisioner, "_invalidate_webui_profile_cache", lambda: None)
    home = homes / "profiles" / "u-anna"
    (home / "google_token.json").write_text("{}")
    integrations.clickup_set("u-anna", "pk_1234567890abcdef")
    provisioner._build("u-anna", ["memory"], owner="a",
                       integrations=["integration.google", "integration.clickup"], email="anna@ksp.example")
    assert (home / "google_token.json").exists() and integrations.status("u-anna")["clickup"]  # kept while granted
    provisioner._build("u-anna", ["memory"], owner="a", integrations=[], email="anna@ksp.example")
    assert not (home / "google_token.json").exists() and not integrations.status("u-anna")["clickup"]
