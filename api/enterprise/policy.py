"""Which permission each WebUI route needs. Deny by default: a route missing here answers 403.

Built from the Phase 0 route inventory (docs-hermes-lisa-v1/enterprise/phase0/webui-route-inventory.md,
278 method+path branches). Rules match exact paths first, then the longest prefix. When a new
route lands in ``api/routes.py`` it stays forbidden for organisation users until it is added here,
which is the point: nobody gets a feature by accident.
"""
from __future__ import annotations

ANY_USER = "signed-in"

EXACT = {
    "/": "chat.use", "/index.html": "chat.use", "/sessions": "chat.use",
    "/api/": ANY_USER,
    "/api/client-events/log": ANY_USER,
    "/api/btw": "chat.use",
    "/api/insights": "chat.use",
    "/api/folder/download": "files.workspace",
    "/api/default-model": "operator.settings",
    "/api/admin/reload": "operator.system",
    "/api/auth/logout": ANY_USER,
    # Bound to the user's own profile: appearance / composer footer / personal prefs.
    # Operator-only surfaces (providers, MCP, gateway) stay on their own routes.
    "/api/settings": "chat.use",
    "/api/health/detailed": "operator.system",
    "/api/health": ANY_USER,
    "/api/profiles": ANY_USER,
    "/lisa": ANY_USER,  # account + organisation page; its API calls carry their own permissions  # filtered to the user's own profile for bound requests
}

# (prefix, permission). Longest prefix wins, so specific rules can sit under general ones.
PREFIXES = (
    ("/api/enterprise/", ANY_USER),  # api/enterprise/routes.py checks its own permissions
    ("/api/share/create", "share.create"), ("/api/share/revoke", "share.create"),
    ("/api/crons", "cron.own"),
    ("/api/memory", "memory.own"),
    ("/api/email-assistant", "integration.email"),
    ("/api/kanban", "kanban.use"),
    ("/api/file", "files.workspace"), ("/api/workspace", "files.workspace"), ("/api/notes", "files.workspace"),
    ("/api/wiki", "files.workspace"), ("/api/projects", "files.workspace"), ("/api/project-os", "files.workspace"),
    ("/api/upload", "chat.use"), ("/api/session", "chat.use"), ("/api/sessions", "chat.use"),
    ("/sessions/", "chat.use"), ("/session/", "chat.use"), ("/api/chat", "chat.use"),
    ("/api/clarify", "chat.use"), ("/api/approval", "chat.use"),
    ("/api/escape", "operator.terminal"),  # browses the host outside any workspace
    ("/api/background", "chat.use"), ("/api/bg-task", "chat.use"), ("/api/process-complete-ack", "chat.use"),
    ("/api/goal", "chat.use"), ("/api/reasoning", "chat.use"), ("/api/transcribe", "chat.use"),
    ("/api/tts", "chat.use"), ("/api/media", "chat.use"), ("/api/commands", "chat.use"),
    ("/api/prompts", "chat.use"), ("/api/personalit", "chat.use"), ("/api/list", "chat.use"),
    ("/api/models", "chat.use"), ("/api/model", "chat.use"), ("/api/todo", "chat.use"),
    ("/api/auth/", ANY_USER),
    ("/embed/voice", "chat.use"),  # voice proxy; the session is bound to the user's profile by ticket
    ("/api/skills", "operator.skills"), ("/api/terminal", "operator.terminal"), ("/api/git", "operator.git"),
    ("/api/mcp", "operator.mcp"), ("/api/provider", "operator.providers"),
    ("/api/profile", "operator.profiles"),
    ("/api/updates", "operator.updates"), ("/api/rollback", "operator.updates"),
    ("/api/extensions", "operator.extensions"), ("/extensions/", "operator.extensions"),
    ("/api/plugins", "operator.extensions"), ("/plugins/", "operator.extensions"),
    ("/dashboard-plugins/", "operator.extensions"),
    ("/api/gateway", "operator.gateway"), ("/api/logs", "operator.logs"), ("/api/system", "operator.system"),
    ("/api/shutdown", "operator.system"), ("/api/dashboard", "operator.system"),
    ("/api/customer-wizard", "operator.setup"), ("/api/onboarding", "operator.setup"),
)
_SORTED = sorted(PREFIXES, key=lambda rule: len(rule[0]), reverse=True)

# The page shell asks for these to render, whatever panels the user may open.
SHELL_READS = frozenset({"/api/profile/active", "/api/profiles", "/api/settings", "/api/onboarding/status",
                         "/api/updates/check", "/api/skills"})


def required(method: str, path: str) -> str | None:
    """Permission a request needs, or None when no rule exists (deny)."""
    if method == "GET" and path in SHELL_READS:
        return ANY_USER
    if path in EXACT:
        return EXACT[path]
    for prefix, permission in _SORTED:
        if path.startswith(prefix):
            return permission
    return None


def allowed(user, method: str, path: str) -> bool:
    need = required(method, path)
    if need is None:
        return False
    return need == ANY_USER or user.can(need)
