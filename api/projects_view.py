"""Projects page: the company's ClickUp projects in a PARA view, read live from ClickUp (read-only).

Reuses the project assistant of the clickup-personal plugin (``projects.py``: project map, visibility, status), so
the page and "How is project X?" in chat always agree. Only projects the map shows this person are read, with the
person's own ClickUp token (else the company token); nothing is written, editing stays in ClickUp.
"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from api.profiles import get_active_hermes_home

API = "https://api.clickup.com/api/v2"
CACHE_S = 300  # a saved read older than 5 minutes is refreshed in the background; "Refresh" reads at once
_cache: dict[str, tuple[float, dict]] = {}
_busy: set[str] = set()
_lock = threading.Lock()


def _projects_module():
    base = Path(os.environ.get("LISA_PLUGIN_SOURCE", "/opt/lisa/plugins"))
    spec = importlib.util.spec_from_file_location("lisa_projects", base / "clickup-personal" / "projects.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _env(home: Path) -> dict[str, str]:
    out = {}
    path = home / ".env"
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _email(home: Path) -> str:
    """The person's e-mail as the provisioner wrote it for their ClickUp access."""
    try:
        cfg = yaml.safe_load((home / "config.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return ""
    entries = (cfg.get("plugins") or {}).get("entries") or {}
    return str(((entries.get("clickup-personal") or {}).get("settings") or {}).get("account_email") or "")


def _getter(token: str):
    def get(path: str, params: dict) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": token}), timeout=30) as r:
            return json.loads(r.read().decode())
    return get


def _cache_file(home: Path) -> Path:
    return home / "cache" / "projects-page.json"  # not workspace/: that is the Files tab


def _saved(home: Path) -> tuple[float, dict] | None:
    """The last read: in memory, else from disk (survives a restart, so the page opens at once)."""
    key = str(home)
    with _lock:
        hit = _cache.get(key)
    if hit:
        return hit
    try:
        d = json.loads(_cache_file(home).read_text())
        hit = (float(d["saved"]), d["data"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    with _lock:
        _cache[key] = hit
    return hit


def _refresh_later(home: Path) -> None:
    key = str(home)
    with _lock:
        if key in _busy:
            return
        _busy.add(key)

    def run():
        try:
            _read(home)
        finally:
            with _lock:
                _busy.discard(key)
    threading.Thread(target=run, daemon=True).start()


def get_projects(refresh: bool = False, home: Path | None = None, get=None, check: bool = False) -> dict:
    """``check``: only whether this person has projects (decides if the page button shows; no ClickUp call).
    Otherwise the saved read comes back at once; when it is older than CACHE_S, ClickUp is read again in the
    background. ``refresh`` (the Refresh button) reads ClickUp now."""
    home = home or get_active_hermes_home()
    pj = _projects_module()
    mine = [p for p in pj.load_map(home) if pj.may_see(p, _email(home))]
    hit = _saved(home) if mine else None
    stale = not hit or time.time() - hit[0] >= CACHE_S
    if check:  # the web app just opened: bring the saved read up to date before the page is clicked
        if mine and stale and get is None:
            _refresh_later(home)
        return {"count": len(mine)}
    if not mine:
        return {"configured": bool(pj.load_map(home)), "projects": [], "areas": [], "archive": []}
    if hit and not refresh:
        if stale and get is None:
            _refresh_later(home)
        return {**hit[1], "refreshing": stale and get is None}
    return _read(home, get)


def _read(home: Path, get=None) -> dict:
    pj = _projects_module()
    mine = [p for p in pj.load_map(home) if pj.may_see(p, _email(home))]
    env = _env(home)
    token = env.get("CLICKUP_API_TOKEN") or env.get("CLICKUP_COMPANY_TOKEN")
    if not token and get is None:
        return {"configured": True, "error": "ClickUp is not connected for you yet (Organization -> My account).",
                "projects": [], "areas": [], "archive": []}
    get = get or _getter(token)
    live = [p for p in mine if p.get("para", "project") != "archive"]

    def one(p):
        try:
            return pj.project_status(get, p)
        except Exception as e:  # one broken list must not hide the others; never leak the token
            return {"project": p["name"], "para": p.get("para", "project"), "owner": p.get("owner"),
                    "error": f"ClickUp request failed ({type(e).__name__})"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, live))
    out = {"configured": True, "at": time.strftime("%d.%m. %H:%M"),
           "projects": [r for r in rows if r.get("para", "project") == "project"],
           "areas": [r for r in rows if r.get("para") == "area"],
           "archive": [{"project": p["name"], "owner": p.get("owner"),
                        "lists": [f"https://app.clickup.com/v/li/{x}" for x in p["lists"]]}
                       for p in mine if p.get("para") == "archive"]}
    now = time.time()
    with _lock:
        _cache[str(home)] = (now, out)
    try:  # best effort: without it the page only reads ClickUp again after a restart
        f = _cache_file(home)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"saved": now, "data": out}, ensure_ascii=False))
        tmp.replace(f)
    except OSError:
        pass
    return out
