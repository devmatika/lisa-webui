"""Model / profile / workspace composer chips default hidden for everyone.

Lisa hides these three footer controls by default. Existing settings.json files
that still store hide_*=False (pre-flip) are force-reset unless the user has
explicitly re-shown any of them (composer_core_chips_revealed).
"""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "api" / "config.py"
PANELS = REPO_ROOT / "static" / "panels.js"

_CORE = (
    "hide_composer_model",
    "hide_composer_profile",
    "hide_composer_workspace",
)


def test_defaults_and_marker_registered():
    src = CONFIG.read_text(encoding="utf-8")
    for key in _CORE:
        assert f'"{key}": True' in src, f"{key} must default to hidden"
    assert '"composer_core_chips_revealed": False' in src
    assert '"composer_core_chips_revealed",' in src


def test_panels_records_revealed_optin_when_any_core_chip_shown():
    js = PANELS.read_text(encoding="utf-8")
    assert "composer_core_chips_revealed=true" in js
    assert "hide_composer_model" in js
    assert "hide_composer_profile" in js
    assert "hide_composer_workspace" in js


@pytest.fixture
def _settings_env(tmp_path, monkeypatch):
    import api.config as config

    sf = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", sf)
    return config, sf


def _write(sf, payload):
    sf.write_text(json.dumps(payload), encoding="utf-8")


def test_migration_forces_hide_when_stale_false_stored(_settings_env):
    config, sf = _settings_env
    _write(
        sf,
        {
            "onboarding_completed": True,
            "hide_composer_model": False,
            "hide_composer_profile": False,
            "hide_composer_workspace": False,
        },
    )
    s = config.load_settings()
    assert s["hide_composer_model"] is True
    assert s["hide_composer_profile"] is True
    assert s["hide_composer_workspace"] is True


def test_migration_honours_explicit_reveal(_settings_env):
    config, sf = _settings_env
    _write(
        sf,
        {
            "onboarding_completed": True,
            "composer_core_chips_revealed": True,
            "hide_composer_model": False,
            "hide_composer_profile": True,
            "hide_composer_workspace": True,
        },
    )
    s = config.load_settings()
    assert s["hide_composer_model"] is False
    assert s["hide_composer_profile"] is True
    assert s["hide_composer_workspace"] is True


def test_fresh_settings_use_hidden_defaults(_settings_env):
    config, sf = _settings_env
    assert not sf.exists()
    s = config.load_settings()
    assert s["hide_composer_model"] is True
    assert s["hide_composer_profile"] is True
    assert s["hide_composer_workspace"] is True
    assert s["composer_core_chips_revealed"] is False
