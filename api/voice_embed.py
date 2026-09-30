"""Same-origin proxy for the Matika voice UI.

The browser only ever requests ``/embed/voice/`` on this WebUI. The upstream
host stays in server configuration (and optionally each profile's ``.env``)
and is stripped from HTML, scripts, and redirects before anything is written
back to the client.
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request

_DEFAULT_UPSTREAM = "https://matika-voice.fusiontrade.at/voice"
_PAGE_PATHS = {"/embed/voice", "/embed/voice/"}
_API_NAMES = {"session", "login", "logout", "offer", "client-config"}
_VOICE_COOKIES = {"matika_voice_session", "matika_voice_csrf"}
_MAX_BODY = 2 * 1024 * 1024
_MAX_RESPONSE = 5 * 1024 * 1024
_TIMEOUT_S = 25
_REWRITE_TYPES = {
    "text/html",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/x-javascript",
}
_PROFILE_ENV_KEY = "MATIKA_VOICE_UPSTREAM"
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def is_voice_embed_path(path: str) -> bool:
    return (
        path == "/embed/voice"
        or path.startswith("/embed/voice/")
        or path == "/embed/voice-api"
        or path.startswith("/embed/voice-api/")
    )


def parse_upstream(raw: str) -> tuple[str, str]:
    """Parse an upstream URL into ``(origin, ui_prefix)``.

    Accepts either an origin (``https://voice.example``) or a page base
    (``https://voice.example/voice``). A bare origin defaults the UI prefix
    to ``/voice``.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("empty voice upstream")
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    host = parsed.hostname or ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid voice upstream")
    if parsed.scheme == "https" and host:
        origin = f"https://{parsed.netloc}"
    elif parsed.scheme == "http" and host in {"127.0.0.1", "localhost"}:
        origin = f"http://{parsed.netloc}"
    else:
        raise ValueError("invalid voice upstream")
    path = (parsed.path or "").rstrip("/")
    if not path:
        return origin, "/voice"
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in {".", ".."} or not _safe_token(part) for part in parts):
        raise ValueError("invalid voice upstream path")
    return origin, "/" + "/".join(parts)


def resolve_upstream(handler=None) -> tuple[str, str]:
    """Resolve the active profile's voice upstream, then fall back to process env."""
    candidates: list[str] = []
    profile = _active_profile_name()
    if profile:
        profile_value = _profile_env_value(profile, _PROFILE_ENV_KEY)
        if profile_value:
            candidates.append(profile_value)
        process_profile = os.environ.get(f"{_PROFILE_ENV_KEY}_{_env_profile_suffix(profile)}", "").strip()
        if process_profile:
            candidates.append(process_profile)
    global_value = os.environ.get(_PROFILE_ENV_KEY, "").strip()
    if global_value:
        candidates.append(global_value)
    candidates.append(_DEFAULT_UPSTREAM)
    last_error: Exception | None = None
    for raw in candidates:
        try:
            return parse_upstream(raw)
        except ValueError as exc:
            last_error = exc
            continue
    raise ValueError(str(last_error or "invalid voice upstream"))


def map_upstream_path(path: str, ui_prefix: str = "/voice") -> str | None:
    """Map an embed path to an upstream path, or None when it must not be proxied."""
    if not path or "\\" in path or any(part == ".." for part in path.split("/")):
        return None
    prefix = ui_prefix.rstrip("/") or "/voice"
    if path in _PAGE_PATHS:
        return prefix + "/"
    static_prefix = "/embed/voice/static/"
    if path.startswith(static_prefix):
        rest = path[len(static_prefix):]
        if not rest or not _safe_static_rest(rest):
            return None
        return f"{prefix}/static/{rest}"
    api_prefix = "/embed/voice-api/"
    if path.startswith(api_prefix):
        name = path[len(api_prefix):]
        if name not in _API_NAMES:
            return None
        return "/api/" + name
    return None


def upstream_origin() -> str:
    """Compatibility helper used by older unit tests."""
    return resolve_upstream()[0]


def rewrite_embed_text(text: str, origin: str, ui_prefix: str = "/voice") -> str:
    """Remove the upstream host and retarget absolute voice paths at the embed prefix."""
    origin = origin.rstrip("/")
    prefix = ui_prefix.rstrip("/") or "/voice"
    text = text.replace(origin + prefix + "/static/", "\x00static/")
    text = text.replace(prefix + "/static/", "\x00static/")
    text = text.replace(origin + prefix + "/", "\x00page/")
    text = text.replace(origin + prefix, "\x00page/")
    text = text.replace(origin + "/api/", "/embed/voice-api/")
    text = text.replace(origin, "")
    text = text.replace('"/api/', '"/embed/voice-api/')
    text = text.replace("'/api/", "'/embed/voice-api/")
    text = text.replace("`/api/", "`/embed/voice-api/")
    text = text.replace("\x00static/", "/embed/voice/static/")
    text = text.replace("\x00page/", "/embed/voice/")
    return text


def filter_request_cookie(header: str) -> str:
    kept = []
    for part in header.split(";"):
        piece = part.strip()
        if not piece or "=" not in piece:
            continue
        name = piece.split("=", 1)[0].strip()
        if name in _VOICE_COOKIES:
            kept.append(piece)
    return "; ".join(kept)


def rewrite_set_cookie(value: str) -> str | None:
    if "=" not in value:
        return None
    name = value.split("=", 1)[0].strip()
    if name not in _VOICE_COOKIES:
        return None
    bits = [bit.strip() for bit in value.split(";") if bit.strip()]
    rewritten = [bits[0]]
    saw_path = False
    for bit in bits[1:]:
        key = bit.split("=", 1)[0].strip().lower()
        if key == "domain":
            continue
        if key == "path":
            rewritten.append("Path=/embed")
            saw_path = True
            continue
        rewritten.append(bit)
    if not saw_path:
        rewritten.append("Path=/embed")
    return "; ".join(rewritten)


def proxy_voice_embed(handler, parsed, method: str) -> None:
    path = parsed.path or ""
    if method == "GET" and path in _PAGE_PATHS:
        dest = (handler.headers.get("Sec-Fetch-Dest") or "").strip().lower()
        if dest == "document":
            _send_bytes(handler, 404, b"", "text/plain; charset=utf-8")
            return
    try:
        origin, ui_prefix = resolve_upstream(handler)
    except ValueError:
        _send_unavailable(handler, 503, api=_is_api(path))
        return
    upstream_path = map_upstream_path(path, ui_prefix)
    if upstream_path is None:
        _send_unavailable(handler, 404, api=_is_api(path))
        return
    query = parsed.query or ""
    if query and not _safe_query(query):
        _send_unavailable(handler, 400, api=_is_api(path))
        return
    body = b""
    if method in {"POST", "PATCH"}:
        try:
            body = _read_body(handler)
        except ValueError:
            _send_unavailable(handler, 413, api=True)
            return
    target = origin + upstream_path
    if query:
        target += "?" + query
    request = urllib.request.Request(
        target,
        data=body if method in {"POST", "PATCH"} else None,
        headers=_forward_headers(handler, origin, ui_prefix),
        method=method,
    )
    try:
        status, headers, payload = _open_upstream(request)
    except Exception:
        _send_unavailable(handler, 502, api=_is_api(path))
        return
    if len(payload) > _MAX_RESPONSE:
        _send_unavailable(handler, 502, api=_is_api(path))
        return
    content_type = headers.get("Content-Type", "application/octet-stream")
    if _should_rewrite(content_type):
        payload = rewrite_embed_text(
            payload.decode("utf-8", "replace"),
            origin,
            ui_prefix,
        ).encode("utf-8")
    _write_upstream_response(handler, status, headers, payload, content_type, origin, ui_prefix)


def _active_profile_name() -> str:
    try:
        from api.profiles import get_active_profile_name

        name = (get_active_profile_name() or "").strip()
    except Exception:
        return ""
    if not name or not _PROFILE_NAME_RE.fullmatch(name):
        return ""
    return name


def _env_profile_suffix(profile: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", profile).upper()


def _profile_env_value(profile: str, key: str) -> str:
    try:
        from api.profiles import get_hermes_home_for_profile, get_profile_runtime_env

        env = get_profile_runtime_env(get_hermes_home_for_profile(profile))
    except Exception:
        return ""
    value = env.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _safe_static_rest(rest: str) -> bool:
    if rest.startswith("/") or rest.endswith("/"):
        return False
    parts = rest.split("/")
    return all(part and part not in {".", ".."} and _safe_token(part) for part in parts)


def _safe_token(value: str) -> bool:
    return all(ch.isalnum() or ch in "._-" for ch in value)


def _safe_query(query: str) -> bool:
    return all(ch.isalnum() or ch in "._~%=&+-" for ch in query)


def _is_api(path: str) -> bool:
    return path.startswith("/embed/voice-api")


def _should_rewrite(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() in _REWRITE_TYPES


def _read_body(handler) -> bytes:
    raw_length = handler.headers.get("Content-Length", "0") or "0"
    try:
        length = int(raw_length)
    except ValueError as exc:
        raise ValueError("invalid length") from exc
    if length < 0 or length > _MAX_BODY:
        raise ValueError("too large")
    if length == 0:
        return b""
    return handler.rfile.read(length)


def _forward_headers(handler, origin: str, ui_prefix: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(origin)
    prefix = ui_prefix.rstrip("/") or "/voice"
    headers = {
        "Host": parsed.netloc,
        "Accept": handler.headers.get("Accept", "*/*"),
        "User-Agent": "hermes-webui-voice-embed",
        "Origin": origin,
        "Referer": origin + prefix + "/",
    }
    content_type = handler.headers.get("Content-Type")
    if content_type:
        headers["Content-Type"] = content_type
    csrf = handler.headers.get("X-CSRF-Token")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    cookie = filter_request_cookie(handler.headers.get("Cookie", ""))
    if cookie:
        headers["Cookie"] = cookie
    accept_language = handler.headers.get("Accept-Language")
    if accept_language:
        headers["Accept-Language"] = accept_language
    return headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_upstream(request: urllib.request.Request) -> tuple[int, object, bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        payload = exc.read(_MAX_RESPONSE + 1)
        return exc.code, exc.headers, payload
    with response:
        return response.status, response.headers, response.read(_MAX_RESPONSE + 1)


def _write_upstream_response(
    handler,
    status: int,
    headers,
    payload: bytes,
    content_type: str,
    origin: str,
    ui_prefix: str,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Security-Policy", "frame-ancestors 'self'")
    location = headers.get("Location")
    if location:
        rewritten = rewrite_embed_text(location, origin, ui_prefix)
        if rewritten.startswith("/") and not rewritten.startswith("//"):
            handler.send_header("Location", rewritten)
    for value in headers.get_all("Set-Cookie") or []:
        cookie = rewrite_set_cookie(value)
        if cookie:
            handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    if payload:
        handler.wfile.write(payload)


def _send_unavailable(handler, status: int, *, api: bool) -> None:
    if api:
        body = b'{"detail":"Voice is unavailable"}'
        _send_bytes(handler, status, body, "application/json; charset=utf-8")
        return
    body = b"Voice is unavailable"
    _send_bytes(handler, status, body, "text/plain; charset=utf-8")


def _send_bytes(handler, status: int, body: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Security-Policy", "frame-ancestors 'self'")
    handler.end_headers()
    if body:
        handler.wfile.write(body)
