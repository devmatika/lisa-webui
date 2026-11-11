"""Tests for the Email auto-drafts settings (api.email_assistant)."""

import json

import pytest

import api.email_assistant as ea


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ea, "get_active_hermes_home", lambda: tmp_path)
    return tmp_path


def good_config(**over):
    cfg = {"address": "alex@matika.at", "name": "Alex", "drafts": True,
           "categories": {"customer": {"tone": "friendly", "address": "Sie", "max_words": "120"},
                          "admin": {"tone": "formal", "draft": False}},
           "rules": [{"from": "@bigclient.at", "category": "customer"}, {"subject": "Newsletter", "skip": True}]}
    cfg.update(over)
    return cfg


def test_not_configured_yet_offers_presets(home):
    st = ea.get_email_assistant()
    assert st["configured"] is False and "customer" in st["presets"] and st["cron"] is None


def test_save_writes_the_file_the_cron_job_reads(home):
    ea.save_email_assistant_config(good_config())
    saved = json.loads((home / "workspace/email-assistant/config.json").read_text())
    assert saved["categories"]["customer"] == {"tone": "friendly", "address": "Sie", "max_words": 120}
    assert saved["categories"]["admin"]["draft"] is False
    assert saved["rules"][1] == {"subject": "Newsletter", "skip": True} and saved["drafts"] is True
    assert saved["facts"] == "" and saved["only_from"] == []


def test_server_only_keys_are_kept_and_hidden(home):
    d = home / "workspace/email-assistant"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"address": "a@b.at", "himalaya": "bin/himalaya", "model": "m1",
                                               "watch_seconds": 280}))
    st = ea.save_email_assistant_config(good_config(himalaya="/bin/evil", model="x", watch_seconds=3000,
                                                    facts="We build websites.", only_from=["t@x.at", ""]))
    saved = json.loads((d / "config.json").read_text())
    assert saved["himalaya"] == "bin/himalaya" and saved["model"] == "m1" and saved["watch_seconds"] == 280
    assert saved["facts"] == "We build websites." and saved["only_from"] == ["t@x.at"]
    assert "himalaya" not in st["config"] and "model" not in st["config"]


@pytest.mark.parametrize("bad", [
    {"address": "not-an-email"},
    {"categories": {}},
    {"categories": {"Bad Name!": {}}},
    {"categories": {"customer": {"tone": "x" * 301}}},
    {"rules": [{"from": "@x.at", "category": "missing"}]},
    {"rules": [{"category": "customer"}]},
    {"max_per_run": 99},
    {"account": "a b; rm -rf"},
    {"only_from": "x@y.at"},
    {"facts": "x" * 2001},
])
def test_invalid_config_is_rejected(home, bad):
    with pytest.raises(ValueError):
        ea.save_email_assistant_config(good_config(**bad))


def test_approve_moves_proposal_into_learned_md(home):
    d = home / "workspace/email-assistant"
    d.mkdir(parents=True)
    (d / "proposals.json").write_text(json.dumps({"customer": {"notes": "- sign off 'LG Alex'", "based_on": 3}}))
    st = ea.resolve_proposal("customer", "approve", "- sign off 'LG Alex'\n- keep it short")
    assert st["learned"]["customer"] == "- sign off 'LG Alex'\n- keep it short" and st["proposals"] == {}
    assert "## customer" in (d / "learned.md").read_text()


def test_reject_drops_proposal_without_learning(home):
    d = home / "workspace/email-assistant"
    d.mkdir(parents=True)
    (d / "proposals.json").write_text(json.dumps({"customer": {"notes": "- x"}}))
    st = ea.resolve_proposal("customer", "reject")
    assert st["proposals"] == {} and st["learned"] == {}
    with pytest.raises(ValueError):
        ea.resolve_proposal("customer", "approve")


def test_learned_notes_can_be_edited_and_cleared(home):
    ea.save_learned_notes("customer", "- short")
    assert ea.get_email_assistant()["learned"] == {"customer": "- short"}
    ea.save_learned_notes("customer", "")
    assert ea.get_email_assistant()["learned"] == {}


def test_cron_status_is_read_from_jobs_json(home):
    (home / "cron").mkdir()
    (home / "cron/jobs.json").write_text(json.dumps({"jobs": [
        {"name": "email-drafts", "enabled": True, "schedule": {"display": "*/20 7-19 * * 1-5"}, "last_status": "ok"}]}))
    assert ea.get_email_assistant()["cron"] == {"enabled": True, "schedule": "*/20 7-19 * * 1-5",
                                                "last_run_at": None, "last_status": "ok"}
