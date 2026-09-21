"""Unit tests for white-label branding (api/branding.py)."""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _clear_branding(monkeypatch):
    from api.branding import clear_branding_cache

    clear_branding_cache()
    for key in (
        "NEXT_PUBLIC_APP_NAME",
        "WEBUI_APP_NAME",
        "HERMES_WEBUI_BOT_NAME",
        "NEXT_PUBLIC_COMPANY_NAME",
        "WEBUI_COMPANY_NAME",
        "NEXT_PUBLIC_PRIMARY_COLOR",
        "WEBUI_PRIMARY_COLOR",
        "NEXT_PUBLIC_LOGO",
        "WEBUI_LOGO",
    ):
        monkeypatch.delenv(key, raising=False)
    clear_branding_cache()
    yield
    clear_branding_cache()


def test_default_branding_is_matika():
    from api.branding import default_bot_name, get_branding

    b = get_branding()
    assert b["appName"] == "Matika AI Assistant"
    assert b["companyName"] == "Matika"
    assert default_bot_name() == "Matika AI Assistant"
    assert b["primaryColor"].startswith("#")


def test_next_public_env_overrides(monkeypatch):
    from api.branding import clear_branding_cache, default_bot_name, get_branding

    monkeypatch.setenv("NEXT_PUBLIC_APP_NAME", "Profax AI Assistant")
    monkeypatch.setenv("NEXT_PUBLIC_COMPANY_NAME", "Profax")
    monkeypatch.setenv("NEXT_PUBLIC_PRIMARY_COLOR", "#1B4F72")
    clear_branding_cache()
    b = get_branding()
    assert b["appName"] == "Profax AI Assistant"
    assert b["companyName"] == "Profax"
    assert b["primaryColor"] == "#1B4F72"
    assert default_bot_name() == "Profax AI Assistant"


def test_rejects_javascript_logo(monkeypatch):
    from api.branding import clear_branding_cache, get_branding

    monkeypatch.setenv("NEXT_PUBLIC_LOGO", "javascript:alert(1)")
    clear_branding_cache()
    b = get_branding()
    assert not b["logo"].lower().startswith("javascript:")


def test_apply_branding_to_html_substitutes_tokens():
    from api.branding import apply_branding_to_html

    html = (
        "<title>__BRANDING_APP_NAME__</title>"
        "<script>window.__BRANDING__=__BRANDING_JSON__;</script>"
    )
    out = apply_branding_to_html(html)
    assert "__BRANDING_APP_NAME__" not in out
    assert "Matika" in out
    assert "window.__BRANDING__={" in out
    payload = out.split("window.__BRANDING__=", 1)[1].split(";</script>", 1)[0]
    data = json.loads(payload.replace("<\\/", "</"))
    assert data["appName"] == "Matika AI Assistant"


def test_branded_manifest_rewrites_name():
    from api.branding import branded_manifest

    raw = {
        "name": "Hermes",
        "short_name": "Hermes",
        "description": "Hermes AI Agent Web UI",
        "icons": [{"src": "static/favicon.svg", "sizes": "any"}],
        "shortcuts": [{"description": "Open Hermes ready for a new chat"}],
    }
    data = json.loads(branded_manifest(raw).decode("utf-8"))
    assert data["name"] == "Matika AI Assistant"
    assert data["short_name"] == "Matika"
    assert "Hermes" not in data["shortcuts"][0]["description"]
