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
                      "06-Company-Knowledge/Holidays.md": "# Holidays\nTwo weeks ahead",
                      "90-Templates/Meeting.md": "# Template",
                      "10-Company-Files/offers/note.md": "# Offer draft\nnot knowledge",
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
    assert set(by) == {"01-People", "03-Projects", "06-Company-Knowledge", "90-Templates", "10-Company-Files"} and pre["root_notes"] == 1
    assert by["03-Projects"]["key"] == "projects" and by["03-Projects"]["name"] == "Projects"
    assert by["06-Company-Knowledge"]["suggested"] and not by["90-Templates"]["suggested"] and not by["10-Company-Files"]["suggested"]
    folders = [{"folder": "06-Company-Knowledge", "include": True, "readers": ["staff", "manager"]},
               {"folder": "03-Projects", "include": True, "readers": ["manager"]},
               {"folder": "", "include": True, "key": "general", "name": "General", "readers": ["staff", "manager"]},
               {"folder": "90-Templates", "include": False}]
    with edb.session() as db:
        src = sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="Matika vault", url=url,
                                  branch="main", credential_id=None, folders=folders, write_back=False)
        sid = src.id
    assert sources.sync(sid)["notes"] == 3 and sources.sync(sid)["last_error"] is None
    with pytest.raises(directory.DirectoryError, match="already connected"), edb.session() as db:
        sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="again", url=url, branch="main",
                            credential_id=None, folders=folders, write_back=False)

    def titles(who, q):
        with edb.session() as db:
            return sorted(h["title"] for h in knowledge.search(db, directory.get_user(db, ids[who]), q)["results"])
    assert titles("anna", "holidays") == ["Holidays"]
    assert titles("anna", "budget") == [] and titles("mia", "budget") == ["Hotel pilot"]  # projects: managers only
    assert titles("mia", "template") == []  # left out
    # a note approved in Lisa lives next to the mirrored ones and survives every sync
    (knowledge.vault_root() / "knowledge" / "approved-in-lisa.md").write_text("# Parking\nBehind the barn")
    os.remove(repo / "06-Company-Knowledge" / "Holidays.md")
    (repo / "06-Company-Knowledge" / "Sub").mkdir()
    (repo / "06-Company-Knowledge" / "Sub" / "Expenses.md").write_text("# Expenses\nReceipts monthly")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "edit")
    assert sources.sync(sid)["notes"] == 3
    assert titles("anna", "holidays") == [] and titles("anna", "receipts") == ["Expenses"]
    assert titles("anna", "barn") == ["Parking"]
    with edb.session() as db:  # disconnecting removes what the vault brought, nothing else
        sources.delete_source(db, directory.Actor(ids["boss"]), sid)
    assert titles("anna", "receipts") == [] and titles("anna", "barn") == ["Parking"]


def test_switching_source_url_reclones_instead_of_fetching_stale_origin(ent, tmp_path):
    """Restart / reconnect: changing git@… → file:// must not keep failing on the old deploy key."""
    from api.enterprise import directory, sources
    edb, ids, repo = ent
    other = tmp_path / "sources" / "other"
    (other / "06-Company-Knowledge").mkdir(parents=True)
    (other / "06-Company-Knowledge" / "New.md").write_text("# New\nfrom file mount")
    _git(other, "init", "-q", "-b", "main")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "other")
    with edb.session() as db:
        sid = sources.save_source(
            db, directory.Actor(ids["boss"]), source_id=None, name="v", url=f"file://{repo}",
            branch="main", credential_id=None,
            folders=[{"folder": "06-Company-Knowledge", "include": True, "readers": ["staff"]}]).id
    assert sources.sync(sid)["last_error"] is None
    from api.enterprise.models import KnowledgeSource
    with edb.session() as db:
        db.get(KnowledgeSource, sid).url = f"file://{other}"
    view = sources.sync(sid)
    assert view["last_error"] is None
    assert (sources._repo_dir(sid) / "06-Company-Knowledge" / "New.md").is_file()
    assert sources._origin_url(sources._repo_dir(sid)) == f"file://{other.resolve()}"


def test_a_failed_pull_is_reported_in_plain_words(ent, tmp_path):
    from api.enterprise import directory, sources
    edb, ids, repo = ent
    with pytest.raises(directory.DirectoryError, match="branch does not exist"):
        sources.preview(f"file://{repo}", "no-such-branch", None)
    with edb.session() as db:
        sid = sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="v", url=f"file://{repo}",
                                  branch="main", credential_id=None,
                                  folders=[{"folder": "06-Company-Knowledge", "include": True, "readers": ["staff"]}]).id
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
    folders = [{"folder": "06-Company-Knowledge", "include": True, "readers": ["staff", "manager"]},
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
                                  folders=[{"folder": "06-Company-Knowledge", "include": True, "readers": ["staff"],
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
    shown = subprocess.run(["git", "show", "main:06-Company-Knowledge/parking.md"], cwd=bare, capture_output=True,
                           text=True, check=True).stdout
    assert "Behind the barn" in shown
    before = subprocess.run(["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True).stdout
    sources.sync(sid)  # pushed once only
    assert subprocess.run(["git", "rev-parse", "main"], cwd=bare, capture_output=True, text=True).stdout == before


def test_write_back_commits_into_a_writable_file_vault_working_tree(ent):
    """file:// Obsidian checkouts: commit in place (push to a non-bare branch often fails)."""
    from api.enterprise import directory, knowledge, sources
    edb, ids, repo = ent
    with edb.session() as db:
        sid = sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="v", url=f"file://{repo}",
                                  branch="main", credential_id=None, write_back=True,
                                  folders=[{"folder": "06-Company-Knowledge", "include": True, "readers": ["staff"]}]).id
        knowledge.save_area(db, directory.Actor(ids["boss"]), "knowledge", "Knowledge", [
            {"principal_type": "role", "principal_id": "staff", "level": "contribute"},
            {"principal_type": "role", "principal_id": "admin", "level": "approve"}])
        did = knowledge.submit_draft(db, directory.Actor(ids["anna"]), directory.get_user(db, ids["anna"]),
                                     "knowledge", "Parking", "Behind the barn").id
    with edb.session() as db:
        knowledge.decide(db, directory.Actor(ids["boss"]), directory.get_user(db, ids["boss"]), did, True)
    view = sources.sync(sid)
    assert view["last_error"] is None
    assert (repo / "06-Company-Knowledge" / "parking.md").is_file()
    assert "Behind the barn" in (repo / "06-Company-Knowledge" / "parking.md").read_text()


def test_knowledge_only_notes_are_written_back_to_the_vault(ent):
    """Orphans that landed only in Knowledge (e.g. old import) sync into the mapped vault folder."""
    from api.enterprise import directory, knowledge, sources
    edb, ids, repo = ent
    with edb.session() as db:
        sid = sources.save_source(
            db, directory.Actor(ids["boss"]), source_id=None, name="v", url=f"file://{repo}",
            branch="main", credential_id=None, write_back=True,
            folders=[{"folder": "03-Projects", "include": True, "readers": ["manager"]}]).id
    sources.sync(sid)
    orphan = knowledge.vault_root() / "projects" / "Geniessmi Project--04a9b18f"
    (orphan / "Knowledge").mkdir(parents=True)
    (orphan / "Project.md").write_text("# Geniessmi\nOnly in Knowledge before write-back\n")
    (orphan / "Knowledge" / "overview.md").write_text("# Overview\nProduct notes\n")
    view = sources.sync(sid)
    assert view["last_error"] is None and view["push_error"] is None
    assert (repo / "03-Projects" / "Geniessmi Project--04a9b18f" / "Project.md").is_file()
    assert "Only in Knowledge" in (repo / "03-Projects" / "Geniessmi Project--04a9b18f" / "Project.md").read_text()
    assert (repo / "03-Projects" / "Geniessmi Project--04a9b18f" / "Knowledge" / "overview.md").is_file()


def _two_page_pdf(page1: str, page2: str) -> bytes:
    """A minimal real PDF with one line of text per page (no PDF library needed to write it)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>"]
    for text in (page1, page2):
        n = len(objs) + 1
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {n + 1} 0 R "
                    f"/Resources << /Font << /F1 7 0 R >> >> >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_pdfs_become_searchable_and_answers_name_the_page(ent):
    pytest.importorskip("pypdf")
    from api.enterprise import directory, knowledge, sources
    edb, ids, repo = ent
    (repo / "06-Company-Knowledge" / "Offer.pdf").write_bytes(_two_page_pdf("Welcome to MATIKA", "Total price 480 EUR"))
    (repo / "06-Company-Knowledge" / "Policy.md").write_text("# Policy\n\n## Travel\n\nTrains first.\n\n## Laptops\n\nReturn laptops on the last day.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "pdf")
    folders = [{"folder": "06-Company-Knowledge", "include": True, "readers": ["staff", "manager"]}]
    with edb.session() as db:
        sid = sources.save_source(db, directory.Actor(ids["boss"]), source_id=None, name="v", url=f"file://{repo}",
                                  branch="main", credential_id=None, folders=folders, write_back=False).id
    sources.sync(sid)

    def hit(q):
        with edb.session() as db:
            return knowledge.search(db, directory.get_user(db, ids["anna"]), q)["results"][0]
    pdf = hit("480")
    assert pdf["section"] == "Page 2" and pdf["cite"] == "Offer.pdf, page 2"
    md = hit("laptops")
    assert md["section"] == "Laptops" and md["cite"] == "Policy, Laptops" and md["line"] >= 7
    copy = knowledge.vault_root() / pdf["path"]
    before = copy.stat().st_mtime_ns
    sources.sync(sid)  # unchanged PDF: not extracted again
    assert copy.stat().st_mtime_ns == before
