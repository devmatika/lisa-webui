"""Company knowledge: a Markdown vault whose top-level folders are areas, permissions in Postgres.

Access is computed only from ``area_grants`` (user, role or department; read < contribute <
approve) together with the global ``knowledge.*`` permissions. Nothing in a document's frontmatter
grants anything, and being an administrator grants nothing either. Every function filters BEFORE
text leaves this module: unreadable documents never appear in results, titles or citations.

Teach Lisa: a contributor submits a draft (stored in Postgres, not in the vault). An approver of
that area publishes it to ``<area>/<slug>.md``; an earlier version is moved to ``.history/``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from api.enterprise.directory import Actor, DirectoryError, audit, permissions_of
from api.enterprise.models import (AreaGrant, Department, KnowledgeArea, KnowledgeDraft, KnowledgeGap, User,
                                   utcnow)

LEVELS = {"read": 1, "contribute": 2, "approve": 3}
_AREA_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SNIPPET = 400
MAX_READ = 20_000


def vault_root() -> Path:
    return Path(os.environ.get("LISA_KNOWLEDGE_DIR", "/opt/lisa/knowledge"))


@dataclass(frozen=True)
class Access:
    user_id: str
    levels: dict  # area_key -> highest level number


def access_for(db, user: User) -> Access:
    roles = {r.key for r in user.roles}
    perms = permissions_of(user)
    levels: dict[str, int] = {}
    if "knowledge.read" not in perms:
        return Access(user.id, {})
    dept = db.get(Department, user.department_id).name if user.department_id else None
    for g in db.scalars(select(AreaGrant)):
        match = ((g.principal_type == "user" and g.principal_id == user.id)
                 or (g.principal_type == "role" and g.principal_id in roles)
                 or (g.principal_type == "department" and dept is not None and g.principal_id == dept))
        if not match:
            continue
        level = LEVELS.get(g.level, 0)
        # the global permission caps what an area grant can give
        if level >= 2 and "knowledge.contribute" not in perms:
            level = 1
        if level >= 3 and "knowledge.approve" not in perms:
            level = 2 if "knowledge.contribute" in perms else 1
        levels[g.area_key] = max(levels.get(g.area_key, 0), level)
    return Access(user.id, levels)


def _area_dir(area: str) -> Path:
    if not _AREA_KEY.match(area or ""):
        raise DirectoryError("Unknown knowledge area")
    return (vault_root() / area).resolve()


def _resolve_doc(path: str) -> tuple[str, Path]:
    """``area/sub/doc.md`` -> (area, absolute path inside that area), refusing any escape."""
    parts = Path(str(path or "").strip().lstrip("/")).parts
    if len(parts) < 2 or any(p in ("..", ".") or p.startswith(".") for p in parts):
        raise DirectoryError("Not a knowledge document")
    area = parts[0]
    base = _area_dir(area)
    target = (base / Path(*parts[1:])).resolve()
    if base not in target.parents or target.suffix.lower() != ".md":
        raise DirectoryError("Not a knowledge document")
    return area, target


def _docs(area: str):
    base = _area_dir(area)
    if not base.is_dir():
        return
    for p in sorted(base.rglob("*.md")):
        rel = p.relative_to(base)
        if any(part.startswith(".") for part in rel.parts):
            continue  # .history and hidden files are not searchable
        resolved = p.resolve()
        if base in resolved.parents:  # a symlink out of the area is ignored
            yield f"{area}/{rel.as_posix()}", resolved


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
        m = re.match(r"^title:\s*(.+)$", line)
        if m:
            return m.group(1).strip().strip('"')
    return fallback


def _snippet_from_file(path: Path, query: str) -> tuple[str, str]:
    """Title + snippet from the live mirror file only (never from an external ranker payload)."""
    text = path.read_text(errors="replace")
    terms = [t for t in re.findall(r"\w{2,}", (query or "").lower())]
    low = text.lower()
    pos = min((low.find(t) for t in terms if t in low), default=0)
    snippet = text[max(0, pos - 120): pos + MAX_SNIPPET - 120].strip()
    return _title(text, path.stem), snippet


def _term_search(readable: list[str], query: str, limit: int) -> list[dict]:
    terms = [t for t in re.findall(r"\w{2,}", (query or "").lower())]
    hits = []
    for area in readable:
        for rel, path in _docs(area):
            text = path.read_text(errors="replace")
            low = text.lower()
            name = path.stem.lower()
            score = sum(low.count(t) + 3 * name.count(t) for t in terms)
            if not score:
                continue
            title, snippet = _snippet_from_file(path, query)
            hits.append({"path": rel, "area": area, "title": title, "snippet": snippet, "score": score})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def _ranked_with_local_snippets(ranked: list[dict], query: str, readable: set[str]) -> list[dict]:
    """Keep only hits whose area is allowed and whose file exists in the mirror right now."""
    out = []
    for hit in ranked:
        area = hit.get("area") or ""
        rel = hit.get("path") or ""
        if area not in readable:
            continue
        try:
            resolved_area, path = _resolve_doc(rel)
        except DirectoryError:
            continue
        if resolved_area != area or not path.is_file():
            continue
        title, snippet = _snippet_from_file(path, query)
        out.append({"path": rel, "area": area, "title": title, "snippet": snippet,
                    "score": hit.get("score", 0)})
    return out


def search(db, user: User, query: str, limit: int = 8) -> dict:
    access = access_for(db, user)
    readable = sorted(a for a, lv in access.levels.items() if lv >= 1)
    hits: list[dict] = []
    if readable and (query or "").strip():
        try:
            from api.enterprise import knowledge_fts
            if knowledge_fts.enabled():
                ranked = knowledge_fts.search_paths(readable, query, limit=max(limit, 8))
                hits = _ranked_with_local_snippets(ranked, query, set(readable))
        except Exception:
            hits = []
        if not hits:
            hits = _term_search(readable, query, limit)
    if not hits and query:
        db.add(KnowledgeGap(question=query[:2000], user_id=user.id))
    return {"results": hits[:limit], "searched_areas": readable}


MAX_TREE = 3000  # notes listed per area; search still covers all of them


def tree(db, user: User) -> dict:
    """The areas this person may read and their notes, for browsing (nothing else is listed)."""
    levels = access_for(db, user).levels
    names = {a.key: a.name for a in db.scalars(select(KnowledgeArea))}
    areas = []
    for key in sorted((k for k, lv in levels.items() if lv >= 1), key=lambda k: names.get(k, k).lower()):
        docs = []
        for rel, _path in _docs(key):
            if len(docs) >= MAX_TREE:
                break
            docs.append({"path": rel, "title": Path(rel).stem})
        areas.append({"key": key, "name": names.get(key, key), "level": {1: "read", 2: "contribute", 3: "approve"}[levels[key]],
                      "docs": docs, "truncated": len(docs) >= MAX_TREE})
    return {"areas": areas}


def read(db, user: User, path: str) -> dict:
    area, target = _resolve_doc(path)
    if access_for(db, user).levels.get(area, 0) < 1 or not target.is_file():
        raise DirectoryError("Document not found")  # same answer whether missing or forbidden
    text = target.read_text(errors="replace")
    return {"path": f"{area}/{target.relative_to(_area_dir(area)).as_posix()}", "title": _title(text, target.stem),
            "content": text[:MAX_READ], "truncated": len(text) > MAX_READ}


def submit_draft(db, actor: Actor, user: User, area: str, title: str, body: str) -> KnowledgeDraft:
    if access_for(db, user).levels.get(area, 0) < 2:
        raise DirectoryError("You cannot contribute to this knowledge area")
    if not (title or "").strip() or not (body or "").strip():
        raise DirectoryError("A draft needs a title and content")
    draft = KnowledgeDraft(area_key=area, title=title.strip()[:300], body=body, author_id=user.id)
    db.add(draft)
    db.flush()
    audit(db, actor, "knowledge.draft_submitted", draft.id, area=area, title=draft.title)
    return draft


def pending_for(db, user: User) -> list[KnowledgeDraft]:
    levels = access_for(db, user).levels
    return [d for d in db.scalars(select(KnowledgeDraft).where(KnowledgeDraft.status == "pending")
                                  .order_by(KnowledgeDraft.created_at))
            if levels.get(d.area_key, 0) >= 3]


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80] or "document"


def decide(db, actor: Actor, user: User, draft_id: str, approve: bool) -> KnowledgeDraft:
    draft = db.get(KnowledgeDraft, draft_id)
    if draft is None or draft.status != "pending":
        raise DirectoryError("Draft not found")
    if access_for(db, user).levels.get(draft.area_key, 0) < 3:
        raise DirectoryError("You cannot approve drafts in this area")
    if approve and draft.author_id == user.id:
        raise DirectoryError("A draft needs a second person to approve it")
    draft.status, draft.reviewer_id, draft.decided_at = ("approved" if approve else "rejected"), user.id, utcnow()
    if approve:
        base = _area_dir(draft.area_key)
        base.mkdir(parents=True, exist_ok=True)
        target = base / f"{_slug(draft.title)}.md"
        if target.exists():
            history = base / ".history"
            history.mkdir(exist_ok=True)
            target.rename(history / f"{target.stem}.{utcnow().strftime('%Y%m%dT%H%M%S')}.md")
        target.write_text(f"---\ntitle: {draft.title}\napproved_by: {user.email}\n"
                          f"approved_at: {draft.decided_at.isoformat()}\n---\n\n# {draft.title}\n\n{draft.body}\n")
        draft.published_path = f"{draft.area_key}/{target.name}"
    audit(db, actor, f"knowledge.draft_{draft.status}", draft.id, area=draft.area_key, path=draft.published_path)
    return draft


def save_area(db, actor: Actor, key: str, name: str, grants: list[dict]) -> KnowledgeArea:
    """Create/update an area and replace its grants. grants: [{principal_type, principal_id, level}]."""
    key = (key or "").strip().lower()
    if not _AREA_KEY.match(key):
        raise DirectoryError("Area key: lowercase letters, digits, - or _")
    for g in grants:
        if g.get("principal_type") not in ("user", "role", "department") or g.get("level") not in LEVELS \
                or not str(g.get("principal_id") or "").strip():
            raise DirectoryError("Each grant needs principal_type (user/role/department), principal_id and level")
    area = db.get(KnowledgeArea, key) or KnowledgeArea(key=key, name=name or key)
    area.name = (name or area.name).strip()
    db.add(area)
    for old in db.scalars(select(AreaGrant).where(AreaGrant.area_key == key)):
        db.delete(old)
    db.flush()
    for g in grants:
        db.add(AreaGrant(area_key=key, principal_type=g["principal_type"], principal_id=str(g["principal_id"]),
                         level=g["level"]))
    _area_dir(key).mkdir(parents=True, exist_ok=True)
    audit(db, actor, "knowledge.area_saved", key, grants=grants)
    return area


def delete_area(db, actor: Actor, key: str) -> None:
    """Remove an area, its grants/drafts, vault folder, and any Git-source folder mapping to it.

    Unticking a folder under Folders & access stops syncing but leaves the area row; use this to
    drop stale areas (e.g. legacy ``files`` / ``knowledge``) from Areas and access."""
    import json
    import shutil

    from api.enterprise import sources as knowledge_sources
    from api.enterprise.models import KnowledgeSource

    key = (key or "").strip().lower()
    area = db.get(KnowledgeArea, key)
    if area is None:
        raise DirectoryError("Area not found")
    unmapped: list[str] = []
    for src in db.scalars(select(KnowledgeSource)):
        folders = dict(src.folders or {})
        changed = False
        for folder, mapped in list(folders.items()):
            if mapped == key:
                folders[folder] = None
                changed = True
                unmapped.append(f"{src.name}:{folder or 'top level'}")
        if not changed:
            continue
        if not any(folders.values()):
            raise DirectoryError(
                f'"{area.name}" is the last folder Lisa reads from "{src.name}". '
                "Map another folder under Folders & access, or disconnect the vault first.")
        src.folders = folders
        mfile = knowledge_sources._manifest_path(src.id)
        if mfile.is_file():
            data = json.loads(mfile.read_text())
            if key in data:
                data.pop(key, None)
                mfile.write_text(json.dumps(data))
    db.delete(area)
    db.flush()
    shutil.rmtree(_area_dir(key), ignore_errors=True)
    audit(db, actor, "knowledge.area_removed", key, unmapped=unmapped)


MAX_IMPORT_FILES = 5000
MAX_IMPORT_BYTES = 100 * 1024 * 1024  # uncompressed Markdown
_SKIPPED_DIRS = {".obsidian", ".trash", ".history", ".git", "__MACOSX"}


def _folder_area_key(folder: str) -> str:
    """Area key for a vault folder; a name without Latin letters (e.g. Persian) gets a stable hash."""
    import hashlib
    key = re.sub(r"[^a-z0-9]+", "-", folder.lower()).strip("-")[:64].strip("-")
    return key or "area-" + hashlib.sha1(folder.encode()).hexdigest()[:8]


def import_vault(db, actor: Actor, area: str, zip_bytes: bytes, *, split_folders: bool = False,
                 new_area_grants: list[dict] | None = None) -> dict:
    """Unpack the Markdown notes of a zipped Obsidian vault (or any folder of .md files).

    Only ``.md`` files, folders kept, existing notes with the same path replaced. Who may read them is
    the area's grants, as for everything else in the vault. With ``split_folders`` every top-level
    folder becomes its own area (created with ``new_area_grants`` when it does not exist yet, so each
    folder can get its own access); notes at the top level go to ``area``."""
    import io
    import zipfile
    from pathlib import PurePosixPath
    area = (area or "").strip().lower()
    if db.get(KnowledgeArea, area) is None:
        raise DirectoryError("Area not found: create it first")
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise DirectoryError("That file is not a .zip archive") from exc
    notes = [i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith(".md")]
    if len(notes) > MAX_IMPORT_FILES:
        raise DirectoryError(f"Too many notes ({len(notes)}); at most {MAX_IMPORT_FILES} per import")
    if sum(i.file_size for i in notes) > MAX_IMPORT_BYTES:
        raise DirectoryError("The notes are larger than 100 MB uncompressed")
    parts = [PurePosixPath(i.filename.replace("\\", "/")).parts for i in notes]
    # a vault zipped as its folder ("Company/…") lands at the area's top, not one level down
    tops = {p[0] for p in parts if len(p) > 1}
    strip = 1 if parts and len(tops) == 1 and all(len(p) > 1 for p in parts) else 0
    per_area: dict[str, int] = {}
    skipped = 0
    for info, p in zip(notes, parts):
        rel = p[strip:]
        if not rel or any(x in ("..", "") or x.startswith(".") or x in _SKIPPED_DIRS for x in rel) \
                or PurePosixPath(info.filename).is_absolute():
            skipped += 1
            continue
        target_area = area
        if split_folders and len(rel) > 1:
            folder, rel = rel[0], rel[1:]
            target_area = _folder_area_key(folder)
            if db.get(KnowledgeArea, target_area) is None:
                save_area(db, actor, target_area, folder, list(new_area_grants or []))
        base = _area_dir(target_area)
        target = base.joinpath(*rel).resolve()
        if base not in target.parents:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(info))
        per_area[target_area] = per_area.get(target_area, 0) + 1
    imported = sum(per_area.values())
    audit(db, actor, "knowledge.imported", area, notes=imported, skipped=skipped, areas=per_area)
    return {"ok": True, "area": area, "imported": imported, "skipped": skipped, "areas": per_area}


def areas_view(db) -> list[dict]:
    grants = list(db.scalars(select(AreaGrant)))
    return [{"key": a.key, "name": a.name,
             "grants": [{"principal_type": g.principal_type, "principal_id": g.principal_id, "level": g.level}
                        for g in grants if g.area_key == a.key]}
            for a in db.scalars(select(KnowledgeArea).order_by(KnowledgeArea.key))]
