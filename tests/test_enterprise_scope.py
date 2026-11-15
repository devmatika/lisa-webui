"""Lisa Enterprise request scope: route permissions, bound requests never read across profiles,
workspaces stay inside the user's own profile.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pytest


@dataclass
class _User:
    perms: frozenset

    def can(self, p):
        return p in self.perms


STAFF = _User(frozenset({"chat.use", "memory.own", "files.workspace", "cron.own", "share.create",
                         "integration.email", "knowledge.read", "knowledge.contribute"}))
OPERATOR = _User(frozenset({"operator.terminal", "operator.settings", "operator.profiles", "operator.mcp"}))


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/settings"), ("GET", "/api/terminal/output"), ("GET", "/api/escape/list"),
    ("POST", "/api/escape/authorize"), ("GET", "/api/mcp/servers"), ("GET", "/api/providers"),
    ("POST", "/api/profile/switch"), ("GET", "/api/git/status"), ("GET", "/api/kanban/boards"),
    ("GET", "/api/logs"), ("POST", "/api/default-model"), ("GET", "/api/never-heard-of-it"),
])
def test_staff_is_denied_operator_and_unknown_routes(method, path):
    from api.enterprise.policy import allowed
    assert not allowed(STAFF, method, path)


@pytest.mark.parametrize("method,path", [
    ("GET", "/"), ("GET", "/api/sessions"), ("POST", "/api/chat/start"), ("GET", "/api/memory"),
    ("GET", "/api/crons"), ("GET", "/api/file"), ("GET", "/api/settings"), ("GET", "/api/profiles"),
    ("GET", "/api/profile/active"), ("POST", "/api/share/create"), ("POST", "/api/enterprise/logout"),
])
def test_staff_keeps_chat_own_data_and_shell_reads(method, path):
    from api.enterprise.policy import allowed
    assert allowed(STAFF, method, path)


def test_operator_reaches_operator_routes():
    from api.enterprise.policy import allowed
    assert allowed(OPERATOR, "GET", "/api/escape/list") and allowed(OPERATOR, "POST", "/api/settings")


@pytest.fixture()
def bound(monkeypatch):
    from api import profiles
    profiles.bind_request_profile("u-anna")
    yield
    profiles.clear_request_profile()


def test_bound_request_never_gets_all_profiles(bound):
    from api.routes import _all_profiles_enabled, _request_wants_all_profiles_import
    assert _all_profiles_enabled(urlparse("/api/sessions?all_profiles=1")) is False
    assert _request_wants_all_profiles_import({"all_profiles": True, "profile": "u-ben"}) is False


def test_unbound_request_keeps_all_profiles_opt_in():
    from api.routes import _all_profiles_query_flag
    assert _all_profiles_query_flag(urlparse("/api/sessions?all_profiles=1")) is True


def test_bind_is_cleared_with_the_request():
    from api import profiles
    profiles.bind_request_profile("u-anna")
    profiles.clear_request_profile()
    assert profiles.request_profile_is_bound() is False


@pytest.fixture()
def enterprise_home(tmp_path, monkeypatch):
    """A bound enterprise request whose profile home is <tmp>/profiles/u-anna."""
    from api import profiles
    root = tmp_path / ".hermes"
    anna = root / "profiles" / "u-anna"
    ben = root / "profiles" / "u-ben"
    for p in (anna / "workspace" / "notes", ben / "workspace"):
        p.mkdir(parents=True)
    monkeypatch.setenv("LISA_ENTERPRISE", "1")
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: anna)
    profiles.bind_request_profile("u-anna")
    yield root, anna, ben
    profiles.clear_request_profile()


def test_enterprise_workspace_cannot_point_at_a_colleague_or_the_root(enterprise_home):
    from api.workspace import resolve_trusted_workspace, validate_workspace_to_add
    root, anna, ben = enterprise_home
    for bad in (ben, ben / "workspace", root, Path("/tmp")):
        with pytest.raises(ValueError):
            validate_workspace_to_add(str(bad))
        with pytest.raises(ValueError):
            resolve_trusted_workspace(str(bad))
    assert validate_workspace_to_add(str(anna / "workspace" / "notes")) == (anna / "workspace" / "notes").resolve()


def test_enterprise_default_workspace_is_the_users_own(enterprise_home):
    from api.workspace import get_last_workspace, resolve_trusted_workspace
    _, anna, _ = enterprise_home
    own = (anna / "workspace").resolve()
    assert resolve_trusted_workspace(None) == own
    assert Path(get_last_workspace()).resolve() == own


def test_voice_ticket_round_trip_and_tamper_resistance(monkeypatch):
    from api.enterprise import voice
    monkeypatch.setenv("LISA_VOICE_SECRET", "v" * 40)
    ticket = voice.mint("u-anna", "k-anna", "user-1")
    assert voice.verify(ticket)["p"] == "u-anna"
    body, sig = ticket.rsplit(".", 1)
    assert voice.verify(body + "." + "0" * len(sig)) is None
    assert voice.verify(voice.mint("u-anna", "k", "u", now=0)) is None  # expired
    monkeypatch.setenv("LISA_VOICE_SECRET", "short")
    with pytest.raises(RuntimeError):
        voice.mint("u-anna", "k", "u")
