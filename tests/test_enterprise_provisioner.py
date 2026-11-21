"""Provisioner invariants: Lisa user profiles never inherit Onyx MCP credentials."""
from __future__ import annotations

import json


def test_render_config_never_copies_mcp_servers(monkeypatch):
    from api.enterprise import provisioner

    monkeypatch.setattr(provisioner, "gateway_url", lambda: "http://gateway:8642")
    default = {
        "model": {"default": "test-model"},
        "mcp_servers": {
            "onyx": {
                "transport": "http",
                "url": "https://onyx.example/mcp",
                "headers": {"Authorization": "Bearer onyx_pat_secret"},
            }
        },
        "approvals": {"mode": "manual"},
    }
    cfg = provisioner.render_config(default, ["memory"], "u-anna")
    assert "mcp_servers" not in cfg
    assert cfg["model"]["default"] == "test-model"
    assert cfg["approvals"]["mode"] == "manual"
    assert "org-authz" in cfg["plugins"]["enabled"]


def test_write_allowlist_never_grants_mcp_plugin_toolsets(tmp_path, monkeypatch):
    from api.enterprise import provisioner

    monkeypatch.setattr(provisioner, "allowed_tools", lambda toolsets: ["memory"])
    home = tmp_path / "u-anna"
    home.mkdir()
    provisioner._write_allowlist(home, ["memory", "knowledge"], ["integration.google"])
    data = json.loads((home / "lisa-allowlist.json").read_text())
    plugin_toolsets = data["plugin_toolsets"]
    assert "lisa_knowledge" in plugin_toolsets
    assert "google_workspace_tools" in plugin_toolsets
    assert not any(ts.startswith("mcp") or "onyx" in ts for ts in plugin_toolsets)
    assert "mcp_servers" not in data


def test_copied_config_keys_exclude_mcp_servers():
    from api.enterprise.provisioner import COPIED_CONFIG_KEYS

    assert "mcp_servers" not in COPIED_CONFIG_KEYS
