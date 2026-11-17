"""Encryption at rest for the secrets Lisa stores on disk (mail passwords and tokens, the company
vault's access token and deploy key).

The key is ``LISA_SECRET_KEY`` from the stack's ``.env`` (install.sh / upgrade.sh generate it), which
no backup contains: a stolen backup or knowledge volume alone holds only ciphertext. Without it (a
development install), a key file in the WebUI state directory is used, which still keeps the key off
the knowledge volume where the secrets live. Files written before encryption existed are read as
plain text and encrypted on their next write (``upgrade_file``).
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_PREFIX = b"lisa-enc:1:"


def _key_file() -> Path:
    from api.config import STATE_DIR
    return Path(STATE_DIR) / "lisa-secret.key"


def from_env() -> bool:
    return len(os.environ.get("LISA_SECRET_KEY", "").strip()) >= 32


def _derive(raw: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def _file_key(create: bool) -> str | None:
    path = _key_file()
    if not path.is_file():
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return path.read_text().strip()


def _keys() -> list[Fernet]:
    """Encrypts with LISA_SECRET_KEY (or the state-directory key without it); also decrypts what the
    state-directory key wrote before LISA_SECRET_KEY was added, so adding it never loses a secret."""
    if from_env():
        keys = [os.environ["LISA_SECRET_KEY"].strip(), _file_key(create=False)]
    else:
        keys = [_file_key(create=True)]
    return [_derive(k) for k in keys if k]


def _fernet() -> MultiFernet:
    return MultiFernet(_keys())


def seal(data: bytes) -> bytes:
    return _PREFIX + _fernet().encrypt(data)


def is_sealed(blob: bytes) -> bool:
    return blob.startswith(_PREFIX)


def open_(blob: bytes) -> bytes:
    """Plain bytes of a stored secret; legacy unencrypted files are returned as they are."""
    if not is_sealed(blob):
        return blob
    try:
        return _fernet().decrypt(blob[len(_PREFIX):])
    except InvalidToken as exc:
        raise RuntimeError("A stored secret cannot be decrypted: LISA_SECRET_KEY differs from the one "
                           "it was saved with") from exc


def write(path: Path, data: bytes) -> None:
    """Encrypt ``data`` into ``path`` (0600), replacing it atomically."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(seal(data))
    os.replace(tmp, path)


def read(path: Path) -> bytes:
    return open_(path.read_bytes())


def upgrade_file(path: Path) -> bool:
    """Encrypt a secret file still stored in plain text, or re-encrypt one written with an older key
    with the current one; True when it was rewritten."""
    if not path.is_file():
        return False
    blob = path.read_bytes()
    if is_sealed(blob):
        token = blob[len(_PREFIX):]
        try:
            _keys()[0].decrypt(token)
            return False
        except InvalidToken:
            blob = open_(blob)
    write(path, blob)
    return True
