"""Email auto-drafts settings for the WebUI (Email panel + setup wizard).

One shared config with the ``email_drafts.py`` cron job and Lisa's ``email-drafts`` skill: everything lives in
``<profile>/workspace/email-assistant/`` (config.json, status.json, learned.md, proposals.json). The browser
never writes files directly; every write goes through the validation here.

Never editable from the browser: the himalaya binary/config paths and the model (the cron job executes them).
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import ssl
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from api.paths import _atomic_write_text
from api.profiles import get_active_hermes_home

CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")
TEXT_FIELDS = ("description", "tone", "address", "greeting", "farewell", "do", "dont", "language")
LOCKED_KEYS = ("himalaya", "himalaya_config", "account", "model", "watch_seconds", "watch_period", "watch_every",
               "telegram_chats", "timezone", "open_days", "learn_every", "skip_from",
               "webui_url", "review_image", "review_ignore", "review_weekday", "provider_routing")  # set on the server (admin or Connect mailbox), never by the browser
MAX_CATEGORIES, MAX_RULES = 15, 50
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
PASS_ENV = "EMAIL_ASSISTANT_IMAP_PASSWORD"  # profile .env; the cron job's himalaya reads it via password.command
ACCOUNT = "mailbox"
MAILBOX_TOML = "himalaya/email-assistant.toml"  # relative to the profile (not in the workspace: not browser-readable)
FOLDER_PATTERNS = {
    "drafts": r"(^|[./])(drafts|entw(ü|ue)rfe|brouillons|bozze|borradores)$",
    "sent": r"(^|[./])(sent|sent messages|sent items|sent mail|gesendet|gesendete (objekte|elemente))$",
    "trash": r"(^|[./])(trash|papierkorb|deleted messages|deleted items|gelöschte (objekte|elemente))$",
}

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


def _own(path: Path, mode: int = 0o644) -> None:
    """The WebUI may run as root; the cron job runs as the profile owner and must read (and rewrite) these files."""
    try:
        st = get_active_hermes_home().stat()
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, mode)
    except OSError:
        pass


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _own(path.parent, 0o755)
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1))
    _own(path)


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
    _own(path)


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
        "configured": isinstance(cfg, dict) and bool(cfg.get("categories")),
        "config": public_cfg,
        "presets": PRESETS,
        "status": _read_json(d / "status.json", None),
        "learned": read_learned(d / "learned.md"),
        "proposals": _read_json(d / "proposals.json", {}),
        "cron": _cron_job(get_active_hermes_home()),
        "mailbox": _mailbox_status(cfg if isinstance(cfg, dict) else {}),
    }


def _mailbox_status(cfg: dict[str, Any]) -> dict[str, Any]:
    home = get_active_hermes_home()
    toml = home / str(cfg.get("himalaya_config") or "himalaya/config.toml")
    if not toml.is_file():
        return {"connected": False}
    return {"connected": True, "address": cfg.get("address", ""),
            "via": "wizard" if cfg.get("account") == ACCOUNT else "admin"}


def _toml_str(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string (validated input has no control chars)


def _mailbox_toml(host: str, port: int, user: str, name: str, folders: dict[str, str]) -> str:
    return "\n".join([
        f"# Written by the WebUI Email setup. The password is {PASS_ENV} in the profile .env, never in this file.",
        f"[accounts.{ACCOUNT}]",
        "default = true",
        f"email = {_toml_str(user)}",
        f"display-name = {_toml_str(name or user)}",
        'mailbox.alias.inbox = "INBOX"',
        f"mailbox.alias.drafts = {_toml_str(folders.get('drafts', 'Drafts'))}",
        f"mailbox.alias.sent = {_toml_str(folders.get('sent', 'Sent'))}",
        f"mailbox.alias.trash = {_toml_str(folders.get('trash', 'Trash'))}",
        f"imap.server = {_toml_str(f'imaps://{host}:{port}')}",
        f"imap.sasl.plain.username = {_toml_str(user)}",
        f'imap.sasl.plain.password.command = "printenv {PASS_ENV}"',
        "",
    ])


def _profile_env(home: Path) -> dict[str, str]:
    """The profile .env as a dict: an admin-made mailbox reads its password through a password.command from it."""
    out = {}
    try:
        for line in (home / ".env").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and re.match(r"^[A-Z_][A-Z0-9_]*$", key.strip()):
                out[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return out


def _himalaya(home: Path, toml: Path, password: str | None, args: list[str], account: str | None = ACCOUNT) -> str:
    binary = home / "bin" / "himalaya"
    if not binary.is_file():
        raise ValueError("The mail tool (himalaya) is not installed in this profile yet — ask your admin.")
    env = {**os.environ, **_profile_env(home), **({PASS_ENV: password} if password is not None else {})}
    try:
        r = subprocess.run([str(binary), "-c", str(toml), *(["-a", account] if account else []), *args],
                           capture_output=True, text=True, timeout=45, env=env, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError("The mail server did not answer within 45 s. Check server name and port.")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        hint = next((ln for ln in reversed(msg) if ln.strip()), "")
        # himalaya prints nothing for a rejected login (verified against Mittwald)
        raise ValueError(f"Could not connect: {hint[-200:]}" if hint else
                         "Login failed. Check the email address, password and IMAP server.")
    return r.stdout


def pick_folders(names: list[str]) -> dict[str, str]:
    out = {}
    for role, pattern in FOLDER_PATTERNS.items():
        hit = next((n for n in names if re.search(pattern, n, re.I)), None)
        if hit:
            out[role] = hit
    return out


# Where a provider's mail is not at imap.<domain>: the IMAP host for an MX base domain.
KNOWN_IMAP = {"google.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
              "outlook.com": "outlook.office365.com", "icloud.com": "imap.mail.me.com"}


def _mx_hosts(domain: str, timeout: float = 3.0) -> list[str]:
    """MX hosts of a domain, best first, with one plain UDP query to the system resolver (no extra library)."""
    try:
        ns = next(ln.split()[1] for ln in Path("/etc/resolv.conf").read_text().splitlines()
                  if ln.startswith("nameserver") and len(ln.split()) > 1)
    except (OSError, StopIteration):
        return []
    qid = random.randint(0, 0xFFFF)
    query = qid.to_bytes(2, "big") + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + b"".join(
        bytes([len(p)]) + p.encode("ascii") for p in domain.split(".")) + b"\x00\x00\x0f\x00\x01"
    try:
        with socket.socket(socket.AF_INET6 if ":" in ns else socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(query, (ns, 53))
            data = sock.recv(4096)
    except OSError:
        return []
    return parse_mx(data, qid)


def parse_mx(data: bytes, qid: int | None = None) -> list[str]:
    def name(pos: int) -> tuple[str, int]:
        labels, end, jumps = [], None, 0
        while data[pos]:
            if data[pos] >= 0xC0:  # compression pointer
                end = pos + 2 if end is None else end
                pos = int.from_bytes(data[pos:pos + 2], "big") & 0x3FFF
                jumps += 1
                if jumps > 20:
                    raise ValueError("loop")
                continue
            labels.append(data[pos + 1:pos + 1 + data[pos]].decode("ascii", "replace"))
            pos += 1 + data[pos]
        return ".".join(labels), (pos + 1 if end is None else end)

    try:
        if qid is not None and int.from_bytes(data[:2], "big") != qid:
            return []
        qd, an = int.from_bytes(data[4:6], "big"), int.from_bytes(data[6:8], "big")
        pos = 12
        for _ in range(qd):
            pos = name(pos)[1] + 4
        found = []
        for _ in range(an):
            pos = name(pos)[1]
            rtype, rdlen = int.from_bytes(data[pos:pos + 2], "big"), int.from_bytes(data[pos + 8:pos + 10], "big")
            pos += 10
            if rtype == 15:
                found.append((int.from_bytes(data[pos:pos + 2], "big"), name(pos + 2)[0].lower()))
            pos += rdlen
        return [h for _, h in sorted(found)]
    except (IndexError, ValueError):
        return []


def _imap_ok(host: str, port: int = 993, timeout: float = 4.0) -> bool:
    """An IMAPS server answers here with a certificate valid for this name (rules out a shared host's wrong cert)."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                tls.settimeout(timeout)
                return tls.recv(64).startswith(b"* OK")
    except (OSError, ssl.SSLError):
        return False


def discover_imap(user: str) -> str:
    """The IMAP server for an address: imap./mail.<domain>, else the mail host of its MX provider."""
    domain = user.rsplit("@", 1)[-1].lower()
    candidates = [f"imap.{domain}", f"mail.{domain}"]
    for mx in _mx_hosts(domain)[:2]:
        # shortcut: last two labels as the provider domain — wrong for co.uk-style suffixes; add a suffix list if seen.
        base = ".".join(mx.rstrip(".").split(".")[-2:])
        candidates += [KNOWN_IMAP.get(base, ""), f"imap.{base}", f"mail.{base}"]
    seen = set()
    for host in candidates:
        if host and host not in seen and HOST_RE.match(host):
            seen.add(host)
            if _imap_ok(host):
                return host
    return ""


def connect_imap(data: Any) -> dict[str, Any]:
    """Test an IMAP login read-only, find the Drafts/Sent folders, then save. Nothing is stored if the test fails;
    the password goes only into the profile .env and is never returned. No SMTP: this feature never sends."""
    if not isinstance(data, dict):
        raise ValueError("mailbox data missing")
    # Mailbox actions share this endpoint (routes.py lists it): {"action": "suggest"} proposes categories.
    if data.get("action") == "suggest":
        return suggest_categories()
    user = _text(data.get("user"), "email", 200)
    if not EMAIL_RE.match(user):
        raise ValueError("email: the full mailbox address")
    host = _text(data.get("host"), "server", 253) or discover_imap(user)
    if not host:
        raise ValueError("Could not find the mail server for this address. Open “Server settings” and enter it "
                         "(your provider calls it IMAP server, e.g. mail.example.com).")
    if not HOST_RE.match(host):
        raise ValueError("server: a host name like mail.example.com")
    port = 993 if data.get("port") in (None, "") else int(data["port"])
    if not 1 <= port <= 65535:
        raise ValueError("port: 1–65535")
    password = str(data.get("password") or "")
    if not password or len(password) > 300 or any(c in password for c in "\r\n\0"):
        raise ValueError("password missing")
    name = _text(data.get("name"), "name", 100)

    home = get_active_hermes_home()
    toml = home / MAILBOX_TOML
    toml.parent.mkdir(parents=True, exist_ok=True)
    trial = toml.with_name(toml.name + ".test")
    try:  # test with a trial file: a failed attempt never replaces a working mailbox config
        _atomic_write_text(trial, _mailbox_toml(host, port, user, name, {}))
        listing = json.loads(_himalaya(home, trial, password, ["mailbox", "list", "--json"]) or "{}")
        folders = pick_folders([m.get("name") or m.get("id") or "" for m in listing.get("mailboxes", [])])
        if "drafts" not in folders:
            raise ValueError("Connected, but no Drafts folder was found in this mailbox.")
        _atomic_write_text(trial, _mailbox_toml(host, port, user, name, folders))
        _himalaya(home, trial, password, ["envelope", "list", "-m", "inbox", "-s", "1", "--json"])  # read-only probe
    except Exception:
        trial.unlink(missing_ok=True)
        raise
    trial.replace(toml)
    _own(toml)
    from api.providers import _write_env_file  # shared, locked .env writer
    _write_env_file(home / ".env", {PASS_ENV: password})

    path = _dir() / "config.json"
    cfg = _read_json(path, {})
    cfg = cfg if isinstance(cfg, dict) else {}
    cfg.update({"account": ACCOUNT, "himalaya_config": MAILBOX_TOML, "address": user})
    if name:
        cfg["name"] = name
    if not cfg.get("categories"):  # connected before the wizard finished: keep it a valid draft config
        cfg.setdefault("drafts", False)
    _write_json(path, cfg)
    out = get_email_assistant()
    out["connected_folders"] = folders
    out["connected_host"] = host
    return out


SUGGEST_SYSTEM = """You set up an email assistant that sorts a person's incoming email into categories and writes
reply drafts in a tone per category. From the senders and subjects of their recent inbox, propose 4 to 7 categories
that fit THIS mailbox. Return JSON only: {"categories": {"<name>": {"description": "...", "tone": "...",
"address": "Sie|du|", "draft": true|false}}}. Rules: name = 1-2 lowercase English words joined by - (e.g. customer,
job-applications); description = which emails belong here, in plain words a classifier can apply (one sentence, no
names of people); tone = a few words; address = "du" or "Sie" only if clearly so, else ""; draft = false for mail that
never needs a written reply (invoices, notifications, newsletters). Always include "other" for anything else.
Ignore newsletters and automatic notifications unless they are a large part of the mail."""


def _openrouter(home: Path, system: str, user: str) -> dict[str, Any]:
    key = _profile_env(home).get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise ValueError("No AI key in this profile yet — ask your admin.")
    body = {"model": "deepseek/deepseek-v4-flash-0731", "temperature": 0.2, "max_tokens": 1500,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # same routing as the cron job: providers that neither store nor train on prompts, none in China
            "provider": {"data_collection": "deny", "require_parameters": True,
                         "ignore": ["baidu", "alibaba", "siliconflow", "streamlake", "deepseek"]}}
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            text = json.loads(r.read().decode())["choices"][0]["message"]["content"] or ""
        return json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip()).strip())
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise ValueError(f"Lisa could not make suggestions right now ({str(exc)[:80]}). Try again in a minute.")


def clean_suggestions(raw: Any) -> dict[str, dict[str, Any]]:
    """Model output -> categories that pass validate_config (bad names and fields are dropped, never trusted)."""
    cats = raw.get("categories") if isinstance(raw, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for name, card in (cats.items() if isinstance(cats, dict) else []):
        name = re.sub(r"[^a-z0-9_-]+", "-", str(name).strip().lower()).strip("-")[:31]
        if not CATEGORY_RE.match(name) or not isinstance(card, dict) or len(out) >= MAX_CATEGORIES - 1:
            continue
        c = {f: str(card[f]).strip()[:300] for f in ("description", "tone") if isinstance(card.get(f), str) and card[f].strip()}
        if card.get("address") in ("Sie", "du"):
            c["address"] = card["address"]
        if card.get("draft") is False:
            c["draft"] = False
        out[name] = c
    out.setdefault("other", dict(PRESETS["other"]))
    return out


def suggest_categories() -> dict[str, Any]:
    """Look at the senders and subjects of the last 60 inbox mails (no bodies) and propose categories for them."""
    home = get_active_hermes_home()
    cfg = _read_json(_dir() / "config.json", {})
    cfg = cfg if isinstance(cfg, dict) else {}
    toml = home / str(cfg.get("himalaya_config") or "himalaya/config.toml")
    if not toml.is_file():
        raise ValueError("Connect your mailbox first.")
    listing = json.loads(_himalaya(home, toml, None, ["envelope", "list", "-m", "inbox", "-s", "60", "--json"],
                                   account=cfg.get("account") or None) or "[]")
    envs = listing.get("envelopes", []) if isinstance(listing, dict) else listing
    lines = []
    for e in envs if isinstance(envs, list) else []:
        sender = e.get("from") if isinstance(e, dict) else None  # himalaya: [{"name": ..., "email": ...}]
        sender = sender[0] if isinstance(sender, list) and sender else sender
        sender = sender.get("email") or sender.get("name") if isinstance(sender, dict) else sender
        lines.append(f"- {str(sender or '')[:80]} | {str(e.get('subject') or '')[:120]}")
    if len(lines) < 5:
        raise ValueError("Your inbox has too few emails for suggestions yet — the ready-made categories are a good start.")
    who = f"Mailbox owner: {cfg.get('name') or ''} <{cfg.get('address') or ''}>. {cfg.get('owner') or ''}".strip()
    cats = clean_suggestions(_openrouter(home, SUGGEST_SYSTEM, f"{who}\n\nRecent inbox (sender | subject):\n" + "\n".join(lines)))
    return {"categories": cats, "based_on": len(lines)}


def _text(value: Any, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise ValueError(f"{field} must be text")
    text = str(value).strip()
    if len(text) > limit:
        raise ValueError(f"{field} is too long (max {limit} characters)")
    return text


def _url(value: Any) -> str:
    url = _text(value, "mail_url", 300)
    if url and not re.match(r"^https://[^\s<>\"']+$", url):
        raise ValueError("mail_url must start with https://")
    return url


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
        if r.get("task") in (True, False):  # ClickUp: always / never (absent = Lisa decides by priority)
            rule["task"] = r["task"]
        for field, label in (("task_list", "ClickUp list id"), ("task_assignee", "ClickUp user id")):
            value = str(r.get(field) or "").strip()
            if value:
                if not value.isdigit() or len(value) > 20:
                    raise ValueError(f"rule '{rule[key]}': {label} must be digits")
                rule[field] = value
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
    daily_at = str(data.get("daily_at") if data.get("daily_at") is not None else "07:30").strip()
    if daily_at and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", daily_at):
        raise ValueError("daily summary time: HH:MM, e.g. 07:30 (empty = off)")
    t_in = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
    tasks = {"enabled": t_in.get("enabled") is True, "list_id": str(t_in.get("list_id") or "").strip(),
             "assignee": str(t_in.get("assignee") or "").strip(),
             "min_priority": t_in.get("min_priority") if t_in.get("min_priority") in ("high", "normal") else "high"}
    if tasks["list_id"] and not tasks["list_id"].isdigit() or tasks["assignee"] and not tasks["assignee"].isdigit():
        raise ValueError("ClickUp list and user: the numeric IDs")
    if tasks["enabled"] and not tasks["list_id"]:
        raise ValueError("ClickUp tasks: enter the list ID")
    review_days = int(data.get("review_every_days") if data.get("review_every_days") is not None else 7)
    if not 0 <= review_days <= 30:
        raise ValueError("weekly review: 0 (off) to 30 days")
    return {
        "review_every_days": review_days,
        "address": address,
        "name": _text(data.get("name"), "name", 100),
        "owner": _text(data.get("owner"), "owner", 400),
        "facts": _text(data.get("facts"), "facts", 2000),
        "mail_url": _url(data.get("mail_url")),
        "only_from": only_from,
        "drafts": data.get("drafts") is True,
        "labels": data.get("labels") is not False,  # category label on each sorted inbox mail (default on)
        "paused": data.get("paused") is True,  # kill switch: the cron job does not even read the mailbox
        "notify": data.get("notify") if data.get("notify") in ("actionable", "all", "off") else "actionable",
        "max_per_run": max_per_run,
        "daily_at": daily_at,
        "tasks": tasks,
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
