"""Password hashing (Argon2id) and opaque tokens. Only token hashes are ever stored."""
from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 10
_PH = PasswordHasher()
# Verified against when the email is unknown, so a miss costs the same time as a wrong password.
_DUMMY_HASH = _PH.hash(secrets.token_hex(16))


class PasswordPolicyError(ValueError):
    pass


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _PH.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _PH.verify(stored_hash or _DUMMY_HASH, password or "") and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
