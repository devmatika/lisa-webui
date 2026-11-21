"""Lisa Enterprise Telegram linking: pairing code -> verified Telegram id -> the user's own profile."""
from __future__ import annotations

import json
import os

import pytest
import yaml

pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")


class FakePairing:
    """Stands in for gateway.pairing.PairingStore: codes map to Telegram ids, approvals are tracked."""

    def __init__(self):
        self.codes, self.approved, self.revoked = {}, {}, []

    def approve_code(self, platform, code):
        tg = self.codes.pop(code, None)
        if tg is None:
            return None
        self.approved[tg] = {"user_id": tg, "user_name": f"tg{tg}"}
        return {"user_id": tg, "user_name": f"tg{tg}"}

    def list_approved(self, platform=None):
        return list(self.approved.values())

    def revoke(self, platform, user_id):
        self.revoked.append(user_id)
        return self.approved.pop(user_id, None) is not None


@pytest.fixture()
def tg(tmp_path, monkeypatch, request):
    from api.enterprise import db as edb
    from api.enterprise import directory, provisioner, telegram
    from api.enterprise.models import Base, User

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
    (root / "config.yaml").write_text(yaml.safe_dump({"model": {"default": "m"}, "gateway": {"profile_routes": [
        {"name": "discord-team", "platform": "discord", "guild_id": "1", "profile": "team"}]}}))
    pairing, restarts = FakePairing(), []
    monkeypatch.setattr(telegram, "_store", lambda profile: pairing)
    monkeypatch.setattr(provisioner, "root_home", lambda: root)
    monkeypatch.setattr(provisioner, "ensure_front_desk", lambda: None)
    monkeypatch.setattr(provisioner, "restart_gateway", lambda: restarts.append(1) or {"pausing": True})
    with edb.session() as db:
        directory.seed(db)
        admin = directory.first_run_setup(db, org_name="KSP", email="boss@ksp.example", password="boss password 1",
                                          display_name="Boss")
        anna = directory.create_user(db, directory.Actor(admin.id), email="anna@ksp.example", display_name="Anna",
                                     role_keys=["staff"])
        ben = directory.create_user(db, directory.Actor(admin.id), email="ben@ksp.example", display_name="Ben",
                                    role_keys=["staff"])
        db.get(User, anna.id).hermes_profile = "u-anna"
        db.get(User, ben.id).hermes_profile = "u-ben"
        ids = {"admin": admin.id, "anna": anna.id, "ben": ben.id}
    yield edb, telegram, pairing, root, ids, restarts
    edb.reset_for_tests()


def _routes(root):
    return yaml.safe_load((root / "config.yaml").read_text())["gateway"]["profile_routes"]


def test_code_links_the_signed_in_user_and_routes_their_telegram_to_their_profile(tg):
    edb, telegram, pairing, root, ids, restarts = tg
    from api.enterprise.directory import Actor
    pairing.codes["ABCD2345"] = "111"
    with edb.session() as db:
        telegram.link(db, Actor(ids["anna"]), ids["anna"], "ABCD2345")
    with edb.session() as db:
        result = telegram.apply(db)
    routes = _routes(root)
    assert {"name": "lisa-111", "platform": "telegram", "user_id": "111", "chat_id": "111",
            "profile": "u-anna"} in routes
    assert routes[-1] == {"name": "lisa-front-desk", "platform": "telegram", "profile": "front-desk"}
    assert any(r["platform"] == "discord" for r in routes)  # routes Lisa does not own are kept
    assert result["routes_changed"] and restarts == [1]
    assert "TELEGRAM_ALLOWED_USERS=111\n" in (root / ".env").read_text()
    assert pairing.approved == {}  # pairing proved the id; the allowlist is the authority


def test_unchanged_routes_do_not_restart_the_gateway(tg):
    edb, telegram, pairing, root, ids, restarts = tg
    with edb.session() as db:
        telegram.apply(db)
    with edb.session() as db:
        telegram.apply(db)
    assert restarts == [1]


def test_wrong_or_reused_code_links_nothing(tg):
    edb, telegram, pairing, root, ids, restarts = tg
    from api.enterprise.directory import Actor, DirectoryError
    pairing.codes["ABCD2345"] = "111"
    with edb.session() as db:
        telegram.link(db, Actor(ids["anna"]), ids["anna"], "ABCD2345")
    for code in ("ABCD2345", "ZZZZ9999"):
        with pytest.raises(DirectoryError):
            with edb.session() as db:
                telegram.link(db, Actor(ids["ben"]), ids["ben"], code)


def test_a_telegram_account_cannot_be_linked_to_two_users(tg):
    edb, telegram, pairing, root, ids, restarts = tg
    from api.enterprise.directory import Actor, DirectoryError
    pairing.codes.update({"ANNA2345": "111", "AGAIN234": "111"})
    with edb.session() as db:
        telegram.link(db, Actor(ids["anna"]), ids["anna"], "ANNA2345")
    with pytest.raises(DirectoryError):
        with edb.session() as db:
            telegram.link(db, Actor(ids["ben"]), ids["ben"], "AGAIN234")
    with edb.session() as db:
        telegram.apply(db)
    assert [r["profile"] for r in _routes(root) if r.get("user_id") == "111"] == ["u-anna"]


def test_unlink_and_disable_remove_route_and_revoke_pairing(tg):
    edb, telegram, pairing, root, ids, restarts = tg
    from api.enterprise import directory
    from api.enterprise.directory import Actor
    pairing.codes.update({"ANNA2345": "111", "BENB2345": "222"})
    with edb.session() as db:
        telegram.link(db, Actor(ids["anna"]), ids["anna"], "ANNA2345")
        telegram.link(db, Actor(ids["ben"]), ids["ben"], "BENB2345")
    with edb.session() as db:
        telegram.unlink(db, Actor(ids["anna"]), ids["anna"])
        directory.set_status(db, Actor(ids["admin"]), ids["ben"], "disabled")
    with edb.session() as db:
        telegram.apply(db)
    assert not any(r.get("user_id") in ("111", "222") for r in _routes(root))
    assert "TELEGRAM_ALLOWED_USERS=\n" in (root / ".env").read_text()
    assert pairing.approved == {}


def test_identity_mapping_is_generated_from_the_user_directory(tg):
    edb, telegram, pairing, root, ids, restarts = tg
    from api.enterprise.directory import Actor
    pairing.codes["ANNA2345"] = "111"
    with edb.session() as db:
        telegram.link(db, Actor(ids["anna"]), ids["anna"], "ANNA2345")
    with edb.session() as db:
        telegram.apply(db)
    people = json.loads((root / "config" / "identity-mapping.json").read_text())["people"]
    anna = next(p for p in people if p["email"] == "anna@ksp.example")
    assert anna["telegram"] == {"user_id": "111"} and anna["person_id"] == ids["anna"]
    assert "telegram" not in next(p for p in people if p["email"] == "ben@ksp.example")


def test_identity_mapping_preserves_clickup_aliases_and_external_people(tg):
    edb, telegram, pairing, root, ids, restarts = tg
    from api.enterprise.directory import Actor
    mapping = root / "config" / "identity-mapping.json"
    mapping.parent.mkdir(parents=True, exist_ok=True)
    mapping.write_text(json.dumps({
        "people": [
            {
                "person_id": "old-anna",
                "canonical_name": "Anna Berger",
                "name": "Anna Berger",
                "email": "anna@ksp.example",
                "emails": ["anna@ksp.example", "anna.personal@example"],
                "role": "CEO",
                "aliases": ["Anna", "AB"],
                "clickup": {"user_id": 57259309, "member_name": "anna"},
                "telegram": {"user_id": "999", "display_name": "Anna", "username": "@anna"},
            },
            {
                "person_id": "stakeholder-1",
                "canonical_name": "External Partner",
                "name": "External Partner",
                "email": "partner@client.example",
                "emails": ["partner@client.example"],
                "role": "Partner",
                "clickup": {"user_id": 111, "member_name": "partner"},
                "aliases": ["Partner"],
            },
        ],
        "aliases": {"anna": "old-anna", "partner@client.example": "stakeholder-1"},
    }, indent=1))
    pairing.codes["ANNA2345"] = "111"
    with edb.session() as db:
        telegram.link(db, Actor(ids["anna"]), ids["anna"], "ANNA2345")
    with edb.session() as db:
        telegram.apply(db)
    data = json.loads(mapping.read_text())
    people = data["people"]
    anna = next(p for p in people if "anna@ksp.example" in (p.get("emails") or [p.get("email")]))
    assert anna["person_id"] == ids["anna"]
    assert anna["clickup"] == {"user_id": 57259309, "member_name": "anna"}
    assert anna["role"] == "CEO"
    assert anna["telegram"]["user_id"] == "111"
    assert anna["telegram"]["username"] == "@anna"
    assert "anna.personal@example" in anna["emails"]
    partner = next(p for p in people if p["email"] == "partner@client.example")
    assert partner["clickup"]["user_id"] == 111 and partner["person_id"] == "stakeholder-1"
    assert data["aliases"]["anna"] == ids["anna"]
    assert data["aliases"]["partner@client.example"] == "stakeholder-1"
    # Assistants read the file from their own profile home — publish a copy there too.
    profile_copy = root / "profiles" / "u-anna" / "config" / "identity-mapping.json"
    profile_copy.parent.mkdir(parents=True, exist_ok=True)
    with edb.session() as db:
        telegram.apply(db)
    assert profile_copy.is_file()
    copied = json.loads(profile_copy.read_text())
    assert next(p for p in copied["people"] if p["person_id"] == ids["anna"])["clickup"]["user_id"] == 57259309
