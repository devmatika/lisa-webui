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
CACHE_S = 300  # ClickUp is read at most every 5 minutes per person; "Refresh" skips the cache
_cache: dict[str, tuple[float, dict]] = {}
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


def get_projects(refresh: bool = False, home: Path | None = None, get=None, check: bool = False) -> dict:
    """``check``: only whether this person has projects (decides if the page button shows; no ClickUp call)."""
    home = home or get_active_hermes_home()
    pj = _projects_module()
    mine = [p for p in pj.load_map(home) if pj.may_see(p, _email(home))]
    if check:  # the web app just opened: read ClickUp in the background so the page is ready when clicked
        hit = _cache.get(str(home))
        if mine and get is None and not (hit and time.time() - hit[0] < CACHE_S):
            threading.Thread(target=get_projects, kwargs={"home": home}, daemon=True).start()
        return {"count": len(mine)}
    if not mine:
        return {"configured": bool(pj.load_map(home)), "projects": [], "areas": [], "archive": []}
    key = str(home)
    with _lock:
        hit = _cache.get(key)
    if hit and not refresh and time.time() - hit[0] < CACHE_S:
        return hit[1]
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
    out = {"configured": True, "at": time.strftime("%H:%M"),
           "projects": [r for r in rows if r.get("para", "project") == "project"],
           "areas": [r for r in rows if r.get("para") == "area"],
           "archive": [{"project": p["name"], "owner": p.get("owner"),
                        "lists": [f"https://app.clickup.com/v/li/{x}" for x in p["lists"]]}
                       for p in mine if p.get("para") == "archive"]}
    with _lock:
        _cache[key] = (time.time(), out)
    return out
