"""Lisa Enterprise e-mail: several accounts per person, team mailboxes, and the assistant only ever reaches
the mailboxes (and the level: read < draft < send) its person may use. The IMAP test runs against a real
mail server when LISA_TEST_MAIL_HOST names one (GreenMail in the test runner)."""
from __future__ import annotations

import io
import json
import os
import smtplib
import ssl
import time
import urllib.parse
from email.message import EmailMessage

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")
MAIL_HOST = os.environ.get("LISA_TEST_MAIL_HOST")


@pytest.fixture()
def ent(tmp_path, monkeypatch, request):
    from api.enterprise import db as edb
    from api.enterprise import directory
    from api.enterprise import mail_backends as mb
    from api.enterprise.models import Base
    url = os.environ.get("LISA_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'lisa.db'}"
    if url.startswith("postgresql") or MAIL_HOST:
        request.getfixturevalue("allow_outbound_network")
    monkeypatch.setenv("LISA_ENTERPRISE", "1")
    monkeypatch.setenv("LISA_DATABASE_URL", url)
    monkeypatch.setenv("LISA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    # GreenMail's certificate is self-signed; production verifies certificates
    monkeypatch.setattr(mb, "_ssl_context", ssl._create_unverified_context)
    mb._TOKENS.clear()
    edb.reset_for_tests()
    if url.startswith("postgresql"):
        with edb.engine().begin() as conn:
            Base.metadata.drop_all(conn)
            conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    edb.migrate()
    with edb.session() as db:
        directory.seed(db)
        boss = directory.first_run_setup(db, org_name="KSP", email="boss@ksp.example", password="boss password 1",
                                         display_name="Boss")
        a = directory.Actor(boss.id)
        ids = {"boss": boss.id}
        for name, role in (("anna", "staff"), ("mia", "manager"), ("vic", "viewer")):
            ids[name] = directory.create_user(db, a, email=f"{name}@ksp.example", display_name=name.title(),
                                              role_keys=[role]).id
    yield edb, ids
    edb.reset_for_tests()


def _u(db, uid):
    from api.enterprise.directory import get_user
    return get_user(db, uid)


def _settings():
    return {"imap_host": MAIL_HOST, "imap_port": 3993, "imap_security": "ssl", "smtp_host": MAIL_HOST,
            "smtp_port": 3465, "smtp_security": "ssl"}


def _deliver(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "Hotel Sonne <info@sonne.example>", to, subject
    msg.set_content(body)
    with smtplib.SMTP(MAIL_HOST, 3025, timeout=10) as s:
        s.send_message(msg)


@pytest.mark.skipif(not MAIL_HOST, reason="needs a test mail server (LISA_TEST_MAIL_HOST)")
def test_an_imap_account_reads_drafts_and_sends_only_as_far_as_allowed(ent):
    from api.enterprise import directory, mail
    edb, ids = ent
    with edb.session() as db, pytest.raises(directory.DirectoryError, match="user name or password"):
        mail.add_imap(db, directory.Actor(ids["anna"]), _u(db, ids["anna"]), address="anna@ksp.example",
                      password="wrong", label="Work", allow="draft", settings=_settings())
    with edb.session() as db:
        acct = mail.add_imap(db, directory.Actor(ids["anna"]), _u(db, ids["anna"]), address="anna@ksp.example",
                             password="anna-secret", label="Work", allow="draft", settings=_settings())
        aid = acct.id
        assert acct.is_default and "anna-secret" not in json.dumps(mail.view(acct))
    _deliver("anna@ksp.example", "Booking 14 May", "Can we move the tasting to Friday? Grüße")
    time.sleep(1)
    with edb.session() as db:
        anna = _u(db, ids["anna"])
        found = mail.agent_search(db, anna, {"query": "tasting"})["messages"]
        assert [m["subject"] for m in found] == ["Booking 14 May"] and found[0]["unread"]
        full = mail.agent_read(db, anna, {"id": found[0]["id"]})
        assert "Friday" in full["body"] and "Grüße" in full["body"]
        out = mail.agent_draft(db, anna, {"reply_to_id": found[0]["id"], "body": "Friday works."})
        assert out["ok"] and "nothing was sent" in out["note"]
        drafts = mail.agent_search(db, anna, {"folder": "drafts"})["messages"]
        assert drafts[0]["subject"] == "Re: Booking 14 May" and "info@sonne.example" in drafts[0]["to"]
        with pytest.raises(directory.DirectoryError, match="may not send"):
            mail.agent_send(db, anna, {"to": ["team@ksp.example"], "subject": "Hi", "body": "x"})
        mail.update(db, directory.Actor(ids["anna"]), anna, aid, allow="send")
    with edb.session() as db:
        mail.agent_send(db, _u(db, ids["anna"]), {"to": ["team@ksp.example"], "subject": "Tasting moved",
                                                  "body": "Friday 10:00"})
        # a team mailbox: managers may read it, staff (anna) may not
        mail.add_imap(db, directory.Actor(ids["boss"]), None, address="team@ksp.example", password="team-secret",
                      label="Team", allow="read", settings=_settings(),
                      grants=[{"principal_type": "role", "principal_id": "manager", "level": "send"}])
    time.sleep(1)
    with edb.session() as db:
        mia, anna = _u(db, ids["mia"]), _u(db, ids["anna"])
        got = mail.agent_search(db, mia, {"account": "team", "query": "Tasting"})["messages"]
        assert [m["subject"] for m in got] == ["Tasting moved"]
        assert [a["lisa_may"] for a in mail.agent_accounts(db, mia)["accounts"]] == ["read"]  # capped by the box
        with pytest.raises(directory.DirectoryError, match="No such e-mail account"):
            mail.agent_search(db, anna, {"account": "team@ksp.example"})
        sent = mail.agent_search(db, anna, {"folder": "sent"})["messages"]
        assert [m["subject"] for m in sent] == ["Tasting moved"]


def test_accounts_belong_to_their_person_and_need_the_email_permission(ent, monkeypatch):
    from api.enterprise import directory, mail
    from api.enterprise import mail_backends as mb
    edb, ids = ent
    monkeypatch.setattr(mb, "imap_test", lambda account, secret: {"ok": True})
    with edb.session() as db:
        anna = _u(db, ids["anna"])
        for label, address in (("Work", "anna@ksp.example"), ("Personal", "anna.private@gmx.de")):
            mail.add_imap(db, directory.Actor(anna.id), anna, address=address, password="pw", label=label,
                          allow="read")
        with pytest.raises(directory.DirectoryError, match="already connected"):
            mail.add_imap(db, directory.Actor(anna.id), anna, address="anna@ksp.example", password="pw",
                          label="x", allow="read")
        with pytest.raises(directory.DirectoryError, match="Outlook.com"):
            mail.add_imap(db, directory.Actor(anna.id), anna, address="anna@hotmail.com", password="pw",
                          label="x", allow="read")
    with edb.session() as db:
        anna, mia, vic = (_u(db, ids[k]) for k in ("anna", "mia", "vic"))
        listed = mail.agent_accounts(db, anna)["accounts"]
        assert [(a["label"], a["default"]) for a in listed] == [("Work", True), ("Personal", False)]
        assert mail.accounts_for(db, mia) == [] and mail.accounts_for(db, vic) == []
        with pytest.raises(directory.DirectoryError, match="Unknown mail account"):
            mail.remove(db, directory.Actor(mia.id), mia, listed and mail.accounts_for(db, anna)[0][0].id)
        picked, level = mail._pick(db, anna, "personal", "read")
        assert picked.address == "anna.private@gmx.de" and level == "read"
        with pytest.raises(directory.DirectoryError, match="may not write drafts"):
            mail._pick(db, anna, "personal", "draft")


def test_microsoft_365_reaches_only_the_persons_own_mailbox(ent, monkeypatch):
    from api.enterprise import directory, mail
    from api.enterprise import mail_backends as mb
    edb, ids = ent
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=0):
        calls.append(req.full_url)
        if "oauth2" in req.full_url:
            return Resp(json.dumps({"access_token": "t", "expires_in": 3600}).encode())
        return Resp(json.dumps({"value": [{"id": "AAMk" + "x" * 20, "subject": "Invoice", "isRead": False,
                                           "from": {"emailAddress": {"name": "Supplier", "address": "s@x.example"}}}]}).encode())
    monkeypatch.setattr(mb, "_urlopen", fake)
    monkeypatch.setattr(mb, "microsoft_app", lambda: {"tenant": "t", "client_id": "c", "secret": "s"})
    with edb.session() as db:
        anna = _u(db, ids["anna"])
        with pytest.raises(directory.DirectoryError, match="only add your own"):
            mail.add_microsoft(db, directory.Actor(anna.id), anna, address="boss@ksp.example", label="Work",
                               allow="read")
        mail.add_microsoft(db, directory.Actor(anna.id), anna, address="anna@ksp.example", label="Work", allow="read")
        got = mail.agent_search(db, anna, {"query": "invoice"})
    assert got["messages"][0]["subject"] == "Invoice" and got["messages"][0]["unread"]
    mailbox_calls = [c for c in calls if "graph.microsoft.com" in c]
    assert mailbox_calls and all("/users/anna%40ksp.example/" in c for c in mailbox_calls)


def test_a_gmail_account_comes_from_the_persons_google_sign_in(ent, monkeypatch):
    from api.enterprise import directory, integrations, mail
    from api.enterprise import mail_backends as mb
    edb, ids = ent
    sent = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=0):
        sent.append((req.get_method(), req.full_url, req.data))
        if req.full_url.endswith("/token"):
            return Resp(json.dumps({"access_token": "at", "expires_in": 3600}).encode())
        if req.full_url.endswith("/profile"):
            return Resp(json.dumps({"emailAddress": "Anna.Private@gmail.com"}).encode())
        return Resp(json.dumps({"id": "d1"}).encode())
    monkeypatch.setattr(mb, "_urlopen", fake)
    monkeypatch.setattr(integrations, "_google_client", lambda: {"client_id": "cid", "client_secret": "cs"})
    monkeypatch.setenv("LISA_PUBLIC_URL", "https://lisa.ksp.example")
    url = mail.google_start(ids["anna"], "Personal", "draft")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["scope"] == ["https://www.googleapis.com/auth/gmail.modify"]
    with edb.session() as db:
        acct = mail.google_finish(db, directory.Actor(ids["anna"]), _u(db, ids["anna"]),
                                  {"refresh_token": "rt"}, {"mail": True, "label": "Personal", "allow": "draft"})
        assert (acct.kind, acct.address, acct.label) == ("google", "anna.private@gmail.com", "Personal")
        out = mail.agent_draft(db, _u(db, ids["anna"]), {"to": ["x@y.example"], "subject": "Hi", "body": "Hello"})
    assert out["draft_id"] == "d1"
    method, url, data = sent[-1]
    assert method == "POST" and url.endswith("/users/me/drafts") and json.loads(data)["message"]["raw"]


def test_every_mail_permission_reaches_the_assistant_as_tools(tmp_path):
    from api.enterprise import provisioner
    cfg = provisioner.render_config({}, ["memory", "knowledge"], "u-x", ["integration.email"], "a@b.example")
    assert "lisa_mail" in cfg["platform_toolsets"]["telegram"]
    assert cfg["plugins"]["enabled"] == ["org-authz"]
