"""Lisa Enterprise organisation setup: the setup wizard edits the organisation template (default
profile) and refreshes every assistant; the WebUI's own Sign Out really signs out; the wizard's
Microsoft / Google / provider settings reach the users who are granted them."""
from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")


@pytest.fixture()
def ent(tmp_path, monkeypatch, request):
    from api.enterprise import db as edb
    from api.enterprise import directory, provisioner
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
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setattr(provisioner, "root_home", lambda: root)
    with edb.session() as db:
        directory.seed(db)
        boss = directory.first_run_setup(db, org_name="KSP", email="boss@ksp.example", password="boss password 1",
                                         display_name="Boss")
        anna = directory.create_user(db, directory.Actor(boss.id), email="anna@ksp.example", display_name="Anna",
                                     role_keys=["staff"])
        boss.hermes_profile, anna.hermes_profile = "u-boss", "u-anna"
        ids = {"boss": boss.id, "anna": anna.id}
    yield edb, ids, root
    edb.reset_for_tests()


def test_sign_out_from_the_chat_menu_ends_the_enterprise_session(ent):
    from api.auth import _clear_auth_cookie_header, invalidate_session
    from api.enterprise import sessions
    _, ids, _ = ent
    token = sessions.start_session_for(ids["anna"], "10.0.0.1")
    assert sessions.verify(token) is not None
    invalidate_session(token)
    assert sessions.verify(token) is None
    assert _clear_auth_cookie_header().startswith(sessions.COOKIE_NAME + "=")


@pytest.mark.parametrize("path,profile", [("/api/customer-wizard/status", "default"),
                                          ("/api/customer-wizard/apply", "default"),
                                          ("/api/sessions", "u-boss")])
def test_setup_wizard_edits_the_organisation_template_not_the_operators_assistant(monkeypatch, path, profile):
    from urllib.parse import urlparse
    from api import profiles
    from api.enterprise import gate, sessions
    monkeypatch.setenv("LISA_ENTERPRISE", "1")
    operator = SimpleNamespace(user_id="boss", hermes_profile="u-boss", can=lambda p: True)
    monkeypatch.setattr(sessions, "verify", lambda token: operator)
    monkeypatch.setattr("api.auth.read_cookie", lambda handler, name: "token")
    handler = SimpleNamespace(command="GET", headers={}, client_address=("10.0.0.1", 1))
    try:
        assert gate.check(handler, urlparse(path)) is True
        assert profiles.get_active_profile_name() == profile
    finally:
        profiles.clear_request_profile()


def test_wizard_microsoft_and_provider_settings_reach_only_granted_assistants(ent, monkeypatch):
    from api.enterprise import provisioner
    _, _, root = ent
    (root / ".env").write_text("GLM_API_KEY=glm\nMICROSOFT_CLIENT_ID=cid\nMICROSOFT_CLIENT_SECRET=sec\n"
                               "MICROSOFT_TENANT_ID=tid\nTELEGRAM_BOT_TOKEN=bot\n")
    for p in ("u-anna", "u-ben"):
        (root / "profiles" / p).mkdir()
    monkeypatch.setattr(provisioner, "_install_plugins", lambda home, integ=(): None)
    monkeypatch.setattr(provisioner, "allowed_tools", lambda ts: [])
    monkeypatch.setattr(provisioner, "rescan", lambda: None)
    monkeypatch.setattr(provisioner, "_invalidate_webui_profile_cache", lambda: None)
    provisioner._build("u-anna", ["memory"], owner="a", integrations=["integration.outlook"], email="a@x")
    provisioner._build("u-ben", ["memory"], owner="b", integrations=[], email="b@x")
    anna = provisioner._read_env(root / "profiles" / "u-anna" / ".env")
    ben = provisioner._read_env(root / "profiles" / "u-ben" / ".env")
    assert anna["MS_GRAPH_CLIENT_ID"] == "cid" and anna["MS_GRAPH_TENANT_ID"] == "tid"
    assert anna["MS_GRAPH_AUTH_MODE"] == "app"  # mailbox pinned by address needs app auth
    assert not any(k.startswith(("MS_GRAPH", "MICROSOFT")) for k in ben)
    assert anna["GLM_API_KEY"] == ben["GLM_API_KEY"] == "glm"  # every wizard provider reaches users
    assert "TELEGRAM_BOT_TOKEN" not in anna and "TELEGRAM_BOT_TOKEN" not in ben


def test_google_client_saved_by_the_wizard_as_id_and_secret_is_the_organisations(ent):
    from api.enterprise import integrations
    _, _, root = ent
    with pytest.raises(integrations.DirectoryError):
        integrations._google_client()
    (root / ".env").write_text("GOOGLE_CLIENT_ID=gid\nGOOGLE_CLIENT_SECRET=gsec\n")
    assert integrations._google_client() == {"client_id": "gid", "client_secret": "gsec"}
    (root / "client_secret_123.json").write_text(json.dumps({"web": {"client_id": "file", "client_secret": "s"}}))
    assert integrations._google_client()["client_id"] == "file"  # a downloaded client JSON wins


def test_organisation_checklist_reflects_the_template_and_directory(ent):
    from api.enterprise.org_setup import org_setup_status
    _, _, root = ent
    status = org_setup_status()
    assert status["steps"] == ["company", "provider", "services", "organization", "finish"]
    assert status["checks"]["model"] is False and status["counts"]["users"] == 2
    assert status["counts"]["users_waiting_for_password"] == 1  # anna has not redeemed her link
    (root / "config.yaml").write_text("model:\n  default: some/model\n  provider: openrouter\n")
    (root / ".env").write_text("OPENROUTER_API_KEY=k\nTELEGRAM_BOT_TOKEN=t\n")
    status = org_setup_status()
    assert status["checks"]["model"] and status["checks"]["telegram_bot"] and status["model"] == "some/model"


def test_apply_refreshes_every_assistant_and_the_front_desk_then_restarts(ent, monkeypatch):
    from api.enterprise import org_setup, provisioner, routes
    _, ids, _ = ent
    done, refreshed, calls = threading.Event(), [], []
    monkeypatch.setattr(routes, "_provision", refreshed.append)
    monkeypatch.setattr(routes, "_actor", lambda h: routes.Actor(ids["boss"], "10.0.0.1"))
    monkeypatch.setattr(provisioner, "ensure_front_desk", lambda refresh=False: calls.append(("desk", refresh)))
    monkeypatch.setattr(provisioner, "restart_gateway", lambda: (calls.append("restart"), done.set()))
    assert org_setup.after_org_setup(object()) == {"refreshing": 2, "gateway_restart": True}
    assert done.wait(5)
    assert sorted(refreshed) == sorted(ids.values()) and calls == [("desk", True), "restart"]
