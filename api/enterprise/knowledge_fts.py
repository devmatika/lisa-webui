"""SQLite FTS5 index for company knowledge Markdown (ranking only; ACL stays in knowledge.search).

The index lives next to the vault (``LISA_KNOWLEDGE_DIR/.lisa-fts.sqlite``). It is rebuilt when
any indexed note is newer than the stamp stored in the DB. Callers must still filter by
``access_for`` readable areas and build snippets from the live mirror files.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from pathlib import Path

# Set to "0" to force the classic term-count path in knowledge.search.
_ENV_ENABLE = "LISA_KNOWLEDGE_FTS"


def enabled() -> bool:
    return os.environ.get(_ENV_ENABLE, "1").strip().lower() not in ("0", "false", "no", "off")


def _db_path(vault: Path) -> Path:
    return vault / ".lisa-fts.sqlite"


def _connect(vault: Path) -> sqlite3.Connection:
    path = _db_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(
            path UNINDEXED,
            area UNINDEXED,
            title,
            body,
            tokenize = 'porter unicode61'
        );
        """
    )


def _vault_fingerprint(docs: list[tuple[str, str, Path]]) -> str:
    """Change detector over every (path, mtime, size): a note moved between same-length areas with
    `mv` keeps its mtime, so count + newest mtime + path lengths missed it."""
    h = hashlib.sha1()
    for rel, _area, path in sorted(docs):
        try:
            st = path.stat()
        except OSError:
            continue
        h.update(f"{rel}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    return h.hexdigest()


def _all_docs(list_docs) -> list[tuple[str, str, Path]]:
    """list_docs(area) -> iterable of (rel_path, Path); we need every area dir under vault."""
    from api.enterprise.knowledge import vault_root
    root = vault_root()
    out: list[tuple[str, str, Path]] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        area = child.name
        try:
            for rel, path in list_docs(area):
                out.append((rel, area, path))
        except Exception:
            continue
    return out


def rebuild_if_needed(list_docs) -> None:
    from api.enterprise.knowledge import vault_root, _title
    vault = vault_root()
    docs = _all_docs(list_docs)
    fingerprint = _vault_fingerprint(docs)
    conn = _connect(vault)
    try:
        _ensure_schema(conn)
        row = conn.execute("SELECT value FROM meta WHERE key = 'fingerprint'").fetchone()
        if row and row["value"] == fingerprint:
            return
        conn.execute("DELETE FROM notes")
        for rel, area, path in docs:
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            title = _title(text, path.stem)
            conn.execute(
                "INSERT INTO notes(path, area, title, body) VALUES (?, ?, ?, ?)",
                (rel, area, title, text),
            )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('fingerprint', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (fingerprint,),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('built_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(time.time()),),
        )
        conn.commit()
    finally:
        conn.close()


def _fts_query(raw: str) -> str | None:
    """Turn a user query into a safe FTS5 MATCH expression (OR of quoted tokens, ranked by bm25).

    AND made every word required, so a full question ("what did we agree on hosting") matched nothing.
    """
    import re
    terms = [t for t in re.findall(r"\w{2,}", (raw or "").lower())]
    if not terms:
        return None
    # Quote each token so FTS operators in user input cannot break the query.
    return " OR ".join('"' + t.replace('"', "") + '"' for t in dict.fromkeys(terms))


def search_paths(readable_areas: list[str], query: str, limit: int = 8) -> list[dict]:
    """Return ranked ``{path, area, title, score}`` for notes in ``readable_areas`` only.

    Does not return body/snippet text — the caller reads the live mirror file.
    """
    from api.enterprise.knowledge import vault_root, _docs
    if not readable_areas or not (query or "").strip():
        return []
    rebuild_if_needed(_docs)
    match = _fts_query(query)
    if not match:
        return []
    vault = vault_root()
    if not _db_path(vault).is_file():
        return []
    placeholders = ",".join("?" for _ in readable_areas)
    sql = (
        f"SELECT path, area, title, bm25(notes) AS score "
        f"FROM notes WHERE notes MATCH ? AND area IN ({placeholders}) "
        f"ORDER BY score LIMIT ?"
    )
    conn = _connect(vault)
    try:
        _ensure_schema(conn)
        rows = conn.execute(sql, (match, *readable_areas, max(1, limit))).fetchall()
    finally:
        conn.close()
    # bm25: lower is better in SQLite FTS5; invert for knowledge.search sort (higher better).
    return [
        {"path": r["path"], "area": r["area"], "title": r["title"], "score": -float(r["score"])}
        for r in rows
    ]
