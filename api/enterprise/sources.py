"""Knowledge sources: a Git repository (typically the company's Obsidian vault) mirrored into areas.

The organisation connects its vault once (setup wizard or Knowledge page): Lisa reads the
repository, every top-level folder becomes an area, and the admin decides per folder whether it is
included and which roles may read it. Lisa pulls on a schedule and mirrors Markdown into the areas.
With ``write_back``, sync is bidirectional: approved Teach-Lisa notes and Knowledge-only orphans are
committed back into the vault (for ``file://`` mounts, into the working tree when it is writable).
Who may read what is always the area's grants (``knowledge.py``); nothing in the repository grants
anything.

Access to the repository (preferred on Docker servers first):
- ``file://`` below ``LOCAL_SOURCES`` (bind-mount the host clone under
  ``/opt/lisa/sources/<name>``). Prefer a writable mount when ``write_back`` is on. Ops may still
  push with host cron / ``obsidian-vault-sync`` when the container cannot reach the Git remote.
- SSH (``git@gitlab.com:org/vault.git``): Lisa generates a deploy key; give it write access when
  ``write_back`` is on. Keys live under the knowledge volume.
- HTTPS: an access token (read, or write when ``write_back`` is on).

Secrets and clones live under ``<vault>/.sources/`` (a dot folder: never searchable).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from api.enterprise import secretbox
from api.enterprise.directory import Actor, DirectoryError, audit
from api.enterprise.knowledge import (_SKIPPED_DIRS, MAX_IMPORT_BYTES, MAX_IMPORT_FILES, _area_dir, save_area,
                                      vault_root)
from api.enterprise.models import KnowledgeArea, KnowledgeDraft, KnowledgeSource, User, utcnow

logger = logging.getLogger(__name__)

LOCAL_SOURCES = Path(os.environ.get("LISA_LOCAL_SOURCES_DIR", "/opt/lisa/sources"))
GIT_TIMEOUT = 300
_SSH_URL = re.compile(r"^(ssh://[\w.@:/~-]+|[\w.-]+@[\w.-]+:[\w./~-]+)$")
_HTTPS_URL = re.compile(r"^https://[\w.-]+(:\d+)?/[\w./~%-]+$")
_BRANCH = re.compile(r"^[\w./-]{1,200}$")
_CRED = re.compile(r"^[a-f0-9]{32}$")
# Folders a vault usually keeps for Obsidian / deliverables rather than as Knowledge areas
# (standard PARA areas are suggested; Inbox/Templates/Archive/files stay ops-only).
_DEFAULT_EXCLUDED = re.compile(
    r"(^|-)(templates?|archive|inbox|trash|attachments?|files|company-files)$", re.I)
_locks: dict[str, threading.Lock] = {}
_again: set[str] = set()


# ── storage ──────────────────────────────────────────────────────────────────

def _sources_dir() -> Path:
    d = vault_root() / ".sources"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _cred_dir(credential_id: str) -> Path:
    if not _CRED.match(credential_id or ""):
        raise DirectoryError("Unknown credential")
    return _sources_dir() / "credentials" / credential_id


def _repo_dir(source_id: str) -> Path:
    return _sources_dir() / "repos" / source_id


def new_deploy_key() -> dict:
    """A fresh SSH key pair for one source; the public half goes into GitLab/GitHub as a read-only
    deploy key."""
    import secrets
    cred = secrets.token_hex(16)
    d = _cred_dir(cred)
    d.mkdir(parents=True, mode=0o700)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lisa-knowledge",
                    "-f", str(d / "deploy_key")], check=True, timeout=30, capture_output=True)
    secretbox.upgrade_file(d / "deploy_key")
    return {"credential_id": cred, "public_key": (d / "deploy_key.pub").read_text().strip()}


def store_token(token: str, credential_id: str | None = None) -> str:
    import secrets
    cred = credential_id or secrets.token_hex(16)
    d = _cred_dir(cred)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    secretbox.write(d / "token", token.strip().encode())
    return cred


# ── git ──────────────────────────────────────────────────────────────────────

def _check_url(url: str) -> str:
    url = (url or "").strip()
    if _SSH_URL.match(url) or _HTTPS_URL.match(url):
        return url
    if url.startswith("file://"):
        local = Path(url[len("file://"):]).resolve()
        if local == LOCAL_SOURCES.resolve() or LOCAL_SOURCES.resolve() not in local.parents:
            raise DirectoryError(f"A folder on the server must be inside {LOCAL_SOURCES} (mounted into the container)")
        return "file://" + str(local)
    raise DirectoryError(
        "Use a mounted folder (file:///opt/lisa/sources/…), SSH (git@…), or HTTPS (https://…)")


def _git(args: list[str], *, credential_id: str | None, cwd: Path | None = None) -> str:
    gitconfig = _sources_dir() / "gitconfig"
    if not gitconfig.is_file():
        # a vault folder mounted from the host belongs to another uid; git only honours
        # safe.directory from a global config (the local clone's upload-pack ignores -c)
        gitconfig.write_text("[safe]\n\tdirectory = *\n")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(_sources_dir()),
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(gitconfig), "LC_ALL": "C"}
    config = ["-c", "protocol.ext.allow=never", "-c", "safe.directory=*", "-c", "core.hooksPath=/dev/null",
              "-c", "core.symlinks=false"]
    with tempfile.TemporaryDirectory(prefix="lisa-git-") as scratch:
        if credential_id:
            d = _cred_dir(credential_id)
            if (d / "deploy_key").is_file():
                # the key is encrypted at rest; ssh gets a plain copy only for this one command
                key = Path(scratch) / "key"
                fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(secretbox.read(d / "deploy_key"))
                env["GIT_SSH_COMMAND"] = (f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes "
                                          f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={d / 'known_hosts'}")
            if (d / "token").is_file():
                basic = base64.b64encode(b"oauth2:" + secretbox.read(d / "token").strip()).decode()
                config += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
        try:
            r = subprocess.run(["git", *config, *args], cwd=cwd, env=env, capture_output=True, text=True,
                               timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise DirectoryError("The repository did not answer in time") from exc
    if r.returncode:
        raise DirectoryError(_explain(r.stderr, local=any(a.startswith("file://") for a in args),
                                      push="push" in args))
    return r.stdout.strip()


def _explain(stderr: str, local: bool = False, push: bool = False) -> str:
    """Git's error, in words an admin can act on (secrets never appear in stderr)."""
    s = stderr.lower()
    if local and ("could not read from remote" in s or "does not appear to be a git repository" in s):
        return "That folder on the server is not a Git repository Lisa can read."
    if push and ("permission denied" in s or "403" in s or "401" in s or "not allowed" in s
                 or "write access" in s or "protected branch" in s or "could not read from remote" in s):
        return ("Lisa may only read this repository: give its deploy key write access (or use a token "
                "with write access), or allow pushes to this branch.")
    if push and ("rejected" in s or "non-fast-forward" in s or "fetch first" in s):
        return "Someone changed the vault at the same moment; Lisa tries again on the next sync."
    if "permission denied (publickey)" in s or "could not read from remote" in s:
        return "Access denied: add Lisa's deploy key to the repository (read-only) and try again."
    if "authentication failed" in s or "403" in s or "401" in s:
        return "Access denied: the access token is wrong, expired or has no read access."
    if "remote branch" in s and "not found" in s:
        return "That branch does not exist in the repository."
    if "repository not found" in s or "does not appear to be a git repository" in s or "not found" in s:
        return "Repository not found: check the address."
    if "could not resolve host" in s:
        return "The server name in the address cannot be reached."
    tail = [ln for ln in stderr.strip().splitlines() if ln.strip()][-1:] or ["unknown error"]
    return "Git: " + tail[0][:300]


def _origin_url(target: Path) -> str:
    try:
        return _git(["remote", "get-url", "origin"], credential_id=None, cwd=target).strip()
    except DirectoryError:
        return ""


def _fetch(url: str, branch: str, credential_id: str | None, target: Path) -> str:
    """Clone or update ``target`` to the branch's newest commit (shallow); returns the commit.

    If the existing clone's ``origin`` no longer matches ``url`` (e.g. switching from GitLab SSH to
    a mounted ``file://`` vault), re-clone so a stale remote cannot keep failing with deploy-key
    errors after a restart."""
    if not _BRANCH.match(branch or ""):
        raise DirectoryError("Branch: letters, digits, . / - or _")
    url = _check_url(url)
    reuse = (target / ".git").is_dir() and _origin_url(target) == url
    if reuse:
        _git(["fetch", "--depth", "1", "origin", branch], credential_id=credential_id, cwd=target)
        _git(["reset", "--hard", "FETCH_HEAD"], credential_id=credential_id, cwd=target)
        _git(["clean", "-fdx"], credential_id=credential_id, cwd=target)
    else:
        shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--depth", "1", "--single-branch", "--branch", branch, url, str(target)],
             credential_id=credential_id)
    return _git(["rev-parse", "HEAD"], credential_id=credential_id, cwd=target)


def _notes(base: Path):
    """Markdown notes below ``base`` (relative paths), skipping hidden and Obsidian-internal folders
    and anything that resolves outside ``base``."""
    root = base.resolve()
    for p in sorted(base.rglob("*.md")):
        rel = p.relative_to(base)
        if any(x.startswith(".") or x in _SKIPPED_DIRS for x in rel.parts) or p.is_symlink():
            continue
        if root in p.resolve().parents and p.is_file():
            yield rel


def _pdfs(base: Path):
    """PDFs below ``base`` with the same rules as ``_notes``."""
    root = base.resolve()
    for p in sorted(base.rglob("*")):
        if p.suffix.lower() != ".pdf":
            continue
        rel = p.relative_to(base)
        if any(x.startswith(".") or x in _SKIPPED_DIRS for x in rel.parts) or p.is_symlink():
            continue
        if root in p.resolve().parents and p.is_file():
            yield rel


MAX_PDF_PAGES, MAX_PDF_CHARS = 200, 400_000


def pdf_markdown(data: bytes, vault_path: str) -> str | None:
    """A PDF as a searchable note: one ``## Page N`` section per page, so search results can name the page.
    None when the PDF library is missing or the file has no extractable text (scans need OCR)."""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [(i + 1, (pg.extract_text() or "").strip()) for i, pg in enumerate(reader.pages[:MAX_PDF_PAGES])]
    except Exception:  # no pypdf, encrypted or broken PDF: it simply stays unsearchable
        return None
    if not any(text for _, text in pages):
        return None
    name = Path(vault_path).name
    body = "\n\n".join(f"## Page {n}\n\n{text}" for n, text in pages if text)[:MAX_PDF_CHARS]
    digest = hashlib.sha1(data).hexdigest()
    return (f"---\ntitle: {json.dumps(name)}\ntype: pdf\nsource: {json.dumps(vault_path)}\npdf_sha1: {digest}\n---\n\n"
            f"# {name}\n\n{body}\n")


def _area_key_for(folder: str) -> str:
    """'03-Projects' -> 'projects'; standard folders keep their stable Lisa area keys."""
    import hashlib
    from api.enterprise.knowledge_defaults import STANDARD_FOLDERS
    for std_folder, key, *_ in STANDARD_FOLDERS:
        if folder == std_folder:
            return key
    # legacy rename: old vaults still named 06-Knowledge map to the same area
    if folder in ("06-Knowledge", "Knowledge"):
        return "knowledge"
    bare = re.sub(r"^\d+[\s._-]*", "", folder)
    key = re.sub(r"[^a-z0-9]+", "-", bare.lower()).strip("-")[:60].strip("-")
    return key or "area-" + hashlib.sha1(folder.encode()).hexdigest()[:8]


def _area_name_for(folder: str) -> str:
    from api.enterprise.knowledge_defaults import STANDARD_FOLDERS
    for std_folder, _key, name, *_ in STANDARD_FOLDERS:
        if folder == std_folder:
            return name
    if folder in ("06-Knowledge", "Knowledge"):
        return "Company Knowledge"
    bare = re.sub(r"^\d+[\s._-]*", "", folder).replace("-", " ").replace("_", " ").strip()
    return bare or folder


def preview(url: str, branch: str, credential_id: str | None) -> dict:
    """Read the repository once and describe its folders, so the admin can choose per folder."""
    import secrets
    url = _check_url(url)
    tmp = _sources_dir() / "preview" / secrets.token_hex(8)
    try:
        commit = _fetch(url, branch or "main", credential_id, tmp)
        folders = []
        for d in sorted(p for p in tmp.iterdir() if p.is_dir() and not p.name.startswith(".")):
            n = sum(1 for _ in _notes(d))
            folders.append({"folder": d.name, "notes": n, "key": _area_key_for(d.name), "name": _area_name_for(d.name),
                            "suggested": n > 0 and not _DEFAULT_EXCLUDED.search(d.name)})
        root_notes = sum(1 for p in tmp.glob("*.md") if p.is_file() and not p.is_symlink())
        return {"ok": True, "commit": commit[:12], "branch": branch or "main", "folders": folders,
                "root_notes": root_notes}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── sources ──────────────────────────────────────────────────────────────────

def view(src: KnowledgeSource) -> dict:
    return {"id": src.id, "name": src.name, "url": src.url, "branch": src.branch, "folders": src.folders,
            "interval_minutes": src.interval_minutes, "notes": src.notes, "last_commit": (src.last_commit or "")[:12],
            "last_sync_at": src.last_sync_at.isoformat() if src.last_sync_at else None, "last_error": src.last_error,
            "auth": _auth_kind(src.credential_id), "credential_id": src.credential_id,
            "public_key": _public_key(src.credential_id), "write_back": bool(src.write_back),
            "push_error": src.push_error}


def _auth_kind(credential_id: str | None) -> str:
    if not credential_id:
        return "none"
    d = _cred_dir(credential_id)
    return "ssh" if (d / "deploy_key").is_file() else "token" if (d / "token").is_file() else "none"


def _public_key(credential_id: str | None) -> str | None:
    if credential_id and (_cred_dir(credential_id) / "deploy_key.pub").is_file():
        return (_cred_dir(credential_id) / "deploy_key.pub").read_text().strip()
    return None


def _kept_grants(db, key: str, readers: set[str]) -> list[dict]:
    """What an area already grants beyond plain reading survives a vault save: Teach-Lisa contributors
    whose role still reads it, administrators, and grants to single people or departments."""
    from api.enterprise.models import AreaGrant
    kept = []
    for g in db.scalars(select(AreaGrant).where(AreaGrant.area_key == key)):
        role = g.principal_type == "role"
        if not role or g.principal_id == "admin" or (g.level != "read" and g.principal_id in readers):
            kept.append({"principal_type": g.principal_type, "principal_id": g.principal_id, "level": g.level})
    return kept


def save_source(db, actor: Actor, *, source_id: str | None, name: str, url: str, branch: str,
                credential_id: str | None, folders: list[dict], interval_minutes: int = 15,
                write_back: bool = True) -> KnowledgeSource:
    """Create or update a source and its areas.

    folders: [{folder ("" = repository root), include, key, name, readers: [role keys]}]. Included
    folders become (or update) areas readable by ``readers``; administrators may approve."""
    url = _check_url(url)
    if credential_id:
        _cred_dir(credential_id)  # validates the id
    src = db.get(KnowledgeSource, source_id) if source_id else None
    if source_id and src is None:
        raise DirectoryError("Source not found")
    mapping: dict[str, str | None] = {}
    for f in folders:
        folder = str(f.get("folder") or "")
        if "/" in folder or folder.startswith("."):
            raise DirectoryError(f"Not a top-level folder: {folder}")
        if not f.get("include"):
            mapping[folder] = None
            continue
        key = (f.get("key") or _area_key_for(folder or "general")).strip().lower()
        readers = {r for r in f.get("readers") or [] if r != "admin"}
        grants = _kept_grants(db, key, readers)
        held = {g["principal_id"] for g in grants if g["principal_type"] == "role"}
        grants += [{"principal_type": "role", "principal_id": r, "level": "read"} for r in sorted(readers - held)]
        if "admin" not in held:
            grants.append({"principal_type": "role", "principal_id": "admin", "level": "approve"})
        save_area(db, actor, key, f.get("name") or _area_name_for(folder or "General"), grants)
        mapping[folder] = key
    if not any(mapping.values()):
        raise DirectoryError("Choose at least one folder for Lisa to read")
    if src is None and db.scalar(select(KnowledgeSource).where(KnowledgeSource.url == url)) is not None:
        raise DirectoryError('This vault is already connected: use "Folders & access" to change it')
    if src is None:
        src = KnowledgeSource(name=(name or "Company vault").strip(), url=url, branch=branch or "main",
                              credential_id=credential_id, folders=mapping, notes=0)
        db.add(src)
    else:
        src.name, src.url, src.branch, src.folders = (name or src.name).strip(), url, branch or src.branch, mapping
        if credential_id:
            src.credential_id = credential_id
    src.interval_minutes = max(15, min(int(interval_minutes or 15), 24 * 60))
    if src.write_back and not write_back:
        src.push_error = None
    src.write_back = bool(write_back)
    db.flush()
    audit(db, actor, "knowledge.source_saved", src.id, url=url, branch=src.branch,
          folders={k: v for k, v in mapping.items()}, write_back=src.write_back)
    return src


def delete_source(db, actor: Actor, source_id: str) -> None:
    src = db.get(KnowledgeSource, source_id)
    if src is None:
        raise DirectoryError("Source not found")
    _mirror(src, None)  # removes every note this source put into the areas
    shutil.rmtree(_repo_dir(src.id), ignore_errors=True)
    if src.credential_id:
        shutil.rmtree(_cred_dir(src.credential_id), ignore_errors=True)
    db.delete(src)
    audit(db, actor, "knowledge.source_removed", source_id, url=src.url)


def _manifest_path(source_id: str) -> Path:
    return _sources_dir() / "manifests" / f"{source_id}.json"


def _mirror(src: KnowledgeSource, repo: Path | None) -> int:
    """Copy the mapped folders' notes into their areas; remove what this source put there before and
    no longer provides. Notes from elsewhere (e.g. approved Teach-Lisa drafts) are never touched."""
    mpath = _manifest_path(src.id)
    old = json.loads(mpath.read_text()) if mpath.is_file() else {}
    new: dict[str, list[str]] = {}
    total = size = 0
    for folder, area in (src.folders or {}).items() if repo is not None else ():
        if not area:
            continue
        base = repo / folder if folder else repo
        if not base.is_dir():
            continue
        rels = [r for r in _notes(base)] if folder else [p.relative_to(repo) for p in sorted(repo.glob("*.md"))
                                                          if p.is_file() and not p.is_symlink()]
        dest = _area_dir(area)
        for rel in rels:
            total += 1
            if total > MAX_IMPORT_FILES:
                raise DirectoryError(f"More than {MAX_IMPORT_FILES} notes: leave out some folders")
            data = (base / rel).read_bytes()
            size += len(data)
            if size > MAX_IMPORT_BYTES:
                raise DirectoryError("The chosen folders hold more than 100 MB of notes: leave out some folders")
            target = (dest / rel).resolve()
            if dest not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
            new.setdefault(area, []).append(rel.as_posix())
        for rel in _pdfs(base) if folder else ():  # PDFs: a text copy "<name>.pdf.md", rebuilt only when the PDF changes
            target = (dest / (rel.as_posix() + ".md")).resolve()
            if dest not in target.parents:
                continue
            data = (base / rel).read_bytes()
            if target.is_file() and f"pdf_sha1: {hashlib.sha1(data).hexdigest()}" in target.read_text(errors="replace")[:600]:
                new.setdefault(area, []).append(rel.as_posix() + ".md")
                continue
            note = pdf_markdown(data, f"{folder}/{rel.as_posix()}")
            if note is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(note)
            new.setdefault(area, []).append(rel.as_posix() + ".md")
    for area, rels in old.items():
        keep = set(new.get(area, []))
        dest = _area_dir(area)
        for rel in rels:
            if rel not in keep:
                (dest / rel).unlink(missing_ok=True)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(new))
    return sum(len(v) for v in new.values())


def _file_origin_path(url: str) -> Path | None:
    if not (url or "").startswith("file://"):
        return None
    return Path(url[len("file://"):]).resolve()


def _writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".lisa-write-probe"
        probe.write_text("")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _is_bare_git(path: Path) -> bool:
    """Bare repos have HEAD at the root and no ``.git`` directory (unlike a working tree)."""
    return (path / "HEAD").is_file() and not (path / ".git").is_dir()


def _vault_commit_cwd(src: KnowledgeSource, repo: Path) -> Path:
    """Writable file:// working tree when possible; otherwise the sync clone (SSH/HTTPS/bare)."""
    origin = _file_origin_path(src.url)
    if origin is not None and not _is_bare_git(origin) and _writable_dir(origin):
        return origin
    return repo


def _publish_relpaths(src: KnowledgeSource, repo: Path, relpaths: list[str], *,
                      message: str, author: str, body: str | None = None) -> bool:
    """Commit paths already written under ``repo``. For writable ``file://`` origins, mirror into
    that working tree and commit there (Obsidian checkout). Otherwise commit+push from the clone."""
    if not relpaths:
        return False
    cwd = _vault_commit_cwd(src, repo)
    into_origin = cwd.resolve() != repo.resolve()
    if into_origin:
        for rel in relpaths:
            src_f = repo / rel
            if not src_f.is_file():
                continue
            dst = cwd / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = src_f.read_bytes()
            if not dst.is_file() or dst.read_bytes() != data:
                dst.write_bytes(data)
    for rel in relpaths:
        _git(["add", "--", rel], credential_id=None, cwd=cwd)
    if not _git(["status", "--porcelain", "--", *relpaths], credential_id=None, cwd=cwd):
        return False
    cmd = ["-c", "user.name=Lisa", "-c", "user.email=lisa@localhost", "commit", "-q",
           "--author", author, "-m", message]
    if body:
        cmd += ["-m", body]
    _git(cmd, credential_id=None, cwd=cwd)
    try:
        _git(["push", "-q", "origin", f"HEAD:refs/heads/{src.branch}"],
             credential_id=src.credential_id, cwd=cwd)
    except DirectoryError:
        if not into_origin:
            raise  # remote clone must push; file:// working-tree commit is enough without push
        logger.info("Knowledge write-back committed in %s; remote push deferred", cwd)
    return True


def _push_approved(db, src: KnowledgeSource, repo: Path) -> int:
    """Commit every approved Teach-Lisa note of this source's areas not yet in the vault (one commit
    per note, authored by the person who wrote it). Returns the number published."""
    folder_of = {area: folder for folder, area in (src.folders or {}).items() if area}
    drafts = list(db.scalars(select(KnowledgeDraft).where(
        KnowledgeDraft.status == "approved", KnowledgeDraft.pushed_at.is_(None),
        KnowledgeDraft.area_key.in_(list(folder_of))).order_by(KnowledgeDraft.decided_at)))
    done = 0
    for d in drafts:
        area, _, rel = (d.published_path or "").partition("/")
        local = _area_dir(area) / rel
        if not rel or not local.is_file():
            d.pushed_at = utcnow()  # replaced or removed since: nothing to write
            continue
        base = repo / folder_of[area] if folder_of[area] else repo
        target = (base / rel).resolve()
        if repo.resolve() not in target.parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(local.read_bytes())
        relpath = target.relative_to(repo.resolve()).as_posix()
        author, reviewer = db.get(User, d.author_id), db.get(User, d.reviewer_id) if d.reviewer_id else None
        who = f"{(author.display_name if author else 'Lisa').strip() or 'Lisa'} <{author.email if author else 'lisa@localhost'}>"
        _publish_relpaths(src, repo, [relpath], message=f"Lisa: {d.title}", author=who,
                          body=f"Taught in Lisa and approved by {reviewer.email if reviewer else 'an approver'}.")
        d.pushed_at = utcnow()
        done += 1
    return done


def _push_orphans(src: KnowledgeSource, repo: Path) -> int:
    """Copy Markdown that exists only in Knowledge (not in the vault clone) back into the vault."""
    folder_of = {area: folder for folder, area in (src.folders or {}).items() if area}
    relpaths: list[str] = []
    root = repo.resolve()
    for area, folder in folder_of.items():
        area_dir = _area_dir(area)
        if not area_dir.is_dir():
            continue
        base = (repo / folder) if folder else repo
        for path in sorted(area_dir.rglob("*.md")):
            rel = path.relative_to(area_dir)
            if any(part.startswith(".") or part in _SKIPPED_DIRS for part in rel.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            target = (base / rel).resolve()
            if root not in target.parents:
                continue
            if target.is_file():
                continue  # vault already has it; vault wins on content
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            relpaths.append(target.relative_to(root).as_posix())
    if not relpaths:
        return 0
    _publish_relpaths(src, repo, relpaths, author="Lisa <lisa@localhost>",
                      message="Lisa: sync Knowledge notes into the company vault")
    return len(relpaths)


def push_soon(area_key: str) -> None:
    """After a note is approved: sync (and so push) the sources that write this area back."""
    from api.enterprise import db as edb
    with edb.session() as db:
        ids = [s.id for s in db.scalars(select(KnowledgeSource).where(KnowledgeSource.write_back.is_(True)))
               if area_key in (s.folders or {}).values()]
    for sid in ids:
        sync_in_background(sid)


def sync(source_id: str) -> dict:
    """Pull the repository and mirror it. Safe to call from any thread; one sync per source at a time."""
    from api.enterprise import db as edb
    lock = _locks.setdefault(source_id, threading.Lock())
    if not lock.acquire(blocking=False):
        _again.add(source_id)  # e.g. a note approved during a sync: run once more right after it
        return {"ok": False, "running": True}
    try:
        _again.discard(source_id)
        with edb.session() as db:
            src = db.get(KnowledgeSource, source_id)
            if src is None:
                raise DirectoryError("Source not found")
            try:
                commit = _fetch(src.url, src.branch, src.credential_id, _repo_dir(src.id))
                if src.write_back:
                    try:
                        repo = _repo_dir(src.id)
                        wrote = _push_approved(db, src, repo) + _push_orphans(src, repo)
                        if wrote:
                            cwd = _vault_commit_cwd(src, repo)
                            commit = _git(["rev-parse", "HEAD"], credential_id=None, cwd=cwd)
                        src.push_error = None
                    except (DirectoryError, OSError, subprocess.SubprocessError) as exc:
                        src.push_error = str(exc)
                        logger.warning("Writing Knowledge notes back to source %s failed: %s", source_id, exc)
                notes = _mirror(src, _repo_dir(src.id))
                src.last_commit, src.notes, src.last_error = commit, notes, None
            except (DirectoryError, OSError, subprocess.SubprocessError) as exc:
                src.last_error = str(exc)
                logger.warning("Knowledge source %s failed to sync: %s", source_id, exc)
            src.last_sync_at = utcnow()
            audit(db, Actor(None, "sync"), "knowledge.source_synced", source_id,
                  commit=(src.last_commit or "")[:12], notes=src.notes, error=src.last_error,
                  push_error=src.push_error)
            return view(src)
    finally:
        lock.release()
        if source_id in _again:
            sync_in_background(source_id)


def sync_in_background(source_id: str) -> None:
    threading.Thread(target=sync, args=(source_id,), name=f"lisa-source-{source_id[:8]}", daemon=True).start()


def due(db, now=None) -> list[str]:
    now = now or utcnow()
    return [s.id for s in db.scalars(select(KnowledgeSource))
            if s.last_sync_at is None or s.last_sync_at + timedelta(minutes=s.interval_minutes) <= now]


def start_scheduler(poll_seconds: int = 60) -> None:
    """Pull every source on its interval (default hourly) for as long as the WebUI runs."""
    from api.enterprise import db as edb

    def loop() -> None:
        while True:
            try:
                with edb.session() as db:
                    ids = due(db)
                for sid in ids:
                    sync(sid)
            except Exception:
                logger.exception("Knowledge source scheduler pass failed")
            time.sleep(poll_seconds)
    threading.Thread(target=loop, name="lisa-knowledge-sources", daemon=True).start()


def areas_from_sources(db) -> dict[str, str]:
    """area key -> source name, so the Knowledge page can mark synced areas."""
    out = {}
    for s in db.scalars(select(KnowledgeSource)):
        for area in (s.folders or {}).values():
            if area and db.get(KnowledgeArea, area) is not None:
                out[area] = s.name
    return out
