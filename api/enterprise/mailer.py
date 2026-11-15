"""Outgoing mail for Lisa Enterprise: invitations and password resets.

SMTP settings are secrets and come from the stack's ``.env``: ``LISA_SMTP_HOST``, ``LISA_SMTP_PORT``
(587), ``LISA_SMTP_USER``, ``LISA_SMTP_PASSWORD``, ``LISA_SMTP_FROM``, ``LISA_SMTP_TLS``
(``starttls`` | ``ssl`` | ``none``). Links use ``LISA_PUBLIC_URL`` (the address users open, behind
TLS). Without a host, mail is off and administrators hand out links themselves.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def configured() -> bool:
    return bool(os.environ.get("LISA_SMTP_HOST", "").strip() and os.environ.get("LISA_PUBLIC_URL", "").strip())


def public_link(path: str) -> str:
    return os.environ.get("LISA_PUBLIC_URL", "").rstrip("/") + path


def send(to: str, subject: str, body: str) -> None:
    host = os.environ["LISA_SMTP_HOST"].strip()
    port = int(os.environ.get("LISA_SMTP_PORT", "587"))
    mode = os.environ.get("LISA_SMTP_TLS", "starttls").strip().lower()
    msg = EmailMessage()
    msg["From"] = os.environ.get("LISA_SMTP_FROM", "").strip() or f"Lisa <no-reply@{host}>"
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    context = ssl.create_default_context()
    smtp = smtplib.SMTP_SSL(host, port, context=context, timeout=20) if mode == "ssl" else \
        smtplib.SMTP(host, port, timeout=20)
    with smtp:
        if mode == "starttls":
            smtp.starttls(context=context)
        user = os.environ.get("LISA_SMTP_USER", "").strip()
        if user:
            smtp.login(user, os.environ.get("LISA_SMTP_PASSWORD", ""))
        smtp.send_message(msg)


def send_password_link(to: str, name: str, token: str, *, invitation: bool) -> None:
    link = public_link(f"/set-password?token={token}")
    if invitation:
        subject = "Your Lisa account"
        text = (f"Hello {name or to},\n\nan account was created for you in Lisa, your company assistant.\n"
                f"Choose your password here (the link works once, for 48 hours):\n\n{link}\n")
    else:
        subject = "Reset your Lisa password"
        text = (f"Hello {name or to},\n\nsomeone asked to reset the password of this Lisa account.\n"
                f"If it was you, choose a new password here (the link works once, for one hour):\n\n{link}\n\n"
                "If it was not you, ignore this e-mail; your password stays unchanged.\n")
    send(to, subject, text)
