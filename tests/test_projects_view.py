"""Projects page (api.projects_view): PARA view of the company's ClickUp projects, read-only, only what the map allows."""

import json
from pathlib import Path

import api.projects_view as pv

PLUGINS = Path(__file__).resolve().parents[2] / "hermes-plugins"


def setup(tmp_path, monkeypatch, email="alex@firma.at"):
    monkeypatch.setenv("LISA_PLUGIN_SOURCE", str(PLUGINS))
    pv._cache.clear()
    home = tmp_path / "profiles" / "u-alex"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(f"plugins:\n  entries:\n    clickup-personal:\n      settings:\n        account_email: {email}\n")
    (home / ".env").write_text("CLICKUP_API_TOKEN=tok\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "project-map.json").write_text(json.dumps({"projects": [
        {"name": "Geniessmi", "lists": ["11"], "owner": "anna@firma.at", "visible_to": ["alex@firma.at"]},
        {"name": "Sales", "lists": ["21"], "para": "area", "visible_to": ["*"]},
        {"name": "Old shop", "lists": ["31"], "para": "archive", "visible_to": ["*"]},
        {"name": "Secret", "lists": ["41"], "owner": "boss@firma.at"}]}))
    return home


def test_para_view_reads_only_the_lists_this_person_may_see(tmp_path, monkeypatch):
    home = setup(tmp_path, monkeypatch)
    read = []

    def get(path, params):
        read.append(path)
        if params.get("include_closed"):  # nothing closed this week
            return {"tasks": [], "last_page": True}
        return {"tasks": [{"id": "1", "name": "Offer", "status": {"type": "open", "status": "to do"}}], "last_page": True}
    out = pv.get_projects(home=home, get=get)
    assert sorted(set(read)) == ["/list/11/task", "/list/21/task"]  # never the archive, never "Secret"
    assert [p["project"] for p in out["projects"]] == ["Geniessmi"] and out["projects"][0]["without_owner"]["count"] == 1
    assert [p["project"] for p in out["areas"]] == ["Sales"]
    assert out["archive"] == [{"project": "Old shop", "owner": None, "lists": ["https://app.clickup.com/v/li/31"]}]
    pv.get_projects(home=home, get=get)
    assert len(read) == 3  # project: open + changed this week; area: open only; then the saved read
    pv._cache.clear()  # a restart: the read saved on disk comes back without asking ClickUp
    again = pv.get_projects(home=home, get=get)
    assert len(read) == 3 and again["projects"][0]["project"] == "Geniessmi" and not again["refreshing"]
    pv.get_projects(home=home, get=get, refresh=True)
    assert len(read) == 6  # Refresh reads ClickUp now
    assert pv.get_projects(home=home, check=True) == {"count": 3}  # page button: no ClickUp call


def test_a_broken_list_does_not_hide_the_others_and_no_map_means_no_page(tmp_path, monkeypatch):
    home = setup(tmp_path, monkeypatch, email="boss@firma.at")

    def get(path, params):
        if path == "/list/41/task":
            raise OSError("down")
        return {"tasks": [], "last_page": True}
    out = pv.get_projects(home=home, get=get)
    assert out["projects"][0] == {"project": "Secret", "para": "project", "owner": "boss@firma.at",
                                  "error": "ClickUp request failed (OSError)"}
    (tmp_path / "config" / "project-map.json").unlink()
    assert pv.get_projects(home=home, check=True) == {"count": 0}


def test_a_failing_list_keeps_its_last_good_numbers_and_says_why_otherwise(tmp_path, monkeypatch):
    home = setup(tmp_path, monkeypatch)
    ok = lambda path, params: {"tasks": [], "last_page": True}
    pv.get_projects(home=home, get=ok)

    def busy(path, params):
        if path == "/list/11/task":
            raise pv.urllib.error.HTTPError(path, 429, "Too Many Requests", {}, None)
        return {"tasks": [], "last_page": True}
    out = pv.get_projects(home=home, get=busy, refresh=True)
    assert out["projects"][0]["project"] == "Geniessmi" and out["projects"][0]["stale"] and "error" not in out["projects"][0]
    pv._cache.clear()
    (home / "cache" / "projects-page.json").unlink()
    out = pv.get_projects(home=home, get=busy)
    assert out["projects"][0]["error"] == "ClickUp is busy, try Refresh in a minute"


def test_rate_limit_is_waited_out_then_reported(monkeypatch):
    calls, waits = [], []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"tasks": []}'

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise pv.urllib.error.HTTPError(req.full_url, 429, "busy", {"Retry-After": "2"}, None)
        return Resp()
    monkeypatch.setattr(pv.urllib.request, "urlopen", urlopen)
    assert pv._getter("tok", sleep=waits.append)("/list/1/task", {}) == {"tasks": []}
    assert len(calls) == 3 and waits == [2.0, 2.0]
    calls.clear()
    monkeypatch.setattr(pv.urllib.request, "urlopen", lambda req, timeout: (_ for _ in ()).throw(
        pv.urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)))
    try:
        pv._getter("tok", sleep=waits.append)("/list/9/task", {})
        assert False
    except pv.urllib.error.HTTPError as e:
        assert e.code == 404  # not retried
