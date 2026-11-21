"""Organisation identity-mapping.json: merge Lisa users without wiping ClickUp, publish to profiles."""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")


@pytest.fixture()
def idmap_env(tmp_path, monkeypatch, request):
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
    root.mkdir()
    (root / "config.yaml").write_text("model: {default: m}\n")
    monkeypatch.setattr(provisioner, "root_home", lambda: root)
    with edb.session() as db:
        directory.seed(db)
        admin = directory.first_run_setup(db, org_name="KSP", email="boss@ksp.example",
                                          password="boss password 1", display_name="Boss")
        anna = directory.create_user(db, directory.Actor(admin.id), email="anna@ksp.example",
                                     display_name="Anna", role_keys=["staff"])
        ids = {"admin": admin.id, "anna": anna.id}
    yield edb, root, ids
    edb.reset_for_tests()


def test_sync_adds_new_user_and_publishes_to_profiles(idmap_env):
    from api.enterprise import identity_mapping

    edb, root, ids = idmap_env
    (root / "profiles" / "u-anna" / "config").mkdir(parents=True)
    (root / "config" / "identity-mapping.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "config" / "identity-mapping.json").write_text(json.dumps({
        "people": [{
            "person_id": "old",
            "name": "Anna",
            "email": "anna@ksp.example",
            "emails": ["anna@ksp.example"],
            "clickup": {"user_id": 42, "member_name": "anna"},
            "aliases": ["Anna"],
        }],
        "aliases": {},
    }))
    with edb.session() as db:
        payload = identity_mapping.sync(db, root)
    anna = next(p for p in payload["people"] if p["email"] == "anna@ksp.example")
    assert anna["person_id"] == ids["anna"]
    assert anna["clickup"]["user_id"] == 42
    boss = next(p for p in payload["people"] if p["email"] == "boss@ksp.example")
    assert boss["person_id"] == ids["admin"]
    profile_file = root / "profiles" / "u-anna" / "config" / "identity-mapping.json"
    assert json.loads(profile_file.read_text())["people"][0]["clickup"]["user_id"] == 42 or any(
        p.get("clickup", {}).get("user_id") == 42 for p in json.loads(profile_file.read_text())["people"]
    )
