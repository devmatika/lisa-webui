"""Voice embed stays on this origin and never forwards arbitrary paths upstream."""

from pathlib import Path

from api.voice_embed import (
    filter_request_cookie,
    map_upstream_path,
    parse_upstream,
    resolve_upstream,
    rewrite_embed_text,
    rewrite_set_cookie,
)

REPO = Path(__file__).resolve().parent.parent
_ORIGIN = "https://voice.example"


def test_parse_upstream_accepts_origin_or_page_base():
    assert parse_upstream("https://voice.example") == ("https://voice.example", "/voice")
    assert parse_upstream("https://voice.example/voice") == ("https://voice.example", "/voice")
    assert parse_upstream("matika-voice.example/voice") == (
        "https://matika-voice.example",
        "/voice",
    )
    assert parse_upstream("http://127.0.0.1:7860/voice") == (
        "http://127.0.0.1:7860",
        "/voice",
    )


def test_page_maps_to_voice_ui_and_unknown_api_is_rejected():
    assert map_upstream_path("/embed/voice", "/voice") == "/voice/"
    assert map_upstream_path("/embed/voice/", "/custom") == "/custom/"
    assert map_upstream_path("/embed/voice/static/app.js", "/voice") == "/voice/static/app.js"
    assert map_upstream_path("/embed/voice-api/offer") == "/api/offer"
    assert map_upstream_path("/embed/voice-api/session") == "/api/session"
    assert map_upstream_path("/embed/voice-api/shutdown") is None
    assert map_upstream_path("/embed/voice/static/../secret") is None
    assert map_upstream_path("/embed/voice-api/offer/extra") is None


def test_rewrite_strips_upstream_host_and_retargets_api_paths():
    html = (
        f'<link rel="stylesheet" href="{_ORIGIN}/voice/static/app.css">'
        f'<script src="/voice/static/app.js"></script>'
        f'await api("/api/offer", {{}}); await api(\'/api/session\');'
    )
    rewritten = rewrite_embed_text(html, _ORIGIN, "/voice")
    assert _ORIGIN not in rewritten
    assert "/embed/embed/" not in rewritten
    assert 'href="/embed/voice/static/app.css"' in rewritten
    assert 'src="/embed/voice/static/app.js"' in rewritten
    assert '"/embed/voice-api/offer"' in rewritten
    assert "'/embed/voice-api/session'" in rewritten


def test_only_voice_cookies_are_forwarded_and_scoped_to_embed():
    header = "session=webui; matika_voice_session=abc; other=no"
    assert filter_request_cookie(header) == "matika_voice_session=abc"
    cookie = rewrite_set_cookie(
        "matika_voice_csrf=tok; Domain=voice.example; Path=/; Secure; SameSite=Lax"
    )
    assert cookie == "matika_voice_csrf=tok; Path=/embed; Secure; SameSite=Lax"
    assert rewrite_set_cookie("webui_session=secret; Path=/") is None


def test_resolve_prefers_profile_specific_process_env(monkeypatch):
    monkeypatch.setenv("MATIKA_VOICE_UPSTREAM", "https://global.example/voice")
    monkeypatch.setenv("MATIKA_VOICE_UPSTREAM_MATIKA", "https://matika.example/voice")
    monkeypatch.setattr("api.voice_embed._active_profile_name", lambda: "matika")
    monkeypatch.setattr("api.voice_embed._profile_env_value", lambda profile, key: "")
    assert resolve_upstream() == ("https://matika.example", "/voice")


def test_upstream_origin_rejects_non_local_http():
    try:
        parse_upstream("http://10.1.1.1:7860/voice")
    except ValueError:
        return
    raise AssertionError("non-local http upstream must be rejected")


def test_customer_page_does_not_name_the_voice_host():
    page = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    script = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    assert 'data-panel="voice"' in page
    assert 'id="voiceEmbedFrame"' in page
    assert 'id="mainVoice"' in page
    assert "voice-sidebar-link" not in page
    assert "matika-voice.fusiontrade.at" not in page
    assert "matika-voice.fusiontrade.at" not in script
    assert "frame.src = '/embed/voice/'" in script
    assert "window.openVoicePanel = openVoicePanel" in script
