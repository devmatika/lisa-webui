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


def test_rule_task_options_are_saved(home):
    ea.save_email_assistant_config(good_config(rules=[
        {"from": "@geniessmi.it", "category": "customer", "task": True, "task_list": " 901 ", "task_assignee": "937"},
        {"from": "@x.at", "category": "customer", "task": False}, {"from": "@y.at", "category": "customer", "task": "maybe"}]))
    saved = json.loads((home / "workspace/email-assistant/config.json").read_text())["rules"]
    assert saved == [{"from": "@geniessmi.it", "category": "customer", "task": True, "task_list": "901", "task_assignee": "937"},
                     {"from": "@x.at", "category": "customer", "task": False}, {"from": "@y.at", "category": "customer"}]


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
    {"rules": [{"from": "@x.at", "category": "customer", "task_list": "abc"}]},
    {"rules": [{"from": "@x.at", "category": "customer", "task_assignee": "Moh"}]},
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


def test_coaching_settings_survive_a_save_and_review_can_be_switched_off(home):
    d = home / "workspace/email-assistant"
    d.mkdir(parents=True)
    keep = {"address": "a@b.at", "webui_url": "https://lisa.example/#email", "review_image": "x.png",
            "review_ignore": ["test@x.at"], "review_weekday": 0}
    (d / "config.json").write_text(json.dumps(keep))
    ea.save_email_assistant_config(good_config(review_every_days=0, webui_url="https://evil.example/"))
    saved = json.loads((d / "config.json").read_text())
    assert saved["review_every_days"] == 0
    assert {k: saved[k] for k in keep if k != "address"} == {k: v for k, v in keep.items() if k != "address"}
    ea.save_email_assistant_config(good_config())
    assert json.loads((d / "config.json").read_text())["review_every_days"] == 7  # default on
    with pytest.raises(ValueError):
        ea.save_email_assistant_config(good_config(review_every_days=99))


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
if "right" not in [v for k, v in os.environ.items() if k.startswith("EMAIL_ASSISTANT_IMAP_PASSWORD")]:
    print("Error: IMAP authentication failed: invalid credentials", file=sys.stderr); sys.exit(1)
if sys.argv[-3:-1] == ["mailbox", "list"]:  # not the account name "mailbox" in -a
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


def test_pause_is_saved_and_only_true_pauses(home):
    d = home / "workspace/email-assistant"
    d.mkdir(parents=True)
    ea.save_email_assistant_config(good_config(paused=True))
    assert json.loads((d / "config.json").read_text())["paused"] is True
    ea.save_email_assistant_config(good_config(paused="yes"))
    assert json.loads((d / "config.json").read_text())["paused"] is False


def _mx_answer():
    """A DNS answer for matika.at MX: two records, the second host name compressed (pointer into the first)."""
    q = b"\x06matika\x02at\x00"
    head = bytes.fromhex("1234818000010002") + b"\x00\x00\x00\x00"
    question = q + b"\x00\x0f\x00\x01"
    rr1_name = b"\x03mx1\x0dagenturserver\x02de\x00"
    rr1 = b"\xc0\x0c\x00\x0f\x00\x01\x00\x00\x01\x2c" + (2 + len(rr1_name)).to_bytes(2, "big") + b"\x00\x14" + rr1_name
    base = 12 + len(question) + 12 + 2 + 4  # offset of "agenturserver" inside answer 1
    rr2_name = b"\x03mx2\xc0" + bytes([base])
    rr2 = b"\xc0\x0c\x00\x0f\x00\x01\x00\x00\x01\x2c" + (2 + len(rr2_name)).to_bytes(2, "big") + b"\x00\x0a" + rr2_name
    return head + question + rr1 + rr2


MX_ANSWER = _mx_answer()


def test_mx_answer_is_parsed_best_first():
    assert ea.parse_mx(MX_ANSWER) == ["mx2.agenturserver.de", "mx1.agenturserver.de"]  # preference 10 before 20
    assert ea.parse_mx(MX_ANSWER, qid=0x9999) == []  # an answer to someone else's query is ignored
    assert ea.parse_mx(b"\x00\x01") == []


def test_server_is_found_from_the_mail_provider(monkeypatch):
    monkeypatch.setattr(ea, "_mx_hosts", lambda d: ["mx1.agenturserver.de"])
    monkeypatch.setattr(ea, "_imap_ok", lambda h: h == "mail.agenturserver.de")
    assert ea.discover_imap("alex@matika.at") == "mail.agenturserver.de"
    monkeypatch.setattr(ea, "_mx_hosts", lambda d: ["alt1.aspmx.l.google.com"])
    monkeypatch.setattr(ea, "_imap_ok", lambda h: h == "imap.gmail.com")
    assert ea.discover_imap("x@firma.at") == "imap.gmail.com"
    monkeypatch.setattr(ea, "_imap_ok", lambda h: False)
    assert ea.discover_imap("x@firma.at") == ""


def test_connect_without_server_uses_the_found_one(mailhome, monkeypatch):
    monkeypatch.setattr(ea, "discover_imap", lambda u: "mail.agenturserver.de")
    st = ea.connect_imap({"user": "alex@matika.at", "password": "right"})
    assert st["connected_host"] == "mail.agenturserver.de"
    assert "imaps://mail.agenturserver.de:993" in (mailhome[0] / ea.MAILBOX_TOML).read_text()
    monkeypatch.setattr(ea, "discover_imap", lambda u: "")
    with pytest.raises(ValueError, match="Server settings"):
        ea.connect_imap({"user": "a@b.at", "password": "right"})


def test_suggestions_are_cleaned_before_use():
    cats = ea.clean_suggestions({"categories": {
        "Job Applications": {"description": "People applying for a job", "tone": "kind", "address": "Sie"},
        "../evil": {"description": "x"}, "invoices": {"description": "Bills", "draft": False, "address": "Herr"},
        "spam": "not a card"}})
    assert cats["job-applications"] == {"description": "People applying for a job", "tone": "kind", "address": "Sie"}
    assert cats["invoices"] == {"description": "Bills", "draft": False}
    assert "evil" in cats and "spam" not in cats and "other" in cats
    ea.validate_config(good_config(categories=cats, rules=[]))  # always saveable
    assert list(ea.clean_suggestions("garbage")) == ["other"]


def test_suggest_reads_only_senders_and_subjects(mailhome, monkeypatch):
    home, _ = mailhome
    ea.connect_imap({"host": "mail.example.com", "user": "a@b.at", "password": "right"})
    (home / ".env").write_text("EMAIL_ASSISTANT_IMAP_PASSWORD=right\nOPENROUTER_API_KEY=k\n")
    envs = [{"from": [{"name": None, "email": f"c{i}@kunde.at"}], "subject": f"Angebot {i}"} for i in range(6)]
    (home / "bin/himalaya").write_text(FAKE_HIMALAYA.replace('{"envelopes": []}', repr({"envelopes": envs})))
    seen = {}

    def fake_llm(h, system, user):
        seen["user"] = user
        return {"categories": {"customer": {"description": "Existing customers", "tone": "friendly"}}}
    monkeypatch.setattr(ea, "_openrouter", fake_llm)
    out = ea.connect_imap({"action": "suggest"})
    assert out["based_on"] == 6 and set(out["categories"]) == {"customer", "other"}
    assert "c0@kunde.at | Angebot 0" in seen["user"]


def test_labels_default_on_and_can_be_switched_off(home):
    assert ea.validate_config(good_config())["labels"] is True
    assert ea.validate_config({**good_config(), "labels": False})["labels"] is False


def test_newsletter_folder_default_on_and_can_be_switched_off(home):
    assert ea.validate_config(good_config())["folders"] == "automated"
    assert ea.validate_config({**good_config(), "folders": "off"})["folders"] == "off"
    assert ea.validate_config({**good_config(), "folders": "INBOX; DROP"})["folders"] == "automated"


def test_category_folder_switch_is_saved_only_when_true(home):
    cats = {"customer": {"tone": "x", "folder": True}, "admin": {"tone": "y", "folder": "yes"}}
    out = ea.validate_config(good_config(categories=cats, rules=[]))["categories"]
    assert out["customer"]["folder"] is True and "folder" not in out["admin"]


def test_folder_settings_survive_a_save_and_are_validated(home):
    cats = {"applicant": {"tone": "x", "folder": True, "folder_name": "Bewerbungen"}}
    out = ea.validate_config(good_config(categories=cats, rules=[], auto_folder_name="Newsletter",
                                         auto_archive_days="30", invoice_folder="INBOX.Zahlung offen"))
    assert out["categories"]["applicant"]["folder_name"] == "Bewerbungen" and out["auto_folder_name"] == "Newsletter"
    assert out["auto_archive_days"] == 30 and out["invoice_folder"] == "INBOX.Zahlung offen"
    assert ea.validate_config(good_config())["auto_folder_name"] == "Automated"
    for bad in ({"auto_folder_name": "Büro"}, {"auto_archive_days": 999}, {"invoice_folder": 'INBOX" DELETE'},
                {"categories": {"a": {"folder_name": "../x"}}}):
        with pytest.raises(ValueError):
            ea.validate_config(good_config(rules=[], **bad))


def test_a_second_mailbox_lives_beside_the_main_one(mailhome):
    home, env_writes = mailhome
    ea.connect_imap({"host": "mail.agenturserver.de", "user": "alex@matika.at", "password": "right"})
    st = ea.connect_imap({"host": "mail.agenturserver.de", "user": "jobs@matika.at", "password": "right", "box": "new"})
    assert st["box"] == "jobs" and [b["address"] for b in st["boxes"]] == ["alex@matika.at", "jobs@matika.at"]
    cfg = json.loads((home / "workspace/email-assistant/boxes/jobs/config.json").read_text())
    assert cfg["himalaya_config"] == "himalaya/email-assistant-jobs.toml" and cfg["address"] == "jobs@matika.at"
    assert cfg["drafts"] is False and cfg["notify"] == "off"  # a new extra mailbox starts quiet
    assert 'printenv EMAIL_ASSISTANT_IMAP_PASSWORD_JOBS' in (home / "himalaya/email-assistant-jobs.toml").read_text()
    assert env_writes == {ea.PASS_ENV: "right", ea.PASS_ENV + "_JOBS": "right"}
    main = json.loads((home / "workspace/email-assistant/config.json").read_text())
    assert main["address"] == "alex@matika.at" and main["himalaya_config"] == ea.MAILBOX_TOML  # untouched
    # connecting the same address again reuses the box
    assert ea.connect_imap({"host": "mail.agenturserver.de", "user": "jobs@matika.at", "password": "right",
                            "box": "new"})["box"] == "jobs"


def test_mailbox_actions_work_on_the_chosen_mailbox_only(mailhome):
    home, _ = mailhome
    ea.connect_imap({"host": "mail.example.com", "user": "jobs@matika.at", "password": "right", "box": "new"})
    st = ea.connect_imap({"action": "save", "box": "jobs", "config": good_config(address="jobs@matika.at", rules=[])})
    assert st["box"] == "jobs" and st["configured"] and st["config"]["address"] == "jobs@matika.at"
    assert not (home / "workspace/email-assistant/config.json").exists()  # the main mailbox was not written
    assert ea.connect_imap({"action": "get", "box": "jobs"})["config"]["address"] == "jobs@matika.at"
    ea.connect_imap({"action": "learned", "box": "jobs", "category": "customer", "notes": "- short"})
    assert "short" in (home / "workspace/email-assistant/boxes/jobs/learned.md").read_text()
    for bad in ({"action": "get", "box": "nope"}, {"action": "get", "box": "../x"}, {"action": "rm", "box": "jobs"}):
        with pytest.raises(ValueError):
            ea.connect_imap(bad)


def test_env_file_goes_back_to_the_profile_owner_after_connect(mailhome, monkeypatch):
    owned = []
    monkeypatch.setattr(ea, "_own", lambda path, mode=0o644: owned.append((path.name, mode)))
    ea.connect_imap({"host": "mail.example.com", "user": "a@b.at", "password": "right"})
    assert (".env", 0o600) in owned  # seen live: root-owned .env broke the cron job's password lookup


def test_auto_reply_is_server_only_and_survives_a_browser_save(home):
    d = home / "workspace/email-assistant"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({**good_config(), "auto_reply": {"enabled": True, "text": {"en": "x"}}}))
    st = ea.save_email_assistant_config({**good_config(), "auto_reply": {"enabled": True, "text": {"en": "EVIL"}}})
    assert "auto_reply" not in st["config"]  # not shown to the browser
    assert json.loads((d / "config.json").read_text())["auto_reply"]["text"]["en"] == "x"  # browser cannot change it


def test_status_shows_the_automatic_thank_you_read_only(home):
    d = home / "workspace/email-assistant/boxes/jobs"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({**good_config(address="jobs@matika.at"), "auto_reply": {
        "enabled": True, "categories": ["vibe-coder"], "max_per_hour": 40, "retention_days": 214,
        "send_window": {"days": [0, 1, 2, 3, 4], "hours": [9, 17]}, "skip": ["secret@x.com"],
        "text": {"en": "Hey {name}"}}}))
    import time
    (d / "state.json").write_text(json.dumps({"auto_replied": {"a@x.com": 1, "b@x.com": int(time.time())}}))
    (d / "talent_pool.csv").write_text('date,email,name,role\n"2026-11-24","b@x.com","Bea","vibe-coder"\n')
    ar = ea.get_email_assistant("jobs")["auto_reply"]
    assert ar["enabled"] and ar["sent_total"] == 2 and ar["sent_today"] == 1
    assert ar["talent_pool"] == [{"date": "2026-11-24", "name": "Bea", "email": "b@x.com", "role": "vibe-coder"}]
    assert ar["texts"] == {"en": "Hey {name}"} and "skip" not in ar  # the skip list stays on the server
    assert ea.get_email_assistant()["auto_reply"] is None  # mailboxes without it show nothing


def test_owner_can_switch_the_automatic_reply_but_change_nothing_else(home):
    d = home / "workspace/email-assistant/boxes/jobs"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({**good_config(address="jobs@matika.at"),
                                               "auto_reply": {"enabled": True, "text": {"en": "x"}, "skip": ["a@x"]}}))
    st = ea.connect_imap({"action": "auto_reply", "box": "jobs", "enabled": False, "text": {"en": "EVIL"}})
    cfg = json.loads((d / "config.json").read_text())
    assert st["auto_reply"]["enabled"] is False and cfg["auto_reply"] == {"enabled": False, "text": {"en": "x"}, "skip": ["a@x"]}
    for bad in ({"enabled": "yes"}, {"enabled": None}):
        with pytest.raises(ValueError):
            ea.connect_imap({"action": "auto_reply", "box": "jobs", **bad})
    (home / "workspace/email-assistant/config.json").write_text(json.dumps(good_config()))
    with pytest.raises(ValueError, match="no automatic reply"):
        ea.connect_imap({"action": "auto_reply", "enabled": True})


def test_task_age_limit_and_prefix_survive_a_save(home):
    out = ea.validate_config({**good_config(), "tasks": {"enabled": True, "list_id": "1", "max_age_days": 2,
                                                         "name_prefix": "[jobs] "}})["tasks"]
    assert out["max_age_days"] == 2 and out["name_prefix"] == "[jobs]"
