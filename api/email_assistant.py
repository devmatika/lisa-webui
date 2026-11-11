"""Email auto-drafts settings for the WebUI (Email panel + setup wizard).

One shared config with the ``email_drafts.py`` cron job and Lisa's ``email-drafts`` skill: everything lives in
``<profile>/workspace/email-assistant/`` (config.json, status.json, learned.md, proposals.json). The browser
never writes files directly; every write goes through the validation here.

Never editable from the browser: the himalaya binary/config paths and the model (the cron job executes them).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from api.paths import _atomic_write_text
from api.profiles import get_active_hermes_home

CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")
TEXT_FIELDS = ("description", "tone", "address", "greeting", "farewell", "do", "dont", "language")
LOCKED_KEYS = ("himalaya", "himalaya_config", "model", "watch_seconds", "watch_every")  # admin, server only
MAX_CATEGORIES, MAX_RULES = 15, 50

PRESETS: dict[str, dict[str, Any]] = {
    "customer": {"description": "Existing customer", "tone": "friendly, concrete, solution first"},
    "prospect": {"description": "New lead or sales enquiry", "tone": "warm and short, propose a next step"},
    "support": {"description": "Support request or problem report", "tone": "calm, helpful, clear next steps"},
    "partner": {"description": "Supplier or partner", "tone": "direct and collegial"},
    "internal": {"description": "Team and colleagues", "tone": "short and casual", "address": "du"},
    "admin": {"description": "Invoices, contracts, authorities", "tone": "formal and exact", "address": "Sie",
              "draft": False},
    "other": {"description": "Anything else", "tone": "polite and short"},
}


def _dir() -> Path:
    return get_active_hermes_home() / "workspace" / "email-assistant"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1))


def read_learned(path: Path) -> dict[str, str]:
    """Same format the cron script reads: ``## <category>`` followed by note lines."""
    notes: dict[str, list[str]] = {}
    cat = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        if line.startswith("## "):
            cat = line[3:].strip()
            notes[cat] = []
        elif cat and line.strip():
            notes[cat].append(line)
    return {c: "\n".join(v) for c, v in notes.items() if v}


def write_learned(path: Path, notes: dict[str, str]) -> None:
    out = ["# What Lisa learned about your email replies", "",
           "Active style notes per category. Edit in the web UI (Email → Learned) or here.", ""]
    for cat in sorted(notes):
        if notes[cat].strip():
            out += [f"## {cat}", notes[cat].strip(), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, "\n".join(out))


def _cron_job(home: Path) -> dict[str, Any] | None:
    raw = _read_json(home / "cron" / "jobs.json", {})
    jobs = raw.get("jobs", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    job = next((j for j in jobs if isinstance(j, dict) and j.get("name") == "email-drafts"), None)
    if not job:
        return None
    return {"enabled": bool(job.get("enabled")), "schedule": (job.get("schedule") or {}).get("display")
            if isinstance(job.get("schedule"), dict) else job.get("schedule"),
            "last_run_at": job.get("last_run_at"), "last_status": job.get("last_status")}


def get_email_assistant() -> dict[str, Any]:
    d = _dir()
    cfg = _read_json(d / "config.json", None)
    public_cfg = {k: v for k, v in (cfg or {}).items() if k not in LOCKED_KEYS}
    return {
        "configured": isinstance(cfg, dict),
        "config": public_cfg,
        "presets": PRESETS,
        "status": _read_json(d / "status.json", None),
        "learned": read_learned(d / "learned.md"),
        "proposals": _read_json(d / "proposals.json", {}),
        "cron": _cron_job(get_active_hermes_home()),
    }


def _text(value: Any, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise ValueError(f"{field} must be text")
    text = str(value).strip()
    if len(text) > limit:
        raise ValueError(f"{field} is too long (max {limit} characters)")
    return text


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("config must be an object")
    address = _text(data.get("address"), "address", 200)
    if not EMAIL_RE.match(address):
        raise ValueError("address must be an email address")
    cats_in = data.get("categories")
    if not isinstance(cats_in, dict) or not cats_in:
        raise ValueError("choose at least one category")
    if len(cats_in) > MAX_CATEGORIES:
        raise ValueError(f"at most {MAX_CATEGORIES} categories")
    cats: dict[str, dict[str, Any]] = {}
    for name, card in cats_in.items():
        name = str(name).strip().lower()
        if not CATEGORY_RE.match(name):
            raise ValueError(f"category name '{name}': use lowercase letters, digits, - or _")
        if not isinstance(card, dict):
            raise ValueError(f"category {name}: settings must be an object")
        clean = {f: _text(card.get(f), f"{name}.{f}", 300) for f in TEXT_FIELDS if card.get(f) not in (None, "")}
        if card.get("max_words") not in (None, ""):
            words = int(card["max_words"])
            if not 10 <= words <= 1000:
                raise ValueError(f"category {name}: max words between 10 and 1000")
            clean["max_words"] = words
        if card.get("draft") is False:
            clean["draft"] = False
        cats[name] = clean
    rules = []
    for r in data.get("rules") or []:
        if not isinstance(r, dict):
            raise ValueError("each rule must be an object")
        key = "from" if r.get("from") else "subject" if r.get("subject") else ""
        if not key:
            raise ValueError("a rule needs 'from' or 'subject'")
        rule: dict[str, Any] = {key: _text(r[key], f"rule {key}", 100)}
        if r.get("skip") is True:
            rule["skip"] = True
        elif r.get("category") in cats:
            rule["category"] = r["category"]
        else:
            raise ValueError(f"rule '{rule[key]}': choose a category or skip")
        rules.append(rule)
    if len(rules) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} rules")
    max_per_run = int(data.get("max_per_run") or 8)
    if not 1 <= max_per_run <= 30:
        raise ValueError("max_per_run between 1 and 30")
    only_from = data.get("only_from") or []
    if not isinstance(only_from, list) or len(only_from) > 20:
        raise ValueError("only_from: a list of at most 20 senders")
    only_from = [t for t in (_text(x, "only_from", 100) for x in only_from) if t]
    account = _text(data.get("account"), "account", 60)
    if account and not re.match(r"^[A-Za-z0-9_.-]+$", account):
        raise ValueError("account: letters, digits, . _ - only")
    return {
        "account": account,
        "address": address,
        "name": _text(data.get("name"), "name", 100),
        "owner": _text(data.get("owner"), "owner", 400),
        "facts": _text(data.get("facts"), "facts", 2000),
        "only_from": only_from,
        "drafts": data.get("drafts") is True,
        "max_per_run": max_per_run,
        "categories": cats,
        "rules": rules,
    }


def save_email_assistant_config(data: Any) -> dict[str, Any]:
    clean = validate_config(data)
    path = _dir() / "config.json"
    old = _read_json(path, {})
    for key in LOCKED_KEYS:  # keep the server-side settings the browser may not change
        if isinstance(old, dict) and key in old:
            clean[key] = old[key]
    _write_json(path, clean)
    return get_email_assistant()


def resolve_proposal(category: Any, action: Any, notes: Any = None) -> dict[str, Any]:
    """approve: proposal (optionally edited) -> active notes in learned.md; reject: drop it."""
    d = _dir()
    props = _read_json(d / "proposals.json", {})
    category = str(category or "")
    if category not in props:
        raise ValueError("no suggestion for this category")
    if action not in ("approve", "reject"):
        raise ValueError("action must be approve or reject")
    if action == "approve":
        learned = read_learned(d / "learned.md")
        text = _text(notes, "notes", 1200) if notes not in (None, "") else str(props[category].get("notes", ""))
        learned[category] = text
        write_learned(d / "learned.md", learned)
    del props[category]
    _write_json(d / "proposals.json", props)
    return get_email_assistant()


def save_learned_notes(category: Any, notes: Any) -> dict[str, Any]:
    """Edit or clear the active notes of one category (empty text = forget them)."""
    category = str(category or "")
    if not CATEGORY_RE.match(category):
        raise ValueError("invalid category")
    d = _dir()
    learned = read_learned(d / "learned.md")
    text = _text(notes, "notes", 1200)
    if text:
        learned[category] = text
    else:
        learned.pop(category, None)
    write_learned(d / "learned.md", learned)
    return get_email_assistant()
