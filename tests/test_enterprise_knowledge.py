"""Lisa Enterprise knowledge: area grants decide every read, search and draft; nothing leaks across."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")


@pytest.fixture()
def kb(tmp_path, monkeypatch, request):
    from api.enterprise import db as edb
    from api.enterprise import directory
    from api.enterprise.models import Base

    url = os.environ.get("LISA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'lisa.db'}"
    if url.startswith("postgresql"):
        request.getfixturevalue("allow_outbound_network")
    monkeypatch.setenv("LISA_ENTERPRISE", "1")
    monkeypatch.setenv("LISA_DATABASE_URL", url)
    vault = tmp_path / "vault"
    monkeypatch.setenv("LISA_KNOWLEDGE_DIR", str(vault))
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
        a = directory.Actor(boss.id)
        ids = {"boss": boss.id}
        for name, roles, dept in (("anna", ["staff"], "Farm"), ("ben", ["staff"], "Office"),
                                  ("mia", ["manager"], "Farm"), ("max", ["manager"], "Farm")):
            ids[name] = directory.create_user(db, a, email=f"{name}@ksp.example", display_name=name,
                                              role_keys=roles, department=dept).id
    (vault / "farm").mkdir(parents=True)
    (vault / "hr").mkdir(parents=True)
    (vault / "farm" / "milking.md").write_text("# Milking SOP\n\nClean the milking robot at 6am daily.\n")
    (vault / "farm" / "robots" / "maintenance").mkdir(parents=True)
    (vault / "farm" / "robots" / "maintenance" / "checklist.md").write_text(
        "# Robot checklist\n\nInspect the milking robot weekly.\n")
    (vault / "hr" / "salaries.md").write_text("# Salaries\n\nCEO salary SECRET-HR 9000 euro milking bonus.\n")
    (vault / "farm" / ".history").mkdir()
    (vault / "farm" / ".history" / "old.md").write_text("old milking SECRET-OLD")
    with edb.session() as db:  # these tests build their own areas, without the standard folders
        from sqlalchemy import delete
        from api.enterprise.models import AreaGrant, KnowledgeArea, KnowledgeDraft
        # Cascade should clear grants/drafts with areas; delete explicitly so leftover
        # standard-folder grants from first_run_setup cannot survive a partial wipe.
        db.execute(delete(KnowledgeDraft))
        db.execute(delete(AreaGrant))
        db.execute(delete(KnowledgeArea))
    with edb.session() as db:
        from api.enterprise import knowledge
        knowledge.save_area(db, a, "farm", "Farm", [
            {"principal_type": "department", "principal_id": "Farm", "level": "contribute"},
            {"principal_type": "user", "principal_id": ids["mia"], "level": "approve"},
            {"principal_type": "user", "principal_id": ids["max"], "level": "approve"}])
        knowledge.save_area(db, a, "hr", "HR", [{"principal_type": "role", "principal_id": "operator", "level": "read"}])
    yield edb, ids, vault
    edb.reset_for_tests()


def _user(db, uid):
    from api.enterprise.directory import get_user
    return get_user(db, uid)


def test_search_returns_only_readable_areas(kb):
    from api.enterprise import knowledge
    edb, ids, _ = kb
    with edb.session() as db:
        anna = knowledge.search(db, _user(db, ids["anna"]), "milking")
        ben = knowledge.search(db, _user(db, ids["ben"]), "milking")
    assert [r["path"] for r in anna["results"]] == ["farm/milking.md"]
    assert "SECRET" not in str(anna) and ben["results"] == [] and ben["searched_areas"] == []


def test_admin_role_gets_no_document_access_by_itself(kb):
    from api.enterprise import knowledge
    edb, ids, _ = kb
    with edb.session() as db:
        boss = _user(db, ids["boss"])
        # boss is admin+operator: operator holds an explicit HR grant, admin alone grants nothing
        assert knowledge.access_for(db, boss).levels == {"hr": 1}
        assert "SECRET-HR" in str(knowledge.search(db, boss, "salary"))


def test_read_refuses_other_areas_and_path_escapes_identically(kb):
    from api.enterprise import knowledge
    from api.enterprise.directory import DirectoryError
    edb, ids, vault = kb
    (vault / "farm" / "link.md").symlink_to(vault / "hr" / "salaries.md")
    with edb.session() as db:
        anna = _user(db, ids["anna"])
        assert "Milking SOP" in knowledge.read(db, anna, "farm/milking.md")["content"]
        for path in ("hr/salaries.md", "farm/../hr/salaries.md", "farm/link.md", "farm/.history/old.md",
                     "../etc/passwd", "farm/nothing.md"):
            with pytest.raises(DirectoryError) as err:
                knowledge.read(db, anna, path)
            assert str(err.value) in ("Document not found", "Not a knowledge document")


def test_hidden_history_and_symlinked_files_are_never_searched(kb):
    from api.enterprise import knowledge
    edb, ids, vault = kb
    (vault / "farm" / "leak.md").symlink_to(vault / "hr" / "salaries.md")
    with edb.session() as db:
        found = knowledge.search(db, _user(db, ids["anna"]), "milking salary")
    assert "SECRET" not in str(found)


def test_teach_lisa_draft_needs_contribute_and_a_second_approver(kb):
    from api.enterprise import knowledge
    from api.enterprise.directory import Actor, DirectoryError
    edb, ids, vault = kb
    with edb.session() as db:
        with pytest.raises(DirectoryError):
            knowledge.submit_draft(db, Actor(ids["ben"]), _user(db, ids["ben"]), "farm", "X", "y")
        draft = knowledge.submit_draft(db, Actor(ids["mia"]), _user(db, ids["mia"]), "farm", "Feeding SOP",
                                       "Feed calves at 7.").id
    with edb.session() as db:
        assert knowledge.search(db, _user(db, ids["anna"]), "calves")["results"] == []  # not published yet
        with pytest.raises(DirectoryError):  # own draft
            knowledge.decide(db, Actor(ids["mia"]), _user(db, ids["mia"]), draft, True)
        with pytest.raises(DirectoryError):  # anna may contribute but not approve
            knowledge.decide(db, Actor(ids["anna"]), _user(db, ids["anna"]), draft, True)
    with edb.session() as db:
        done = knowledge.decide(db, Actor(ids["max"]), _user(db, ids["max"]), draft, True)
        assert done.published_path == "farm/feeding-sop.md"
        mia, maxu = _user(db, ids["mia"]).email, _user(db, ids["max"]).email
    page = (vault / "farm" / "feeding-sop.md").read_text()  # every published page says where it comes from and when
    assert f"source: Teach Lisa, written by {mia}" in page and f"approved_by: {maxu}" in page
    assert "\ndate: " in page and "\nupdated: " in page
    with edb.session() as db:
        assert knowledge.search(db, _user(db, ids["anna"]), "calves")["results"][0]["path"] == "farm/feeding-sop.md"


def test_republishing_keeps_the_previous_version(kb):
    from api.enterprise import knowledge
    from api.enterprise.directory import Actor
    edb, ids, vault = kb
    for text in ("v1 text", "v2 text"):
        with edb.session() as db:
            d = knowledge.submit_draft(db, Actor(ids["mia"]), _user(db, ids["mia"]), "farm", "Gate SOP", text).id
        with edb.session() as db:
            knowledge.decide(db, Actor(ids["max"]), _user(db, ids["max"]), d, True)
    assert "v2 text" in (vault / "farm" / "gate-sop.md").read_text()
    assert any("v1 text" in p.read_text() for p in (vault / "farm" / ".history").glob("gate-sop.*.md"))


def test_unanswered_questions_become_knowledge_gaps(kb):
    from sqlalchemy import select
    from api.enterprise import knowledge
    from api.enterprise.models import KnowledgeGap
    edb, ids, _ = kb
    with edb.session() as db:
        knowledge.search(db, _user(db, ids["anna"]), "tractor warranty")
    with edb.session() as db:
        assert [g.question for g in db.scalars(select(KnowledgeGap))] == ["tractor warranty"]


def test_agent_calls_must_prove_the_profile_with_its_own_key(kb, tmp_path, monkeypatch):
    from api.enterprise import provisioner, routes
    from api.enterprise.directory import DirectoryError
    from api.enterprise.models import User
    edb, ids, _ = kb
    root = tmp_path / ".hermes"
    for name, key in (("u-anna", "anna-key-123"), ("u-ben", "ben-key-456")):
        (root / "profiles" / name).mkdir(parents=True)
        (root / "profiles" / name / ".env").write_text(f"API_SERVER_KEY={key}\n")
    monkeypatch.setattr(provisioner, "root_home", lambda: root)
    with edb.session() as db:
        db.get(User, ids["anna"]).hermes_profile = "u-anna"
        db.get(User, ids["ben"]).hermes_profile = "u-ben"

    class H:
        def __init__(self, profile, key):
            self.headers = {"X-Lisa-Profile": profile, "Authorization": f"Bearer {key}"}
    with edb.session() as db:
        assert routes._agent_user(db, H("u-anna", "anna-key-123")).id == ids["anna"]
        for profile, key in (("u-anna", "ben-key-456"), ("u-ben", "anna-key-123"), ("u-anna", ""),
                             ("", "anna-key-123"), ("u-ghost", "x")):
            with pytest.raises(DirectoryError):
                routes._agent_user(db, H(profile, key))


def test_browsing_lists_only_the_areas_a_person_may_read(kb):
    from api.enterprise import knowledge
    edb, ids, _ = kb
    with edb.session() as db:
        anna = knowledge.tree(db, _user(db, ids["anna"]))
        ben = knowledge.tree(db, _user(db, ids["ben"]))
    assert [(a["key"], a["name"], a["level"]) for a in anna["areas"]] == [("farm", "Farm", "contribute")]
    # Nested vault paths stay nested so the Knowledge tab can build a folder tree (not a flat list).
    assert [d["path"] for d in anna["areas"][0]["docs"]] == [
        "farm/milking.md",
        "farm/robots/maintenance/checklist.md",
    ]  # never .history or other areas
    assert ben["areas"] == [] and "salaries" not in str(anna)


def test_delete_area_removes_grants_folder_and_source_mapping(kb, tmp_path, monkeypatch):
    import json

    from api.enterprise import knowledge, sources
    from api.enterprise.directory import Actor, DirectoryError
    from api.enterprise.models import KnowledgeArea, KnowledgeSource

    edb, ids, vault = kb
    local = tmp_path / "sources"
    repo = local / "vault"
    (repo / "Stale").mkdir(parents=True)
    (repo / "Keep").mkdir(parents=True)
    (repo / "Stale" / "x.md").write_text("# x\n")
    (repo / "Keep" / "y.md").write_text("# y\n")
    monkeypatch.setattr(sources, "LOCAL_SOURCES", local)
    with edb.session() as db:
        actor = Actor(ids["boss"])
        src = sources.save_source(
            db, actor, source_id=None, name="Vault", url=f"file://{repo}", branch="main",
            credential_id=None,
            folders=[{"folder": "Stale", "include": True, "key": "stale", "name": "Stale files",
                      "readers": ["staff"]},
                     {"folder": "Keep", "include": True, "key": "keep", "name": "Keep",
                      "readers": ["staff"]}])
        sid = src.id
        (vault / "stale").mkdir(parents=True, exist_ok=True)
        (vault / "stale" / "old.md").write_text("# old\n")
        sources._manifest_path(sid).parent.mkdir(parents=True, exist_ok=True)
        sources._manifest_path(sid).write_text('{"stale": ["old.md"], "keep": []}')
    with edb.session() as db:
        knowledge.delete_area(db, Actor(ids["boss"]), "stale")
        assert db.get(KnowledgeArea, "stale") is None
        assert db.get(KnowledgeArea, "keep") is not None
        mapped = db.get(KnowledgeSource, sid).folders
        assert mapped["Stale"] is None and mapped["Keep"] == "keep"
    assert not (vault / "stale").exists()
    assert "stale" not in json.loads(sources._manifest_path(sid).read_text())
    with edb.session() as db:
        with pytest.raises(DirectoryError, match="not found"):
            knowledge.delete_area(db, Actor(ids["boss"]), "stale")
        with pytest.raises(DirectoryError, match="last folder"):
            knowledge.delete_area(db, Actor(ids["boss"]), "keep")
