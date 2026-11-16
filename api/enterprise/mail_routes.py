"""HTTP routes for e-mail accounts: a person's own (My account), shared mailboxes (administrator) and the
assistant's tools (org-authz plugin, authenticated by the profile's own key). Merged into
``routes.ROUTES``; permissions are checked there, ownership and levels in ``mail``."""
from __future__ import annotations

from api.enterprise import db as edb
from api.enterprise import directory, mail
from api.enterprise import mail_backends as mb
from api.enterprise.directory import DirectoryError
from api.enterprise.gate import current_user


def _call(handler, fn):
    from api.enterprise.routes import _knowledge_call
    return _knowledge_call(handler, fn)


def _me(db, handler):
    return directory.get_user(db, current_user(handler).user_id)


def _actor(handler):
    from api.enterprise.routes import _actor as actor
    return actor(handler)


def _list(handler, _body):
    def run(db):
        me = _me(db, handler)
        from api.enterprise.integrations import _google_ready
        return {"accounts": [mail.view(a, level) for a, level in mail.accounts_for(db, me)],
                "providers": {"google": _google_ready(), "microsoft": mb.microsoft_app() is not None},
                "own_email": me.email}
    return _call(handler, run)


def _add_imap(handler, b):
    def run(db):
        a = mail.add_imap(db, _actor(handler), _me(db, handler), address=b.get("address", ""),
                          password=str(b.get("password") or ""), label=b.get("label", ""),
                          allow=b.get("allow", "draft"), display_name=b.get("display_name", ""),
                          settings=b.get("settings") or None)
        return {"ok": True, "account": mail.view(a)}
    return _call(handler, run)


def _add_microsoft(handler, b):
    def run(db):
        me = _me(db, handler)
        a = mail.add_microsoft(db, _actor(handler), me, address=me.email, label=b.get("label", ""),
                               allow=b.get("allow", "draft"), display_name=b.get("display_name", ""))
        return {"ok": True, "account": mail.view(a)}
    return _call(handler, run)


def _google_start(handler, _body):
    from urllib.parse import parse_qs, quote
    from api.enterprise.routes import _redirect
    q = parse_qs(handler._lisa_parsed.query or "")
    try:
        url = mail.google_start(current_user(handler).user_id, (q.get("label") or ["Personal"])[0],
                                (q.get("allow") or ["draft"])[0])
    except DirectoryError as exc:
        return _redirect(handler, "/lisa?error=" + quote(str(exc)) + "#account")
    return _redirect(handler, url)


def _update(handler, b, admin=False):
    def run(db):
        a = mail.update(db, _actor(handler), _me(db, handler), str(b.get("id") or ""), label=b.get("label"),
                        allow=b.get("allow"), display_name=b.get("display_name"),
                        is_default=bool(b.get("is_default")) or None,
                        grants=list(b["grants"]) if admin and isinstance(b.get("grants"), list) else None,
                        admin=admin)
        return {"ok": True, "account": mail.view(a)}
    return _call(handler, run)


def _remove(handler, b, admin=False):
    return _call(handler, lambda db: (mail.remove(db, _actor(handler), _me(db, handler), str(b.get("id") or ""),
                                                 admin=admin), {"ok": True})[1])


def _check(handler, b, admin=False):
    return _call(handler, lambda db: {"ok": True, "account": mail.view(
        mail.check(db, _me(db, handler), str(b.get("id") or ""), admin=admin))})


def _shared_list(handler, _body):
    return _call(handler, lambda db: {"shared": mail.shared(db),
                                      "providers": {"microsoft": mb.microsoft_app() is not None}})


def _shared_add(handler, b):
    def run(db):
        common = {"address": b.get("address", ""), "label": b.get("label", ""), "allow": b.get("allow", "draft"),
                  "display_name": b.get("display_name", ""), "grants": list(b.get("grants") or [])}
        if b.get("kind") == "microsoft":
            a = mail.add_microsoft(db, _actor(handler), None, **common)
        else:
            a = mail.add_imap(db, _actor(handler), None, password=str(b.get("password") or ""),
                              settings=b.get("settings") or None, **common)
        return {"ok": True, "account": mail.view(a)}
    return _call(handler, run)


def _agent(fn):
    def run_route(handler, body):
        from api.enterprise.routes import _agent_user
        return _call(handler, lambda db: fn(db, _agent_user(db, handler), body))
    return run_route


ROUTES = {
    ("GET", "/api/enterprise/mail/accounts"): (_list, "integration.email"),
    ("POST", "/api/enterprise/mail/accounts/imap"): (_add_imap, "integration.email"),
    ("POST", "/api/enterprise/mail/accounts/microsoft"): (_add_microsoft, "integration.email"),
    ("GET", "/api/enterprise/mail/google/start"): (_google_start, "integration.email"),
    ("POST", "/api/enterprise/mail/accounts/update"): (_update, "integration.email"),
    ("POST", "/api/enterprise/mail/accounts/remove"): (_remove, "integration.email"),
    ("POST", "/api/enterprise/mail/accounts/check"): (_check, "integration.email"),
    ("GET", "/api/enterprise/mail/shared"): (_shared_list, "org.settings.manage"),
    ("POST", "/api/enterprise/mail/shared"): (_shared_add, "org.settings.manage"),
    ("POST", "/api/enterprise/mail/shared/update"): (lambda h, b: _update(h, b, admin=True), "org.settings.manage"),
    ("POST", "/api/enterprise/mail/shared/remove"): (lambda h, b: _remove(h, b, admin=True), "org.settings.manage"),
    ("POST", "/api/enterprise/mail/shared/check"): (lambda h, b: _check(h, b, admin=True), "org.settings.manage"),
    # Called by the org-authz plugin's mail tools; authenticated by the profile's own key.
    ("POST", "/api/enterprise/mail/agent/accounts"): (_agent(lambda db, u, b: mail.agent_accounts(db, u)), None),
    ("POST", "/api/enterprise/mail/agent/search"): (_agent(mail.agent_search), None),
    ("POST", "/api/enterprise/mail/agent/read"): (_agent(mail.agent_read), None),
    ("POST", "/api/enterprise/mail/agent/draft"): (_agent(mail.agent_draft), None),
    ("POST", "/api/enterprise/mail/agent/send"): (_agent(mail.agent_send), None),
}
