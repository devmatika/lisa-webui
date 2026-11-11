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
    assert st["mailbox"] == {"connected": False}


def test_save_writes_the_file_the_cron_job_reads(home):
    ea.save_email_assistant_config(good_config())
    saved = json.loads((home / "workspace/email-assistant/config.json").read_text())
    assert saved["categories"]["customer"] == {"tone": "friendly", "address": "Sie", "max_words": 120}
    assert saved["categories"]["admin"]["draft"] is False
    assert saved["rules"][1] == {"subject": "Newsletter", "skip": True} and saved["drafts"] is True
    assert saved["facts"] == "" and saved["only_from"] == [] and saved["notify"] == "actionable"


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
    {"only_from": "x@y.at"},
    {"facts": "x" * 2001},
    {"mail_url": "javascript:alert(1)"},
    {"daily_at": "7.30"},
    {"tasks": {"enabled": True, "list_id": ""}},
    {"tasks": {"enabled": True, "list_id": "abc"}},
])
def test_invalid_config_is_rejected(home, bad):
    with pytest.raises(ValueError):
        ea.save_email_assistant_config(good_config(**bad))


def test_daily_summary_and_clickup_settings_survive_a_save(home):
    d = home / "workspace/email-assistant"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"address": "a@b.at", "timezone": "Europe/Vienna", "open_days": 7}))
    ea.save_email_assistant_config(good_config(daily_at="08:15", timezone="UTC",
                                               tasks={"enabled": True, "list_id": "901", "assignee": "66", "min_priority": "x"}))
    saved = json.loads((d / "config.json").read_text())
    assert saved["daily_at"] == "08:15" and saved["timezone"] == "Europe/Vienna" and saved["open_days"] == 7
    assert saved["tasks"] == {"enabled": True, "list_id": "901", "assignee": "66", "min_priority": "high"}
    assert json.loads((d / "config.json").read_text()) == saved
    ea.save_email_assistant_config(good_config(daily_at=""))  # empty = off
    assert json.loads((d / "config.json").read_text())["daily_at"] == ""


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


FAKE_HIMALAYA = """#!/usr/bin/env python3
import json, os, sys
if os.environ.get("EMAIL_ASSISTANT_IMAP_PASSWORD") != "right":
    print("Error: IMAP authentication failed: invalid credentials", file=sys.stderr); sys.exit(1)
if "mailbox" in sys.argv:
    print(json.dumps({"mailboxes": [{"name": "INBOX"}, {"name": "INBOX.Drafts"}, {"name": "INBOX.Sent Messages"},
                                    {"name": "INBOX.Papierkorb"}]}))
else:
    print(json.dumps({"envelopes": []}))
"""


@pytest.fixture
def mailhome(home, monkeypatch):
    (home / "bin").mkdir()
    b = home / "bin/himalaya"
    b.write_text(FAKE_HIMALAYA)
    b.chmod(0o755)
    env_writes = {}
    import api.providers
    monkeypatch.setattr(api.providers, "_write_env_file", lambda path, upd: env_writes.update(upd))
    return home, env_writes


def test_connect_imap_finds_folders_and_keeps_password_out_of_files(mailhome):
    home, env_writes = mailhome
    st = ea.connect_imap({"host": "mail.agenturserver.de", "port": 993, "user": "alex@matika.at",
                          "password": "right", "name": "Alex"})
    toml = (home / ea.MAILBOX_TOML).read_text()
    assert 'mailbox.alias.drafts = "INBOX.Drafts"' in toml and 'mailbox.alias.sent = "INBOX.Sent Messages"' in toml
    assert "right" not in toml and env_writes == {ea.PASS_ENV: "right"}
    cfg = json.loads((home / "workspace/email-assistant/config.json").read_text())
    assert cfg["account"] == "mailbox" and cfg["himalaya_config"] == ea.MAILBOX_TOML and cfg["address"] == "alex@matika.at"
    assert st["mailbox"]["connected"] and st["mailbox"]["via"] == "wizard" and st["configured"] is False
    assert st["connected_folders"]["drafts"] == "INBOX.Drafts"
    assert "account" not in st["config"]


def test_wrong_password_saves_nothing(mailhome):
    home, env_writes = mailhome
    with pytest.raises(ValueError, match="invalid credentials"):
        ea.connect_imap({"host": "mail.example.com", "user": "a@b.at", "password": "wrong"})
    assert not (home / ea.MAILBOX_TOML).exists() and not list((home / "himalaya").glob("*.test")) and env_writes == {}


@pytest.mark.parametrize("bad", [{"host": "evil;rm -rf /"}, {"host": "-x"}, {"user": "nope"}, {"password": ""},
                                 {"password": "a\nb"}, {"port": 0}])
def test_connect_input_is_validated(mailhome, bad):
    with pytest.raises(ValueError):
        ea.connect_imap({"host": "mail.example.com", "port": 993, "user": "a@b.at", "password": "right", **bad})


def test_folder_names_of_common_providers():
    assert ea.pick_folders(["INBOX", "[Gmail]/Drafts", "[Gmail]/Sent Mail", "[Gmail]/Trash"]) == {
        "drafts": "[Gmail]/Drafts", "sent": "[Gmail]/Sent Mail", "trash": "[Gmail]/Trash"}
    assert ea.pick_folders(["Posteingang", "Entwürfe", "Gesendete Elemente", "Gelöschte Elemente"]) == {
        "drafts": "Entwürfe", "sent": "Gesendete Elemente", "trash": "Gelöschte Elemente"}
    assert ea.pick_folders(["INBOX", "Drafts", "Sent", "Trash"])["drafts"] == "Drafts"


def test_browser_cannot_change_server_side_mailbox_settings(home):
    d = home / "workspace/email-assistant"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"address": "a@b.at", "account": "mailbox", "himalaya_config": "himalaya/x.toml",
                                               "telegram_chats": ["1"]}))
    ea.save_email_assistant_config(good_config(account="evil", himalaya_config="/etc/passwd", telegram_chats=["999"]))
    saved = json.loads((d / "config.json").read_text())
    assert saved["account"] == "mailbox" and saved["himalaya_config"] == "himalaya/x.toml" and saved["telegram_chats"] == ["1"]
