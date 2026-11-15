"""Break-glass: give a role back to a user from the server shell, when nobody signed in can.

    docker compose exec webui /app/venv/bin/python -m api.enterprise.recover <email> [role ...]   (default: admin operator)

Adds roles, never removes them, re-activates the account and records ``user.recovered`` in the
audit log. Needs shell access to the host, which already implies full control of the stack.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from api.enterprise import db as edb
from api.enterprise import directory
from api.enterprise.models import User


def recover(email: str, role_keys: list[str]) -> list[str]:
    with edb.session() as db:
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None:
            raise directory.DirectoryError(f"No user with e-mail {email}")
        keys = sorted({r.key for r in user.roles} | set(role_keys))
        user.roles = directory._roles(db, keys)
        user.status = "active"
        directory.audit(db, directory.Actor(None, "server-shell"), "user.recovered", user.id, roles=keys)
        return keys


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 2
    try:
        keys = recover(argv[0], argv[1:] or ["admin", "operator"])
    except directory.DirectoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{argv[0]} now has roles: {', '.join(keys)}. Reload the page to see the change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
