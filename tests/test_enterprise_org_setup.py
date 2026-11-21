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
    monkeypatch.setenv("LISA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
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
    assert status["steps"] == [
        "company", "provider", "capabilities", "services",
        "approvals", "knowledge", "orgknowledge", "team", "briefing", "finish",
    ]
    assert status["checks"]["model"] is False and status["counts"]["users"] == 2
    assert status["counts"]["users_waiting_for_password"] == 1  # anna has not redeemed her link
    (root / "config.yaml").write_text("model:\n  default: some/model\n  provider: openrouter\n")
    (root / ".env").write_text("OPENROUTER_API_KEY=k\nTELEGRAM_BOT_TOKEN=t\n")
    status = org_setup_status()
    assert status["checks"]["model"] and status["checks"]["telegram_bot"] and status["model"] == "some/model"
    (root / ".env").write_text("")
    (root / "config.yaml").write_text("model:\n  default: local\n  provider: custom\n  base_url: http://llm:8000/v1\n")
    assert org_setup_status()["checks"]["model"]  # a custom / local endpoint needs no key in .env


def test_apply_refreshes_every_assistant_and_the_front_desk_then_restarts(ent, monkeypatch):
    from api.enterprise import org_setup, provisioner, routes
    _, ids, _ = ent
    done, refreshed, calls = threading.Event(), [], []
    monkeypatch.setattr(routes, "_provision", refreshed.append)
    monkeypatch.setattr(routes, "_actor", lambda h: routes.Actor(ids["boss"], "10.0.0.1"))
    monkeypatch.setattr(provisioner, "ensure_front_desk", lambda refresh=False: calls.append(("desk", refresh)))
    monkeypatch.setattr(provisioner, "restart_gateway", lambda: (calls.append("restart"), done.set()))
    result = org_setup.after_org_setup(object())
    assert result["refreshing"] == 2 and result["gateway_restart"] is True
    assert done.wait(5)
    assert sorted(refreshed) == sorted(ids.values()) and calls == [("desk", True), "restart"]


def test_the_organisation_can_never_lock_itself_out_of_setup(ent):
    """The only operator removing their own Operator role left nobody able to open the wizard."""
    from api.enterprise import directory
    edb, ids, _ = ent
    with pytest.raises(directory.DirectoryError, match="run the setup wizard"), edb.session() as db:
        directory.set_roles(db, directory.Actor(ids["boss"]), ids["boss"], ["admin"])
    with pytest.raises(directory.DirectoryError, match="manage users"), edb.session() as db:
        directory.save_role(db, directory.Actor(ids["boss"]), key="admin", name="Administrator",
                            permissions=["chat.use"], toolsets=[])
    with edb.session() as db:  # once someone else holds it, the change is fine
        directory.set_roles(db, directory.Actor(ids["boss"]), ids["anna"], ["staff", "operator"])
        directory.set_roles(db, directory.Actor(ids["boss"]), ids["boss"], ["admin"])
        assert "operator.setup" not in directory.permissions_of(directory.get_user(db, ids["boss"]))


def test_server_shell_recovery_gives_the_roles_back(ent):
    from api.enterprise import directory, recover
    from api.enterprise.models import AuditEvent
    from sqlalchemy import select
    edb, ids, _ = ent
    with edb.session() as db:
        directory.set_roles(db, directory.Actor(ids["boss"]), ids["anna"], ["staff", "operator"])
        directory.set_roles(db, directory.Actor(ids["boss"]), ids["boss"], ["admin"])
    assert recover.main(["BOSS@ksp.example"]) == 0
    with edb.session() as db:
        assert {r.key for r in directory.get_user(db, ids["boss"]).roles} == {"admin", "operator"}
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == "user.recovered")) is not None
    assert recover.main(["ghost@ksp.example"]) == 1


def _zip(files: dict) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def test_an_obsidian_vault_is_imported_into_an_area_and_nothing_escapes_it(ent, tmp_path, monkeypatch):
    from api.enterprise import directory, knowledge
    edb, ids, _ = ent
    vault = tmp_path / "vault"
    monkeypatch.setenv("LISA_KNOWLEDGE_DIR", str(vault))
    actor = directory.Actor(ids["boss"])
    with edb.session() as db:
        with pytest.raises(directory.DirectoryError, match="create it first"):
            knowledge.import_vault(db, actor, "handbook", _zip({"a.md": "x"}))
        knowledge.save_area(db, actor, "handbook", "Handbook", [{"principal_type": "role", "principal_id": "staff",
                                                                  "level": "read"}])
        r = knowledge.import_vault(db, actor, "handbook", _zip({
            "Company/Sales/Pricing.md": "# Pricing", "Company/Welcome.md": "hi", "Company/.obsidian/app.md": "cfg",
            "Company/logo.png": "png", "Company/../../escape.md": "evil"}))
        assert r["imported"] == 2 and r["skipped"] == 2  # .obsidian and the traversal; the png is not a note
        anna = directory.get_user(db, ids["anna"])
        assert [h["title"] for h in knowledge.search(db, anna, "pricing")["results"]] == ["Pricing"]
    assert (vault / "handbook" / "Sales" / "Pricing.md").is_file()  # the vault's own folder is not repeated
    assert not (tmp_path / "escape.md").exists() and not list(vault.rglob("app.md"))
    with edb.session() as db, pytest.raises(directory.DirectoryError, match="not a .zip"):
        knowledge.import_vault(db, actor, "handbook", b"plain text")


def test_company_clickup_reaches_granted_assistants_with_their_email(ent, monkeypatch):
    from api.enterprise import provisioner
    _, _, root = ent
    (root / ".env").write_text("CLICKUP_API_KEY=pk_company\n")
    for p in ("u-anna", "u-ben"):
        (root / "profiles" / p).mkdir()
    monkeypatch.setattr(provisioner, "_install_plugins", lambda home, integ=(): None)
    monkeypatch.setattr(provisioner, "allowed_tools", lambda ts: [])
    monkeypatch.setattr(provisioner, "rescan", lambda: None)
    monkeypatch.setattr(provisioner, "_invalidate_webui_profile_cache", lambda: None)
    provisioner._build("u-anna", ["memory"], owner="a", integrations=["integration.clickup"], email="anna@x")
    provisioner._build("u-ben", ["memory"], owner="b", integrations=[], email="ben@x")
    anna = provisioner._read_env(root / "profiles" / "u-anna" / ".env")
    assert anna["CLICKUP_COMPANY_TOKEN"] == "pk_company" and "CLICKUP_API_TOKEN" not in anna
    assert "CLICKUP" not in (root / "profiles" / "u-ben" / ".env").read_text()
    import yaml
    cfg = yaml.safe_load((root / "profiles" / "u-anna" / "config.yaml").read_text())
    assert cfg["plugins"]["entries"]["clickup-personal"]["settings"] == {"account_email": "anna@x"}


def test_each_vault_folder_can_become_its_own_area_with_its_own_access(ent, tmp_path, monkeypatch):
    from api.enterprise import directory, knowledge
    edb, ids, _ = ent
    monkeypatch.setenv("LISA_KNOWLEDGE_DIR", str(tmp_path / "vault"))
    actor = directory.Actor(ids["boss"])
    staff_read = [{"principal_type": "role", "principal_id": "staff", "level": "read"}]
    with edb.session() as db:
        knowledge.save_area(db, actor, "general", "General", staff_read)
        knowledge.save_area(db, actor, "projects", "Projects", [])  # exists: its (empty) access is kept
        r = knowledge.import_vault(db, actor, "general", _zip({
            "lisa/Welcome.md": "# Welcome", "lisa/Knowledge/Onboarding.md": "# Onboarding steps",
            "lisa/Projects/Secret plan.md": "# Secret plan", "lisa/پروژه‌ها/Note.md": "# Persian folder"}),
            split_folders=True, new_area_grants=staff_read)
        assert r["areas"] == {"general": 1, "knowledge": 1, "projects": 1, knowledge._folder_area_key("پروژه‌ها"): 1}
        anna = directory.get_user(db, ids["anna"])
        assert [h["title"] for h in knowledge.search(db, anna, "onboarding")["results"]] == ["Onboarding steps"]
        assert knowledge.search(db, anna, "secret")["results"] == []  # the existing area's access wins


def test_an_obsidian_note_is_found_by_its_file_name(ent, tmp_path, monkeypatch):
    """Obsidian titles are file names; the default Welcome.md never says 'welcome' in its text."""
    from api.enterprise import directory, knowledge
    edb, ids, _ = ent
    monkeypatch.setenv("LISA_KNOWLEDGE_DIR", str(tmp_path / "vault"))
    actor = directory.Actor(ids["boss"])
    with edb.session() as db:
        knowledge.save_area(db, actor, "general", "General",
                            [{"principal_type": "role", "principal_id": "staff", "level": "read"}])
        knowledge.import_vault(db, actor, "general", _zip({"Holiday policy.md": "Ask two weeks ahead."}))
        anna = directory.get_user(db, ids["anna"])
        assert [h["title"] for h in knowledge.search(db, anna, "holiday")["results"]] == ["Holiday policy"]


def test_single_user_onboarding_never_covers_the_organisation_wizard(monkeypatch):
    """A fresh install showed Hermes' own 'Provider setup' on top of the setup wizard."""
    import io
    import json as _json
    from urllib.parse import urlparse
    from api import routes
    monkeypatch.setenv("LISA_ENTERPRISE", "1")
    monkeypatch.setattr(routes, "get_onboarding_status", lambda: {"completed": False})
    sent = {}
    monkeypatch.setattr(routes, "j", lambda handler, data, *a, **k: sent.update(data) or True)
    routes.handle_get(SimpleNamespace(wfile=io.BytesIO(), headers={}), urlparse("/api/onboarding/status"))
    assert sent["completed"] is True


def test_provisioner_enables_approval_companions_and_copies_approvals(ent):
    from api.enterprise.provisioner import companion_plugins, render_config
    assert companion_plugins(["integration.google", "integration.clickup"]) == [
        "business-action-approval", "clickup-approval",
    ]
    cfg = render_config(
        {"approvals": {"policies": {"calendar_write": "ask", "clickup_write": "ask"}}},
        ["memory"], "u-x",
        ["integration.google", "integration.clickup"], "a@x.com",
        permissions={"integration.google", "integration.clickup"},
    )
    enabled = cfg["plugins"]["enabled"]
    assert "business-action-approval" in enabled and "clickup-approval" in enabled
    assert cfg["approvals"]["policies"]["calendar_write"] == "ask"


def test_provisioner_seeds_persona_from_default_template(ent, monkeypatch):
    from api.enterprise import provisioner
    _, _, root = ent
    (root / "SOUL.md").write_text("# Company Soul\n", encoding="utf-8")
    (root / "memories").mkdir(parents=True)
    (root / "memories" / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    (root / "memories" / "USER.md").write_text("# Template User\n", encoding="utf-8")
    (root / "config.yaml").write_text("model: {default: m}\n")
    (root / "profiles" / "u-anna").mkdir(parents=True)  # skip hermes create_profile
    monkeypatch.setattr(provisioner, "_install_plugins", lambda home, integ=(): None)
    monkeypatch.setattr(provisioner, "allowed_tools", lambda ts: [])
    monkeypatch.setattr(provisioner, "rescan", lambda: None)
    monkeypatch.setattr(provisioner, "_invalidate_webui_profile_cache", lambda: None)
    provisioner._build("u-anna", ["memory"], owner="a", integrations=[], email="a@x")
    home = root / "profiles" / "u-anna"
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "# Company Soul\n"
    assert (home / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "# Memory\n"
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8") == "# Template User\n"
    (home / "memories" / "USER.md").write_text("# Personalized\n", encoding="utf-8")
    (root / "memories" / "USER.md").write_text("# New Template\n", encoding="utf-8")
    provisioner._build("u-anna", ["memory"], owner="a", integrations=[], email="a@x")
    assert (home / "memories" / "USER.md").read_text(encoding="utf-8") == "# Personalized\n"
    assert (home / "SOUL.md").read_text(encoding="utf-8") == "# Company Soul\n"


def test_capability_grants_add_missing_integration_permissions(ent, monkeypatch, tmp_path):
    from api.enterprise import directory, org_setup
    from api.enterprise.models import Role
    from sqlalchemy import select
    edb, ids, root = ent
    monkeypatch.setattr("api.customer_wizard.load_wizard_state", lambda: {
        "capabilities": ["email_assistant", "task_assistant"],
    })
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: root)
    with edb.session() as db:
        applied = org_setup.apply_capability_grants(db)
        staff = db.scalar(select(Role).where(Role.key == "staff"))
        perms = {p.permission for p in staff.permissions}
    assert "integration.clickup" in perms
    assert any(a.startswith("staff:") for a in applied)
