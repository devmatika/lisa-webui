"""Lisa Enterprise knowledge sources: the company's Git vault becomes areas, each with its own
readers, kept in sync, and nothing outside the mounted sources folder can be read."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture()
def ent(tmp_path, monkeypatch, request):
    from api.enterprise import db as edb
    from api.enterprise import directory, sources
    from api.enterprise.models import Base
    url = os.environ.get("LISA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'lisa.db'}"
    if url.startswith("postgresql"):
        request.getfixturevalue("allow_outbound_network")
    monkeypatch.setenv("LISA_ENTERPRISE", "1")
    monkeypatch.setenv("LISA_DATABASE_URL", url)
    monkeypatch.setenv("LISA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.setattr(sources, "LOCAL_SOURCES", tmp_path / "sources")
    edb.reset_for_tests()
    if url.startswith("postgresql"):
        with edb.engine().begin() as conn:
            Base.metadata.drop_all(conn)
            conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    edb.migrate()
    repo = tmp_path / "sources" / "vault"
    for rel, text in {"README.md": "# Company brain", "01-People/Anna.md": "# Anna\nSales lead",
                      "03-Projects/Hotel.md": "# Hotel pilot\nconfidential budget",
                      "06-Knowledge/Holidays.md": "# Holidays\nTwo weeks ahead",
                      "90-Templates/Meeting.md": "# Template",
                      "files/offers/note.md": "# Offer draft\nnot knowledge",
                      ".obsidian/app.md": "x"}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "vault")
    with edb.session() as db:
        directory.seed(db)
        boss = directory.first_run_setup(db, org_name="KSP", email="boss@ksp.example", password="boss password 1",
                                         display_name="Boss")
        anna = directory.create_user(db, directory.Actor(boss.id), email="anna@ksp.example", display_name="Anna",
                                     role_keys=["staff"])
        mgr = directory.create_user(db, directory.Actor(boss.id), email="mia@ksp.example", display_name="Mia",
                                    role_keys=["manager"])
        ids = {"boss": boss.id, "anna": anna.id, "mia": mgr.id}
    yield edb, ids, repo
    edb.reset_for_tests()


def test_only_repositories_and_the_mounted_sources_folder_can_be_connected(ent, tmp_path):
    from api.enterprise import directory, sources
    for bad in ("file:///home/hermeswebui/.hermes", f"file://{tmp_path}/sources/../knowledge", "ext::sh -c id",
                "/etc", "http://insecure.example/repo.git"):
        with pytest.raises(directory.DirectoryError):
            sources._check_url(bad)
    assert sources._check_url("git@gitlab.com:matikadev/vault.git")
    assert sources._check_url("https://github.com/org/vault.git")


def test_the_vault_becomes_areas_with_their_own_readers_and_stays_in_sync(ent):
    from api.enterprise import directory, knowledge, sources
    edb, ids, repo = ent
    url = f"file://{repo}"
    pre = sources.preview(url, "main", None)
    by = {f["folder"]: f for f in pre["folders"]}
    assert set(by) == {"01-People", "03-Projects", "06-Knowledge", "90-Templates", "files"} and pre["root_notes"] == 1
    assert by["03-Projects"]["key"] == "projects" and by["03-Projects"]["name"] == "Projects"
    assert by["06-Knowledge"]["suggested"] and not by["90-Templates"]["suggested"] and not by["files"]["suggested"]
    folders = [{"folder": "06-Knowledge", "include": True, "readers": ["staff", "manager"]},
               {"folder": "03-Projects", "include": True, "readers": ["manager"]},
               {"folder": "", "include": True, "key": "general", "name": "General", "readers": ["staff", "manager"]},
               {"folder": "90-Templates", "include": False}]
    with edb.session() as db:
        src = sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="Matika vault", url=url,
                                  branch="main", credential_id=None, folders=folders)
        sid = src.id
    assert sources.sync(sid)["notes"] == 3 and sources.sync(sid)["last_error"] is None
    with pytest.raises(directory.DirectoryError, match="already connected"), edb.session() as db:
        sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="again", url=url, branch="main",
                            credential_id=None, folders=folders)

    def titles(who, q):
        with edb.session() as db:
            return sorted(h["title"] for h in knowledge.search(db, directory.get_user(db, ids[who]), q)["results"])
    assert titles("anna", "holidays") == ["Holidays"]
    assert titles("anna", "budget") == [] and titles("mia", "budget") == ["Hotel pilot"]  # projects: managers only
    assert titles("mia", "template") == []  # left out
    # a note approved in Lisa lives next to the mirrored ones and survives every sync
    (knowledge.vault_root() / "knowledge" / "approved-in-lisa.md").write_text("# Parking\nBehind the barn")
    os.remove(repo / "06-Knowledge" / "Holidays.md")
    (repo / "06-Knowledge" / "Sub").mkdir()
    (repo / "06-Knowledge" / "Sub" / "Expenses.md").write_text("# Expenses\nReceipts monthly")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "edit")
    assert sources.sync(sid)["notes"] == 3
    assert titles("anna", "holidays") == [] and titles("anna", "receipts") == ["Expenses"]
    assert titles("anna", "barn") == ["Parking"]
    with edb.session() as db:  # disconnecting removes what the vault brought, nothing else
        sources.delete_source(db, directory.Actor(ids["boss"]), sid)
    assert titles("anna", "receipts") == [] and titles("anna", "barn") == ["Parking"]


def test_a_failed_pull_is_reported_in_plain_words(ent, tmp_path):
    from api.enterprise import directory, sources
    edb, ids, repo = ent
    with pytest.raises(directory.DirectoryError, match="branch does not exist"):
        sources.preview(f"file://{repo}", "no-such-branch", None)
    with edb.session() as db:
        sid = sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="v", url=f"file://{repo}",
                                  branch="main", credential_id=None,
                                  folders=[{"folder": "06-Knowledge", "include": True, "readers": ["staff"]}]).id
    shutil.rmtree(repo)
    view = sources.sync(sid)
    assert view["last_error"] and view["last_sync_at"]


def test_a_new_organisation_starts_with_the_standard_folders_and_their_access(ent):
    from api.enterprise import directory, knowledge
    from api.enterprise.knowledge_defaults import STANDARD_FOLDERS, add_standard_areas, missing_standard_areas
    edb, ids, _repo = ent
    with edb.session() as db:
        assert missing_standard_areas(db) == []
        levels = {who: knowledge.access_for(db, directory.get_user(db, ids[who])).levels for who in ("anna", "mia")}
        assert len(STANDARD_FOLDERS) == 8
    assert "people" not in levels["anna"] and "projects" not in levels["anna"]
    assert levels["mia"]["people"] == 1 and levels["anna"]["meetings"] == 1
    assert levels["anna"]["knowledge"] == 2  # staff may teach Lisa how things are done
    with edb.session() as db:  # adding the defaults again never touches an area the admin changed
        knowledge.save_area(db, directory.Actor(ids["boss"]), "people", "Team", [])
    with edb.session() as db:
        assert add_standard_areas(db, directory.Actor(ids["boss"])) == []
        assert knowledge.areas_view(db)[[a["key"] for a in knowledge.areas_view(db)].index("people")]["grants"] == []


def test_a_vault_with_the_standard_folders_keeps_their_access(ent):
    from api.enterprise import directory, knowledge, sources
    edb, ids, repo = ent
    url = f"file://{repo}"
    folders = [{"folder": "06-Knowledge", "include": True, "readers": ["staff", "manager"]},
               {"folder": "03-Projects", "include": True, "readers": ["manager"]}]
    with edb.session() as db:
        sid = sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="v", url=url, branch="main",
                                  credential_id=None, folders=folders).id
    sources.sync(sid)
    with edb.session() as db:
        anna = knowledge.access_for(db, directory.get_user(db, ids["anna"])).levels
        assert anna["knowledge"] == 2 and "projects" not in anna  # Teach-Lisa access survived the vault save
        assert [h["title"] for h in knowledge.search(db, directory.get_user(db, ids["mia"]), "budget")["results"]] \
            == ["Hotel pilot"]


def test_the_starter_vault_has_every_standard_folder():
    import io
    import zipfile
    from api.enterprise.knowledge_defaults import STANDARD_FOLDERS, UNREAD_FOLDERS, starter_vault_zip
    names = zipfile.ZipFile(io.BytesIO(starter_vault_zip("KSP"))).namelist()
    for folder in [f[0] for f in STANDARD_FOLDERS] + [f[0] for f in UNREAD_FOLDERS]:
        assert any(n.startswith(f"company-vault/{folder}/") for n in names)
    assert "company-vault/README.md" in names


def test_an_approved_note_is_committed_to_the_vault_by_its_author(ent, tmp_path):
    from api.enterprise import directory, knowledge, sources
    edb, ids, repo = ent
    bare = tmp_path / "sources" / "vault.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True)
    with edb.session() as db:
        sid = sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="v", url=f"file://{bare}",
                                  branch="main", credential_id=None, write_back=True,
                                  folders=[{"folder": "06-Knowledge", "include": True, "readers": ["staff"],
                                            "contributors": ["staff"]}]).id
        knowledge.save_area(db, directory.Actor(ids["boss"]), "knowledge", "Knowledge", [
            {"principal_type": "role", "principal_id": "staff", "level": "contribute"},
            {"principal_type": "role", "principal_id": "admin", "level": "approve"}])
    sources.sync(sid)
    with edb.session() as db:
        draft = knowledge.submit_draft(db, directory.Actor(ids["anna"]), directory.get_user(db, ids["anna"]),
                                       "knowledge", "Parking", "Behind the barn")
        did = draft.id
    with edb.session() as db:
        knowledge.decide(db, directory.Actor(ids["boss"]), directory.get_user(db, ids["boss"]), did, True)
    view = sources.sync(sid)
    assert view["push_error"] is None and view["last_error"] is None
    log = subprocess.run(["git", "log", "-1", "--format=%an <%ae>|%s|%b", "main"], cwd=bare, capture_output=True,
                         text=True, check=True).stdout
    assert log.startswith("Anna <anna@ksp.example>|Lisa: Parking|Taught in Lisa and approved by boss@ksp.example")
    shown = subprocess.run(["git", "show", "main:06-Knowledge/parking.md"], cwd=bare, capture_output=True,
                           text=True, check=True).stdout
    assert "Behind the barn" in shown
    before = subprocess.run(["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True).stdout
    sources.sync(sid)  # pushed once only
    assert subprocess.run(["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True).stdout == before


def test_a_vault_lisa_may_not_write_to_keeps_the_note_and_says_why(ent, tmp_path):
    from api.enterprise import directory, knowledge, sources
    edb, ids, repo = ent
    with edb.session() as db:  # a checked-out (non-bare) repository refuses pushes to its branch
        sid = sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="v", url=f"file://{repo}",
                                  branch="main", credential_id=None, write_back=True,
                                  folders=[{"folder": "06-Knowledge", "include": True, "readers": ["staff"]}]).id
        knowledge.save_area(db, directory.Actor(ids["boss"]), "knowledge", "Knowledge", [
            {"principal_type": "role", "principal_id": "staff", "level": "contribute"},
            {"principal_type": "role", "principal_id": "admin", "level": "approve"}])
        did = knowledge.submit_draft(db, directory.Actor(ids["anna"]), directory.get_user(db, ids["anna"]),
                                     "knowledge", "Parking", "Behind the barn").id
    with edb.session() as db:
        knowledge.decide(db, directory.Actor(ids["boss"]), directory.get_user(db, ids["boss"]), did, True)
    view = sources.sync(sid)
    assert view["push_error"] and view["last_error"] is None
    with edb.session() as db:
        hits = knowledge.search(db, directory.get_user(db, ids["anna"]), "barn")["results"]
    assert [h["title"] for h in hits] == ["Parking"]
