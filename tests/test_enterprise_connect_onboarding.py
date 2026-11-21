"""Staff default Google/Outlook grants and per-member getting-started wizard."""
from __future__ import annotations

import os

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
    monkeypatch.setenv("LISA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    edb.reset_for_tests()
    if url.startswith("postgresql"):
        with edb.engine().begin() as conn:
            Base.metadata.drop_all(conn)
            conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    edb.migrate()
    with edb.session() as db:
        directory.seed(db)
    yield edb
    edb.reset_for_tests()


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    from api.enterprise import provisioner
    root = tmp_path / ".hermes"
    (root / "profiles" / "u-anna").mkdir(parents=True)
    monkeypatch.setattr(provisioner, "root_home", lambda: root)
    return root


def _admin(edb):
    from api.enterprise import directory
    with edb.session() as db:
        return directory.first_run_setup(
            db, org_name="KSP", email="boss@ksp.example", password="boss password 1",
            display_name="Boss",
        )


def _session_user(user, profile="u-anna"):
    from api.enterprise.directory import permissions_of
    from api.enterprise.sessions import SessionUser
    return SessionUser(
        user_id=user.id, email=user.email, display_name=user.display_name,
        hermes_profile=profile, roles=tuple(sorted(r.key for r in user.roles)),
        permissions=permissions_of(user), token_hash="t",
    )


def test_staff_builtin_includes_google_and_outlook(ent):
    from sqlalchemy import select
    from api.enterprise.models import Role

    _admin(ent)
    with ent.session() as db:
        staff = db.scalar(select(Role).where(Role.key == "staff"))
        perms = {p.permission for p in staff.permissions}
    assert {"integration.google", "integration.outlook", "integration.email"} <= perms


def test_grant_release_adds_once_and_respects_later_removal(ent):
    from sqlalchemy import select
    from api.enterprise import directory
    from api.enterprise.models import Organization, Role, RolePermission

    _admin(ent)
    with ent.session() as db:
        staff = db.scalar(select(Role).where(Role.key == "staff"))
        staff.permissions = [
            RolePermission(permission=p)
            for p in ("chat.use", "memory.own", "files.workspace", "integration.email",
                      "knowledge.read", "knowledge.contribute")
        ]
        org = db.scalar(select(Organization))
        org.settings = {**(org.settings or {}), "applied_grant_releases": []}

    with ent.session() as db:
        changed = directory.apply_default_grant_releases(db)
    assert "staff" in changed

    with ent.session() as db:
        staff = db.scalar(select(Role).where(Role.key == "staff"))
        perms = {p.permission for p in staff.permissions}
        org = db.scalar(select(Organization))
        applied = set((org.settings or {}).get("applied_grant_releases") or [])
    assert {"integration.google", "integration.outlook"} <= perms
    assert "2026-11-staff-google-outlook" in applied

    with ent.session() as db:
        staff = db.scalar(select(Role).where(Role.key == "staff"))
        staff.permissions = [p for p in staff.permissions if p.permission != "integration.google"]
    with ent.session() as db:
        assert directory.apply_default_grant_releases(db) == []
    with ent.session() as db:
        staff = db.scalar(select(Role).where(Role.key == "staff"))
        assert "integration.google" not in {p.permission for p in staff.permissions}


def test_member_wizard_shows_education_even_when_apps_not_ready(ent, homes, monkeypatch):
    from api.enterprise import connect_onboarding, directory, integrations, mail_backends, routes

    boss = _admin(ent)
    with ent.session() as db:
        anna = directory.create_user(
            db, directory.Actor(boss.id), email="anna@ksp.example", display_name="Anna",
            role_keys=["staff"],
        )
        anna.hermes_profile = "u-anna"
        su = _session_user(anna)

    monkeypatch.setattr(integrations, "_google_ready", lambda: False)
    monkeypatch.setattr(mail_backends, "microsoft_app", lambda: None)

    with ent.session() as db:
        st = connect_onboarding.status(db, su)
    assert st["show"] is True  # education wizard, not only connect
    ids = [p["id"] for p in st["pages"]]
    assert ids[:2] == ["welcome", "basics"]
    assert "google" in ids and "microsoft" in ids and "telegram" in ids and ids[-1] == "finish"
    assert st["google"]["available"] and not st["google"]["needed"]  # allowed but company app missing
    assert st["needed"] == []  # telegram is optional; company apps not ready → nothing required
    assert any(c["status"] == "waiting" for c in st["checklist"] if c["label"] == "Google")
    assert st["telegram"]["optional"] is True

    connect_onboarding.complete("u-anna")
    with ent.session() as db:
        st = connect_onboarding.status(db, su)
    assert st["show"] is False and st["completed"] is True

    connect_onboarding.reopen("u-anna")
    with ent.session() as db:
        st = connect_onboarding.status(db, su)
    assert st["show"] is True and st["step"] == "welcome"

    assert ("POST", "/api/enterprise/onboarding/connect/complete") in routes.ROUTES
    assert ("POST", "/api/enterprise/onboarding/connect/reopen") in routes.ROUTES


def test_member_wizard_skip_and_connect_gates(ent, homes, monkeypatch):
    from api.enterprise import connect_onboarding, directory, integrations, mail_backends

    boss = _admin(ent)
    with ent.session() as db:
        anna = directory.create_user(
            db, directory.Actor(boss.id), email="anna@ksp.example", display_name="Anna",
            role_keys=["staff"],
        )
        anna.hermes_profile = "u-anna"
        su = _session_user(anna)

    monkeypatch.setattr(integrations, "_google_ready", lambda: True)
    monkeypatch.setattr(mail_backends, "microsoft_app", lambda: {"tenant": "t"})

    with ent.session() as db:
        st = connect_onboarding.status(db, su)
    assert "google" in st["needed"] and "microsoft" in st["needed"]

    connect_onboarding.skip_action("u-anna", "google")
    (homes / "profiles" / "u-anna" / "google_token.json").write_text("{}")
    with ent.session() as db:
        st = connect_onboarding.status(db, su)
    assert "google" not in st["needed"]
    assert st["google"]["connected"] is True or st["google"]["skipped"] is True

    connect_onboarding.dismiss("u-anna")
    with ent.session() as db:
        assert connect_onboarding.status(db, su)["show"] is False


def test_seed_still_does_not_restore_removed_grants(ent):
    from sqlalchemy import select
    from api.enterprise.directory import seed
    from api.enterprise.models import Role, RolePermission

    _admin(ent)
    with ent.session() as db:
        staff = db.scalar(select(Role).where(Role.key == "staff"))
        staff.permissions = [RolePermission(permission="chat.use")]
    with ent.session() as db:
        seed(db)
    with ent.session() as db:
        staff = db.scalar(select(Role).where(Role.key == "staff"))
        assert [p.permission for p in staff.permissions] == ["chat.use"]
