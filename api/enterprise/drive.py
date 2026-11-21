"""Personal Google Drive browse/import/upload for Lisa Enterprise (user OAuth only).

Uses the signed-in user's own ``google_token.json``. No company service account.
Browse needs ``integration.google``; upload needs ``integration.google.drive_write``.
"""
from __future__ import annotations

import json
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from api.enterprise import integrations, provisioner
from api.enterprise.directory import DirectoryError

FOLDER_MIME = "application/vnd.google-apps.folder"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")
_FIELDS = "files(id,name,mimeType,modifiedTime,size,webViewLink,iconLink,parents),nextPageToken"
_META_FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,iconLink,parents,owners(emailAddress),driveId"
_MAX_IMPORT = 25 * 1024 * 1024
_MAX_UPLOAD = 25 * 1024 * 1024


def _token_path(profile: str) -> Path:
    return provisioner._home(profile) / integrations.GOOGLE_TOKEN


def _assert_owner(profile: str, user_id: str) -> None:
    owner = provisioner._home(profile) / "lisa-owner.json"
    if not owner.is_file():
        raise DirectoryError("This assistant profile is not linked to a Lisa user")
    try:
        data = json.loads(owner.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DirectoryError("This assistant profile is not linked to a Lisa user") from exc
    if str(data.get("user_id") or "") != str(user_id):
        raise DirectoryError("Refusing to use another person's Google account")


def _access_token(profile: str) -> str:
    data = integrations.load_google_token(profile)
    if data is None:
        raise DirectoryError("Connect Google under My account first")
    cached = str(data.get("token") or "").strip()
    # Always refresh: cached access tokens expire; refresh_token is the durable secret.
    form = urllib.parse.urlencode({
        "client_id": data["client_id"], "client_secret": data["client_secret"],
        "refresh_token": data["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        data.get("token_uri") or "https://oauth2.googleapis.com/token",
        data=form, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise DirectoryError("Google refused to renew access; reconnect Google under My account") from None
    token = str(out.get("access_token") or "").strip()
    if not token:
        raise DirectoryError("Google did not return an access token; reconnect Google")
    integrations.save_google_access_token(profile, token)
    return token or cached


def _drive_request(profile: str, method: str, path: str, *, query: dict | None = None,
                   body: bytes | None = None, content_type: str | None = None,
                   raw: bool = False):
    token = _access_token(profile)
    url = "https://www.googleapis.com/drive/v3" + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None and v != ""})
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            return data if raw else (json.loads(data) if data else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code in (401, 403):
            raise DirectoryError("Google Drive access denied for this account or folder") from None
        raise DirectoryError(f"Google Drive error ({exc.code}): {detail or exc.reason}") from None


def status(profile: str, *, can_browse: bool, can_write: bool) -> dict:
    connected = integrations.google_connected(profile)
    return {
        "connected": connected,
        "browse_allowed": bool(can_browse),
        "write_allowed": bool(can_browse and can_write),
        "ready": connected and can_browse,
    }


def roots(profile: str, user_id: str) -> dict:
    _assert_owner(profile, user_id)
    drives = _drive_request(profile, "GET", "/drives", query={"pageSize": "50", "fields": "drives(id,name)"})
    return {
        "roots": [
            {"id": "root", "name": "My Drive", "corpus": "user", "drive_id": ""},
            {"id": "sharedWithMe", "name": "Shared with me", "corpus": "sharedWithMe", "drive_id": ""},
            *[{"id": d["id"], "name": d.get("name") or d["id"], "corpus": "drive", "drive_id": d["id"]}
              for d in (drives.get("drives") or []) if d.get("id")],
        ]
    }


def _list_query(*, corpus: str, drive_id: str, folder_id: str) -> tuple[str, dict]:
    """Build q + list params for My Drive / Shared with me / a Shared drive folder."""
    params: dict[str, str] = {
        "pageSize": "100",
        "fields": _FIELDS,
        "orderBy": "folder,name_natural",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    parent = (folder_id or "").strip()
    if corpus == "sharedWithMe" and (not parent or parent in {"sharedWithMe", "root"}):
        params["q"] = "sharedWithMe = true and trashed = false"
        params["spaces"] = "drive"
        return params["q"], params
    if corpus == "drive" and drive_id:
        params["corpora"] = "drive"
        params["driveId"] = drive_id
        fid = parent if parent and parent != drive_id else drive_id
        params["q"] = f"'{fid}' in parents and trashed = false"
        return params["q"], params
    # My Drive (user corpus)
    fid = parent if parent and parent not in {"root", "myDrive"} else "root"
    params["q"] = f"'{fid}' in parents and trashed = false"
    params["spaces"] = "drive"
    return params["q"], params


def list_files(profile: str, user_id: str, *, corpus: str = "user", drive_id: str = "",
               folder_id: str = "") -> dict:
    _assert_owner(profile, user_id)
    _, params = _list_query(corpus=corpus or "user", drive_id=drive_id or "", folder_id=folder_id or "")
    data = _drive_request(profile, "GET", "/files", query=params)
    items = []
    for f in data.get("files") or []:
        mime = f.get("mimeType") or ""
        items.append({
            "id": f.get("id"), "name": f.get("name") or "", "mimeType": mime,
            "folder": mime == FOLDER_MIME, "modifiedTime": f.get("modifiedTime"),
            "size": f.get("size"), "webViewLink": f.get("webViewLink"),
            "iconLink": f.get("iconLink"),
        })
    return {"items": items, "nextPageToken": data.get("nextPageToken")}


def meta(profile: str, user_id: str, file_id: str) -> dict:
    _assert_owner(profile, user_id)
    if not (file_id or "").strip():
        raise DirectoryError("file_id is required")
    return _drive_request(profile, "GET", f"/files/{urllib.parse.quote(file_id)}",
                          query={"fields": _META_FIELDS, "supportsAllDrives": "true"})


def _safe_filename(name: str) -> str:
    base = _SAFE_NAME.sub("_", (name or "file").strip())[:120].strip(" ._") or "file"
    return base


def import_to_workspace(profile: str, user_id: str, file_id: str, *, dest_dir: str = "drive-imports") -> dict:
    """Download a Drive file into the user's Hermes workspace (for preview/attach)."""
    _assert_owner(profile, user_id)
    info = meta(profile, user_id, file_id)
    mime = info.get("mimeType") or ""
    name = _safe_filename(info.get("name") or file_id)
    home = provisioner._home(profile)
    workspace = home / "workspace"
    target_dir = (workspace / (dest_dir or "drive-imports")).resolve()
    try:
        target_dir.relative_to(workspace.resolve())
    except ValueError as exc:
        raise DirectoryError("Import destination must stay under the workspace") from exc
    target_dir.mkdir(parents=True, exist_ok=True)

    native = {
        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
        "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.drawing": ("image/png", ".png"),
    }
    if mime == FOLDER_MIME:
        raise DirectoryError("Folders cannot be imported; open them in Drive")
    if mime in native:
        export_mime, ext = native[mime]
        if not name.endswith(ext):
            name += ext
        path = f"/files/{urllib.parse.quote(file_id)}/export"
        raw = _drive_request(profile, "GET", path, query={"mimeType": export_mime}, raw=True)
    else:
        raw = _drive_request(profile, "GET", f"/files/{urllib.parse.quote(file_id)}",
                             query={"alt": "media", "supportsAllDrives": "true"}, raw=True)
    if len(raw) > _MAX_IMPORT:
        raise DirectoryError(f"File is too large to import (max {_MAX_IMPORT} bytes)")
    out = target_dir / name
    out.write_bytes(raw)
    rel = out.relative_to(workspace).as_posix()
    return {"ok": True, "path": rel, "name": name, "bytes": len(raw), "mimeType": mime,
            "webViewLink": info.get("webViewLink")}


def upload_from_workspace(profile: str, user_id: str, *, relative_path: str, parent_id: str = "",
                          name: str = "") -> dict:
    """Upload a workspace file to Drive. Caller must have already checked drive_write."""
    _assert_owner(profile, user_id)
    home = provisioner._home(profile)
    workspace = (home / "workspace").resolve()
    rel = (relative_path or "").strip().lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise DirectoryError("A workspace-relative file path is required")
    local = (workspace / rel).resolve()
    try:
        local.relative_to(workspace)
    except ValueError as exc:
        raise DirectoryError("Upload path must stay under the workspace") from exc
    if not local.is_file():
        raise DirectoryError("File not found in workspace")
    size = local.stat().st_size
    if size > _MAX_UPLOAD:
        raise DirectoryError(f"File too large for Drive upload (max {_MAX_UPLOAD} bytes)")
    fname = (name or local.name).strip() or local.name
    mime = mimetypes.guess_type(str(local))[0] or "application/octet-stream"
    metadata = {"name": fname}
    if parent_id and parent_id not in {"root", "myDrive", "sharedWithMe"}:
        metadata["parents"] = [parent_id]
    boundary = "lisa_drive_upload"
    meta_json = json.dumps(metadata)
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta_json}\r\n"
        f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n"
    ).encode() + local.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    token = _access_token(profile)
    url = ("https://www.googleapis.com/upload/drive/v3/files?"
           + urllib.parse.urlencode({"uploadType": "multipart", "supportsAllDrives": "true",
                                     "fields": "id,name,mimeType,webViewLink"}))
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": f"multipart/related; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code in (401, 403):
            raise DirectoryError("Google Drive refused the upload (permission or Shared drive access)") from None
        raise DirectoryError(f"Google Drive upload failed ({exc.code}): {detail or exc.reason}") from None
    return {"ok": True, "id": data.get("id"), "name": data.get("name") or fname,
            "mimeType": data.get("mimeType") or mime, "webViewLink": data.get("webViewLink")}
