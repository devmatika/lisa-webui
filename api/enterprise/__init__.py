"""Lisa Enterprise: organisation users, roles and per-user Hermes profiles.

Off unless ``LISA_ENTERPRISE`` is truthy. When off, nothing in this package is imported on the request
path and the WebUI behaves exactly as the single-operator Lisa. When on, Postgres
(``LISA_DATABASE_URL``) is the source of truth for who a request is and which Hermes profile it may
use; every failure to reach it denies (fail closed).

Modules: ``models`` (schema), ``db`` (engine + migrations), ``security`` (hashing, tokens),
``directory`` (users, roles, first-run setup, audit), ``sessions`` (login/verify/revoke),
``provisioner`` (one Hermes profile per user), ``gate`` (request authentication + profile binding),
``routes`` (HTTP API).
"""
from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.environ.get("LISA_ENTERPRISE", "").strip().lower() in _TRUTHY
