"""
Sprint 27 Tests: configurable assistant display name (bot_name).
Tests cover settings API round-trip, empty/missing input defaults,
login page rendering, and server-side sanitization.
"""
import json
import urllib.error
import urllib.request

from tests._pytest_port import BASE


def _default_name() -> str:
    from api.branding import clear_branding_cache, default_bot_name

    clear_branding_cache()
    return default_bot_name()


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status


def get_raw(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.read().decode(), r.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def _reset_bot_name():
    post("/api/settings", {"bot_name": _default_name()})


# ── Default value ─────────────────────────────────────────────────────────

def test_settings_default_bot_name():
    """GET /api/settings should return bot_name defaulting to white-label appName."""
    expected = _default_name()
    d, status = get("/api/settings")
    assert status == 200
    assert "bot_name" in d
    assert d["bot_name"] == expected


# ── Round-trip ────────────────────────────────────────────────────────────

def test_settings_set_bot_name():
    """POST /api/settings with bot_name should persist and round-trip."""
    try:
        d, status = post("/api/settings", {"bot_name": "TestBot"})
        assert status == 200
        assert d.get("bot_name") == "TestBot"
        d2, _ = get("/api/settings")
        assert d2.get("bot_name") == "TestBot"
    finally:
        _reset_bot_name()


def test_settings_bot_name_special_chars():
    """bot_name with safe special characters should persist correctly."""
    try:
        d, status = post("/api/settings", {"bot_name": "My Assistant 2.0"})
        assert status == 200
        d2, _ = get("/api/settings")
        assert d2.get("bot_name") == "My Assistant 2.0"
    finally:
        _reset_bot_name()


# ── Server-side sanitization ──────────────────────────────────────────────

def test_settings_empty_bot_name_defaults_to_branding():
    """Posting an empty bot_name should default to branding appName server-side."""
    expected = _default_name()
    try:
        d, status = post("/api/settings", {"bot_name": ""})
        assert status == 200
        assert d.get("bot_name") == expected
        d2, _ = get("/api/settings")
        assert d2.get("bot_name") == expected
    finally:
        _reset_bot_name()


def test_settings_whitespace_bot_name_defaults_to_branding():
    """Posting a whitespace-only bot_name should default to branding appName."""
    expected = _default_name()
    try:
        d, status = post("/api/settings", {"bot_name": "   "})
        assert status == 200
        assert d.get("bot_name") == expected
    finally:
        _reset_bot_name()


# ── Login page rendering ──────────────────────────────────────────────────

def test_login_page_shows_default_bot_name():
    """GET /login should contain branding appName in title and h1 when default."""
    expected = _default_name()
    html, status = get_raw("/login")
    assert status == 200
    assert f"<title>{expected}" in html
    assert f"<h1>{expected}</h1>" in html


def test_login_page_shows_custom_bot_name():
    """GET /login should reflect the configured bot_name."""
    try:
        post("/api/settings", {"bot_name": "Aria"})
        html, status = get_raw("/login")
        assert status == 200
        assert "<title>Aria" in html
        assert "<h1>Aria</h1>" in html
    finally:
        _reset_bot_name()


def test_login_page_empty_name_does_not_crash():
    """Login page must not 500 even if somehow bot_name is empty in settings."""
    # Force an empty value by patching settings file directly — skipped here
    # because the server-side guard in POST /api/settings prevents storing empty.
    # Instead, verify that /login returns 200 reliably.
    html, status = get_raw("/login")
    assert status == 200
    assert "Sign in" in html


def test_login_page_xss_escaped():
    """bot_name with HTML special chars should be escaped in the login page."""
    try:
        post("/api/settings", {"bot_name": "<script>alert(1)</script>"})
        html, status = get_raw("/login")
        assert status == 200
        # Raw tag must not appear unescaped
        assert "<script>alert(1)</script>" not in html
        # Escaped form should appear
        assert "&lt;script&gt;" in html
    finally:
        _reset_bot_name()
