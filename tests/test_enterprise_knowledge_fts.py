"""FTS5 ranking stays behind Lisa ACL; snippets come from the local mirror only."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")


@pytest.fixture()
def fts_kb(tmp_path, monkeypatch):
    from api.enterprise import db as edb
    from api.enterprise import directory, knowledge
    from api.enterprise.models import Base, AreaGrant, KnowledgeArea, KnowledgeDraft
    from sqlalchemy import delete

    url = os.environ.get("LISA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'lisa-fts.db'}"
    monkeypatch.setenv("LISA_ENTERPRISE", "1")
    monkeypatch.setenv("LISA_DATABASE_URL", url)
    monkeypatch.setenv("LISA_KNOWLEDGE_FTS", "1")
    vault = tmp_path / "vault"
    monkeypatch.setenv("LISA_KNOWLEDGE_DIR", str(vault))
    edb.reset_for_tests()
    edb.migrate()
    with edb.session() as db:
        directory.seed(db)
        boss = directory.first_run_setup(db, org_name="KSP", email="boss@ksp.example",
                                         password="boss password 1", display_name="Boss")
        a = directory.Actor(boss.id)
        ids = {"boss": boss.id}
        for name, roles, dept in (("anna", ["staff"], "Farm"), ("ben", ["staff"], "Office")):
            ids[name] = directory.create_user(db, a, email=f"{name}@ksp.example", display_name=name,
                                              role_keys=roles, department=dept).id
        db.execute(delete(KnowledgeDraft))
        db.execute(delete(AreaGrant))
        db.execute(delete(KnowledgeArea))
    (vault / "farm").mkdir(parents=True)
    (vault / "hr").mkdir(parents=True)
    (vault / "farm" / "milking.md").write_text("# Milking SOP\n\nClean the milking robot at 6am daily.\n")
    (vault / "hr" / "salaries.md").write_text("# Salaries\n\nCEO salary SECRET-HR 9000 euro.\n")
    with edb.session() as db:
        knowledge.save_area(db, a, "farm", "Farm", [
            {"principal_type": "department", "principal_id": "Farm", "level": "contribute"}])
        knowledge.save_area(db, a, "hr", "HR", [
            {"principal_type": "role", "principal_id": "operator", "level": "read"}])
    yield edb, ids, vault
    edb.reset_for_tests()


def _user(db, uid):
    from api.enterprise.directory import get_user
    return get_user(db, uid)


def test_fts_search_respects_area_grants(fts_kb):
    from api.enterprise import knowledge
    edb, ids, _ = fts_kb
    with edb.session() as db:
        anna = knowledge.search(db, _user(db, ids["anna"]), "milking")
        ben = knowledge.search(db, _user(db, ids["ben"]), "milking")
        boss = knowledge.search(db, _user(db, ids["boss"]), "salary SECRET-HR")
    assert [r["path"] for r in anna["results"]] == ["farm/milking.md"]
    assert "SECRET" not in str(anna)
    assert ben["results"] == [] and ben["searched_areas"] == []
    assert any("salaries" in r["path"] for r in boss["results"])
    assert "SECRET-HR" in str(boss["results"])


def test_moved_note_dropped_even_if_stale_rank_would_match(fts_kb, monkeypatch):
    from api.enterprise import knowledge, knowledge_fts
    edb, ids, vault = fts_kb
    # Build a real index, then remove the farm file so a stale path must be dropped.
    with edb.session() as db:
        knowledge.search(db, _user(db, ids["anna"]), "milking")
    (vault / "farm" / "milking.md").unlink()

    def stale_paths(readable, query, limit=8):
        return [{"path": "farm/milking.md", "area": "farm", "title": "Milking SOP", "score": 10.0}]

    monkeypatch.setattr(knowledge_fts, "search_paths", stale_paths)
    with edb.session() as db:
        out = knowledge.search(db, _user(db, ids["anna"]), "milking")
    assert out["results"] == []  # term fallback also finds nothing


def test_fts_disabled_falls_back_to_term_search(fts_kb, monkeypatch):
    from api.enterprise import knowledge
    monkeypatch.setenv("LISA_KNOWLEDGE_FTS", "0")
    edb, ids, _ = fts_kb
    with edb.session() as db:
        anna = knowledge.search(db, _user(db, ids["anna"]), "milking")
    assert [r["path"] for r in anna["results"]] == ["farm/milking.md"]
