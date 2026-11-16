"""Talking to mailboxes: IMAP + SMTP, the Gmail API and Microsoft Graph, behind one small interface.

Every backend takes the account (``kind``, ``address``, ``display_name``, ``settings``) and its secret
and offers ``test``, ``search``, ``read``, ``draft`` and ``send``, returning plain dicts. Messages are
addressed by the id ``search`` returned. Errors surface as ``MailError`` with a sentence a non-technical
person can act on; the raw protocol text never reaches the browser or the model.
"""
from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import json
import re
import smtplib
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parsedate_to_datetime

TIMEOUT = 25
MAX_BODY = 20_000
MAX_RESULTS = 25


class MailError(ValueError):
    pass


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def _urlopen(req, timeout=TIMEOUT):
    return urllib.request.urlopen(req, timeout=timeout)


# ── message helpers ─────────────────────────────────────────────────────────────────────────────

def _addr_list(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    out = []
    for name, addr in getaddresses([str(v) for v in value or []]):
        if not re.match(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$", addr or ""):
            raise MailError(f"Not an e-mail address: {name or addr}")
        out.append(formataddr((name, addr)) if name else addr)
    return out


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    import html as _h
    return re.sub(r"\n{3,}", "\n\n", _h.unescape(text)).strip()


def _body_of(msg) -> tuple[str, list[str]]:
    plain, html, files = None, None, []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart():
            continue
        name = part.get_filename()
        if name or part.get_content_disposition() == "attachment":
            files.append(name or "attachment")
            continue
        ctype = part.get_content_type()
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if ctype == "text/plain" and plain is None:
            plain = str(content)
        elif ctype == "text/html" and html is None:
            html = str(content)
    body = plain if plain is not None else _html_to_text(html or "")
    return body.strip()[:MAX_BODY], files


def _date(value: str | None) -> str:
    try:
        return parsedate_to_datetime(value).isoformat() if value else ""
    except (TypeError, ValueError):
        return value or ""


def _summary(msg, msg_id: str, unread: bool) -> dict:
    body, _files = _body_of(msg)
    return {"id": msg_id, "from": str(msg.get("From", "")), "to": str(msg.get("To", "")),
            "subject": str(msg.get("Subject", "")), "date": _date(msg.get("Date")),
            "snippet": re.sub(r"\s+", " ", body)[:200], "unread": unread}


def _full(msg, msg_id: str) -> dict:
    body, files = _body_of(msg)
    return {"id": msg_id, "from": str(msg.get("From", "")), "to": str(msg.get("To", "")),
            "cc": str(msg.get("Cc", "")), "subject": str(msg.get("Subject", "")), "date": _date(msg.get("Date")),
            "message_id": str(msg.get("Message-ID", "")), "body": body, "attachments": files}


def compose(account: dict, *, to, cc=None, subject: str, body: str, reply_to: dict | None = None) -> EmailMessage:
    """A plain-text message from the account. ``reply_to`` is the original (``_full`` shape)."""
    msg = EmailMessage()
    msg["From"] = formataddr((account.get("display_name") or "", account["address"]))
    to_list = _addr_list(to) if to else (_addr_list(reply_to["from"]) if reply_to else [])
    if not to_list:
        raise MailError("Who should the e-mail go to?")
    msg["To"] = ", ".join(to_list)
    if cc:
        msg["Cc"] = ", ".join(_addr_list(cc))
    subj = (subject or "").strip()
    if reply_to:
        orig = reply_to.get("subject") or ""
        subj = subj or (orig if orig.lower().startswith("re:") else f"Re: {orig}")
        if reply_to.get("message_id"):
            msg["In-Reply-To"] = reply_to["message_id"]
            msg["References"] = reply_to["message_id"]
    if not subj:
        raise MailError("The e-mail needs a subject.")
    msg["Subject"] = subj
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=account["address"].split("@")[-1])
    msg.set_content(body or "")
    return msg


# ── IMAP + SMTP ─────────────────────────────────────────────────────────────────────────────────

# domain -> (imap host, imap port, smtp host, smtp port, smtp security, note); ports 993 / 465 = TLS
PROVIDERS = {
    "gmail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465, "ssl",
                  "Gmail needs an app password: Google account → Security → 2-Step Verification → App passwords."),
    "googlemail.com": ("imap.gmail.com", 993, "smtp.gmail.com", 465, "ssl",
                       "Gmail needs an app password: Google account → Security → 2-Step Verification → App passwords."),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "smtp.mail.yahoo.com", 465, "ssl",
                  "Yahoo needs an app password: Account security → Generate app password."),
    "icloud.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587, "starttls",
                   "iCloud needs an app-specific password: appleid.apple.com → Sign-In and Security."),
    "me.com": ("imap.mail.me.com", 993, "smtp.mail.me.com", 587, "starttls",
               "iCloud needs an app-specific password: appleid.apple.com → Sign-In and Security."),
    "gmx.de": ("imap.gmx.net", 993, "mail.gmx.net", 587, "starttls",
               "GMX: allow POP3/IMAP once in the GMX settings (E-Mail → POP3/IMAP Abruf)."),
    "gmx.net": ("imap.gmx.net", 993, "mail.gmx.net", 587, "starttls",
                "GMX: allow POP3/IMAP once in the GMX settings (E-Mail → POP3/IMAP Abruf)."),
    "gmx.at": ("imap.gmx.net", 993, "mail.gmx.net", 587, "starttls", ""),
    "gmx.ch": ("imap.gmx.net", 993, "mail.gmx.net", 587, "starttls", ""),
    "web.de": ("imap.web.de", 993, "smtp.web.de", 587, "starttls",
               "WEB.DE: allow POP3/IMAP once in the WEB.DE settings."),
    "t-online.de": ("secureimap.t-online.de", 993, "securesmtp.t-online.de", 465, "ssl",
                    "Telekom: use the e-mail password set in the Telekom e-mail centre."),
    "posteo.de": ("posteo.de", 993, "posteo.de", 465, "ssl", ""),
    "mailbox.org": ("imap.mailbox.org", 993, "smtp.mailbox.org", 465, "ssl", ""),
    "ionos.de": ("imap.ionos.de", 993, "smtp.ionos.de", 465, "ssl", ""),
}
MICROSOFT_CONSUMER = {"outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.de", "hotmail.de"}


def provider_hint(address: str) -> dict:
    domain = address.rsplit("@", 1)[-1].lower()
    if domain in MICROSOFT_CONSUMER:
        return {"blocked": True, "note": "Microsoft no longer lets apps sign in to Outlook.com / Hotmail with a "
                "password. Forward those e-mails to an account Lisa can read instead."}
    p = PROVIDERS.get(domain)
    return {"known": bool(p), "note": p[5] if p else ""}


def candidate_settings(address: str, username: str = "") -> list[dict]:
    """Settings to try for an address: the known provider, else the usual host names of the domain."""
    domain = address.rsplit("@", 1)[-1].lower()
    user = username or address
    if domain in PROVIDERS:
        ih, ip, sh, sp, ss, _note = PROVIDERS[domain]
        return [{"imap_host": ih, "imap_port": ip, "imap_security": "ssl", "smtp_host": sh, "smtp_port": sp,
                 "smtp_security": ss, "username": user, "save_sent": "gmail" not in ih}]
    out = []
    for prefix in ("imap.", "mail.", ""):
        host = prefix + domain
        smtp = ("smtp." + domain) if prefix == "imap." else host
        out.append({"imap_host": host, "imap_port": 993, "imap_security": "ssl", "smtp_host": smtp,
                    "smtp_port": 587, "smtp_security": "starttls", "username": user, "save_sent": True})
    return out


_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def clean_settings(s: dict, address: str) -> dict:
    out = {}
    for side, default_port in (("imap", 993), ("smtp", 587)):
        host = str(s.get(f"{side}_host") or "").strip()
        if not _HOST.match(host):
            raise MailError(f"The {side.upper()} server name is not valid.")
        try:
            port = int(s.get(f"{side}_port") or default_port)
        except (TypeError, ValueError):
            raise MailError(f"The {side.upper()} port must be a number.") from None
        security = s.get(f"{side}_security") or ("ssl" if port in (993, 465) else "starttls")
        if not 1 <= port <= 65535 or security not in ("ssl", "starttls"):
            raise MailError(f"The {side.upper()} port or security is not valid.")
        out.update({f"{side}_host": host, f"{side}_port": port, f"{side}_security": security})
    out["username"] = str(s.get("username") or address).strip()[:320]
    out["save_sent"] = bool(s.get("save_sent", True))
    return out


def _explain(exc: Exception, side: str) -> MailError:
    text = str(exc).lower()
    if isinstance(exc, (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError)) and (
            "auth" in text or "login" in text or "credentials" in text or "password" in text or "535" in text):
        return MailError("The mail server did not accept the user name or password. Many providers need an "
                         "app password instead of the normal one.")
    if isinstance(exc, ssl.SSLError):
        return MailError(f"The {side} server's security certificate could not be verified.")
    if isinstance(exc, (socket.gaierror,)):
        return MailError(f"The {side} server name could not be found. Check it for typos.")
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, OSError)):
        return MailError(f"Lisa could not reach the {side} server. Check the server name and port.")
    return MailError(f"The {side} server refused the request.")


def _imap(account: dict, secret: dict) -> imaplib.IMAP4:
    s = account["settings"]
    try:
        if s["imap_security"] == "ssl":
            conn = imaplib.IMAP4_SSL(s["imap_host"], s["imap_port"], ssl_context=_ssl_context(), timeout=TIMEOUT)
        else:
            conn = imaplib.IMAP4(s["imap_host"], s["imap_port"], timeout=TIMEOUT)
            conn.starttls(ssl_context=_ssl_context())
        conn.login(s["username"], secret.get("password", ""))
        return conn
    except (imaplib.IMAP4.error, OSError) as exc:
        raise _explain(exc, "IMAP") from exc


def _close(conn) -> None:
    try:
        conn.logout()
    except (imaplib.IMAP4.error, OSError):
        pass


_SPECIAL = {"drafts": r"\\Drafts", "sent": r"\\Sent", "trash": r"\\Trash"}
_FOLDER_NAMES = {
    "drafts": r"(^|[./])(drafts|entw(ü|ue)rfe|brouillons|bozze|borradores)$",
    "sent": r"(^|[./])(sent|sent messages|sent items|sent mail|gesendet|gesendete (objekte|elemente))$",
}


def _folders(conn) -> dict[str, str]:
    typ, data = conn.list()
    found, names = {}, []
    for raw in data or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        m = re.match(r'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip()
        name = name[1:-1].replace('\\"', '"') if name.startswith('"') else name
        names.append(name)
        for role, flag in _SPECIAL.items():
            if re.search(flag, m.group("flags"), re.I):
                found.setdefault(role, name)
    for role, pattern in _FOLDER_NAMES.items():
        if role not in found:
            found[role] = next((n for n in names if re.search(pattern, n, re.I)), "Drafts" if role == "drafts" else "Sent")
    found["inbox"] = "INBOX"
    return found


def _q(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_folder(conn, folder: str) -> str:
    folder = (folder or "inbox").lower()
    return _folders(conn).get(folder, "INBOX") if folder in ("inbox", "sent", "drafts") else "INBOX"


def imap_test(account: dict, secret: dict) -> dict:
    conn = _imap(account, secret)
    try:
        folders = _folders(conn)
    finally:
        _close(conn)
    s = account["settings"]
    try:
        smtp = _smtp(s)
        try:
            smtp.login(s["username"], secret.get("password", ""))
        finally:
            smtp.quit()
    except (smtplib.SMTPException, OSError) as exc:
        raise _explain(exc, "SMTP") from exc
    return {"ok": True, "folders": folders}


def _smtp(s: dict):
    if s["smtp_security"] == "ssl":
        return smtplib.SMTP_SSL(s["smtp_host"], s["smtp_port"], timeout=TIMEOUT, context=_ssl_context())
    conn = smtplib.SMTP(s["smtp_host"], s["smtp_port"], timeout=TIMEOUT)
    conn.starttls(context=_ssl_context())
    return conn


def _fetch(conn, uid: str, what: str):
    typ, data = conn.uid("FETCH", uid, what)
    if typ != "OK":
        raise MailError("That e-mail was not found any more.")
    for item in data or []:
        if isinstance(item, tuple):
            return item
    raise MailError("That e-mail was not found any more.")


def _search_criteria(query: str, unread_only: bool) -> list[str]:
    crit = ["UNSEEN"] if unread_only else []
    q = (query or "").strip()
    if q:
        crit += ["TEXT", _q(q)]
    return crit or ["ALL"]


def imap_search(account: dict, secret: dict, query: str, folder: str, limit: int, unread_only: bool) -> list[dict]:
    conn = _imap(account, secret)
    try:
        name = _imap_folder(conn, folder)
        conn.select(_q(name), readonly=True)
        crit = _search_criteria(query, unread_only)
        if (query or "").isascii():
            typ, data = conn.uid("SEARCH", *crit)
        else:  # non-Latin words go as a UTF-8 literal
            conn.literal = query.strip().encode()
            typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *(["UNSEEN"] if unread_only else []), "TEXT")
        uids = (data[0] or b"").split() if typ == "OK" and data else []
        out = []
        for uid in reversed(uids[-limit:]):
            head, raw = _fetch(conn, uid.decode(), "(FLAGS BODY.PEEK[]<0.16384>)")
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            out.append(_summary(msg, f"{uid.decode()}:{name}", b"\\Seen" not in head))
        return out
    except (imaplib.IMAP4.error, OSError) as exc:
        raise _explain(exc, "IMAP") from exc
    finally:
        _close(conn)


def _split_id(msg_id: str) -> tuple[str, str]:
    uid, _, folder = str(msg_id or "").partition(":")
    if not uid.isdigit() or not folder:
        raise MailError("Unknown e-mail id: use an id from a search.")
    return uid, folder


def imap_read(account: dict, secret: dict, msg_id: str) -> dict:
    uid, folder = _split_id(msg_id)
    conn = _imap(account, secret)
    try:
        conn.select(_q(folder), readonly=True)
        _head, raw = _fetch(conn, uid, "(BODY.PEEK[])")
        return _full(email.message_from_bytes(raw, policy=email.policy.default), msg_id)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise _explain(exc, "IMAP") from exc
    finally:
        _close(conn)


def _append(account: dict, secret: dict, role: str, msg: EmailMessage, flags: str) -> str:
    conn = _imap(account, secret)
    try:
        folder = _folders(conn)[role]
        stamp = imaplib.Time2Internaldate(time.time())
        typ, data = conn.append(_q(folder), flags, stamp, msg.as_bytes())
        if typ != "OK" and b"TRYCREATE" in b" ".join(d for d in data or [] if isinstance(d, bytes)).upper():
            conn.create(_q(folder))  # a mailbox without a Drafts/Sent folder yet
            typ, data = conn.append(_q(folder), flags, stamp, msg.as_bytes())
        if typ != "OK":
            raise MailError(f"The mail server did not save the e-mail in {folder}.")
        return folder
    except (imaplib.IMAP4.error, OSError) as exc:
        raise _explain(exc, "IMAP") from exc
    finally:
        _close(conn)


def imap_draft(account: dict, secret: dict, msg: EmailMessage) -> dict:
    return {"ok": True, "saved_in": _append(account, secret, "drafts", msg, "(\\Draft \\Seen)")}


def imap_send(account: dict, secret: dict, msg: EmailMessage) -> dict:
    s = account["settings"]
    try:
        smtp = _smtp(s)
        try:
            smtp.login(s["username"], secret.get("password", ""))
            smtp.send_message(msg)
        finally:
            smtp.quit()
    except (smtplib.SMTPException, OSError) as exc:
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            raise MailError("The mail server refused the recipient address.") from exc
        raise _explain(exc, "SMTP") from exc
    if s.get("save_sent", True):
        try:
            _append(account, secret, "sent", msg, "(\\Seen)")
        except MailError:
            pass  # sent anyway; only the copy in "Sent" is missing
    return {"ok": True, "sent": True}


# ── HTTP APIs (Gmail, Graph) ────────────────────────────────────────────────────────────────────

def _http(method: str, url: str, *, token: str | None = None, body=None, form: dict | None = None,
          headers: dict | None = None) -> dict:
    hdrs = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with _urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code in (401, 403) or "invalid_grant" in detail:
            raise MailError("The mailbox no longer accepts Lisa's access. Connect the account again.") from exc
        if exc.code == 404:
            raise MailError("That e-mail or mailbox was not found.") from exc
        raise MailError(f"The mail service answered with an error ({exc.code}).") from exc
    except OSError as exc:
        raise MailError("Lisa could not reach the mail service. Try again in a minute.") from exc


_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


def _cached_token(key: str, fetch) -> str:
    with _TOKEN_LOCK:
        hit = _TOKENS.get(key)
        if hit and hit[1] > time.time() + 60:
            return hit[0]
    data = fetch()
    token = data.get("access_token")
    if not token:
        raise MailError("The mail service did not hand out an access token.")
    with _TOKEN_LOCK:
        _TOKENS[key] = (token, time.time() + int(data.get("expires_in") or 3000))
    return token


GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_GMAIL_FOLDER = {"inbox": "in:inbox", "sent": "in:sent", "drafts": "in:drafts"}


def _gmail_token(account: dict, secret: dict) -> str:
    from api.enterprise.integrations import _google_client
    client = _google_client()
    return _cached_token("google:" + secret.get("refresh_token", "")[-16:], lambda: _http(
        "POST", client.get("token_uri", "https://oauth2.googleapis.com/token"),
        form={"grant_type": "refresh_token", "refresh_token": secret.get("refresh_token", ""),
              "client_id": client["client_id"], "client_secret": client["client_secret"]}))


def _gmail_headers(payload: dict) -> EmailMessage:
    msg = EmailMessage()
    for h in payload.get("headers", []):
        if h.get("name") in ("From", "To", "Cc", "Subject", "Date", "Message-ID", "Message-Id"):
            msg[h["name"] if h["name"] != "Message-Id" else "Message-ID"] = h.get("value", "")
    return msg


def gmail_test(account: dict, secret: dict) -> dict:
    prof = _http("GET", f"{GMAIL}/profile", token=_gmail_token(account, secret))
    return {"ok": True, "address": prof.get("emailAddress", "")}


def gmail_search(account: dict, secret: dict, query: str, folder: str, limit: int, unread_only: bool) -> list[dict]:
    token = _gmail_token(account, secret)
    q = " ".join(x for x in (_GMAIL_FOLDER.get((folder or "inbox").lower(), ""), "is:unread" if unread_only else "",
                             (query or "").strip()) if x)
    found = _http("GET", f"{GMAIL}/messages?" + urllib.parse.urlencode({"q": q, "maxResults": limit}), token=token)
    out = []
    for m in found.get("messages", [])[:limit]:
        meta = _http("GET", f"{GMAIL}/messages/{m['id']}?format=metadata&metadataHeaders=From&metadataHeaders=To"
                     "&metadataHeaders=Subject&metadataHeaders=Date", token=token)
        msg = _gmail_headers(meta.get("payload", {}))
        out.append({"id": m["id"], "from": str(msg.get("From", "")), "to": str(msg.get("To", "")),
                    "subject": str(msg.get("Subject", "")), "date": _date(msg.get("Date")),
                    "snippet": meta.get("snippet", ""), "unread": "UNREAD" in meta.get("labelIds", [])})
    return out


def gmail_read(account: dict, secret: dict, msg_id: str) -> dict:
    if not re.match(r"^[A-Za-z0-9_-]{6,64}$", str(msg_id or "")):
        raise MailError("Unknown e-mail id: use an id from a search.")
    raw = _http("GET", f"{GMAIL}/messages/{msg_id}?format=raw", token=_gmail_token(account, secret))
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw.get("raw", "") + "==="), policy=email.policy.default)
    out = _full(msg, msg_id)
    out["thread_id"] = raw.get("threadId", "")
    return out


def _gmail_raw(msg: EmailMessage, thread_id: str = "") -> dict:
    out = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        out["threadId"] = thread_id
    return out


def gmail_draft(account: dict, secret: dict, msg: EmailMessage, thread_id: str = "") -> dict:
    d = _http("POST", f"{GMAIL}/drafts", token=_gmail_token(account, secret),
              body={"message": _gmail_raw(msg, thread_id)})
    return {"ok": True, "saved_in": "Drafts", "draft_id": d.get("id", "")}


def gmail_send(account: dict, secret: dict, msg: EmailMessage, thread_id: str = "") -> dict:
    _http("POST", f"{GMAIL}/messages/send", token=_gmail_token(account, secret), body=_gmail_raw(msg, thread_id))
    return {"ok": True, "sent": True}


GRAPH = "https://graph.microsoft.com/v1.0"
_GRAPH_FOLDER = {"inbox": "inbox", "sent": "sentitems", "drafts": "drafts"}


def microsoft_app() -> dict | None:
    """The organisation's Microsoft 365 app (setup wizard / IT), or None when it is not set up."""
    from api.enterprise import provisioner
    env = provisioner._read_env(provisioner.root_home() / ".env")
    get = lambda a, b: env.get(a) or env.get(b) or ""  # noqa: E731
    app = {"tenant": get("MS_GRAPH_TENANT_ID", "MICROSOFT_TENANT_ID"),
           "client_id": get("MS_GRAPH_CLIENT_ID", "MICROSOFT_CLIENT_ID"),
           "secret": get("MS_GRAPH_CLIENT_SECRET", "MICROSOFT_CLIENT_SECRET")}
    return app if all(app.values()) else None


def _graph_token() -> str:
    app = microsoft_app()
    if app is None:
        raise MailError("Microsoft 365 is not set up for this organisation yet. Ask IT.")
    return _cached_token("graph:" + app["client_id"], lambda: _http(
        "POST", f"https://login.microsoftonline.com/{urllib.parse.quote(app['tenant'])}/oauth2/v2.0/token",
        form={"grant_type": "client_credentials", "client_id": app["client_id"], "client_secret": app["secret"],
              "scope": "https://graph.microsoft.com/.default"}))


def _box(account: dict) -> str:
    return f"{GRAPH}/users/{urllib.parse.quote(account['address'])}"


def _recips(value: str) -> list[dict]:
    return [{"emailAddress": {"address": a}} for _n, a in getaddresses([value or ""]) if a]


def _graph_msg(m: dict) -> dict:
    f = (m.get("from") or {}).get("emailAddress") or {}
    to = ", ".join((r.get("emailAddress") or {}).get("address", "") for r in m.get("toRecipients") or [])
    return {"id": m.get("id", ""), "from": formataddr((f.get("name", ""), f.get("address", ""))), "to": to,
            "subject": m.get("subject") or "", "date": m.get("receivedDateTime") or "",
            "snippet": (m.get("bodyPreview") or "")[:200], "unread": not m.get("isRead", True)}


def microsoft_test(account: dict, _secret: dict) -> dict:
    _http("GET", f"{_box(account)}/mailFolders/inbox?$select=id", token=_graph_token())
    return {"ok": True}


def microsoft_search(account: dict, _secret: dict, query: str, folder: str, limit: int, unread_only: bool) -> list[dict]:
    params = {"$top": str(limit), "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview,isRead"}
    if (query or "").strip():
        params["$search"] = '"' + query.strip().replace('"', "") + '"'
    else:
        params["$orderby"] = "receivedDateTime desc"
        if unread_only:
            params["$filter"] = "isRead eq false"
    box = _GRAPH_FOLDER.get((folder or "inbox").lower(), "inbox")
    data = _http("GET", f"{_box(account)}/mailFolders/{box}/messages?" + urllib.parse.urlencode(params),
                 token=_graph_token())
    out = [_graph_msg(m) for m in data.get("value", [])]
    return [m for m in out if m["unread"]] if unread_only and (query or "").strip() else out


def _graph_id(msg_id: str) -> str:
    if not re.match(r"^[A-Za-z0-9_=+/-]{10,300}$", str(msg_id or "")):
        raise MailError("Unknown e-mail id: use an id from a search.")
    return urllib.parse.quote(msg_id, safe="")


def microsoft_read(account: dict, _secret: dict, msg_id: str) -> dict:
    m = _http("GET", f"{_box(account)}/messages/{_graph_id(msg_id)}?$select=id,subject,from,toRecipients,"
              "ccRecipients,receivedDateTime,body,internetMessageId,hasAttachments", token=_graph_token(),
              headers={"Prefer": 'outlook.body-content-type="text"'})
    out = _graph_msg(m)
    out.pop("snippet", None)
    out.pop("unread", None)
    out.update({"cc": ", ".join((r.get("emailAddress") or {}).get("address", "") for r in m.get("ccRecipients") or []),
                "message_id": m.get("internetMessageId", ""), "attachments": ["(has attachments)"] if m.get("hasAttachments") else [],
                "body": ((m.get("body") or {}).get("content") or "")[:MAX_BODY]})
    return out


def _graph_payload(msg: EmailMessage) -> dict:
    return {"subject": str(msg["Subject"]), "body": {"contentType": "Text", "content": msg.get_content()},
            "toRecipients": _recips(str(msg["To"])), "ccRecipients": _recips(str(msg.get("Cc", "")))}


def microsoft_draft(account: dict, _secret: dict, msg: EmailMessage, reply_id: str = "") -> dict:
    token = _graph_token()
    if reply_id:
        d = _http("POST", f"{_box(account)}/messages/{_graph_id(reply_id)}/createReply", token=token,
                  body={"message": _graph_payload(msg)})
    else:
        d = _http("POST", f"{_box(account)}/messages", token=token, body=_graph_payload(msg))
    return {"ok": True, "saved_in": "Drafts", "draft_id": d.get("id", "")}


def microsoft_send(account: dict, _secret: dict, msg: EmailMessage, reply_id: str = "") -> dict:
    token = _graph_token()
    if reply_id:
        _http("POST", f"{_box(account)}/messages/{_graph_id(reply_id)}/reply", token=token,
              body={"message": _graph_payload(msg)})
    else:
        _http("POST", f"{_box(account)}/sendMail", token=token,
              body={"message": _graph_payload(msg), "saveToSentItems": True})
    return {"ok": True, "sent": True}


# ── dispatch ───────────────────────────────────────────────────────────────────────────────────

BACKENDS = {
    "imap": {"test": imap_test, "search": imap_search, "read": imap_read, "draft": imap_draft, "send": imap_send},
    "google": {"test": gmail_test, "search": gmail_search, "read": gmail_read, "draft": gmail_draft,
               "send": gmail_send},
    "microsoft": {"test": microsoft_test, "search": microsoft_search, "read": microsoft_read,
                  "draft": microsoft_draft, "send": microsoft_send},
}


def call(kind: str, op: str, *args, **kwargs):
    try:
        fn = BACKENDS[kind][op]
    except KeyError:
        raise MailError("Unknown kind of mail account.") from None
    return fn(*args, **kwargs)
