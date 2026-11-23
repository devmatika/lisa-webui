"""Personal Google Drive API: browse vs write permission, owner isolation, Shared drives roots."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("argon2")


@pytest.fixture()
def profile_home(tmp_path, monkeypatch):
    from api.enterprise import provisioner
    root = tmp_path / "hermes"
    home = root / "profiles" / "u-anna"
    home.mkdir(parents=True)
    (home / "lisa-owner.json").write_text(json.dumps({"user_id": "anna"}))
    (home / "workspace").mkdir()
    monkeypatch.setattr(provisioner, "root_home", lambda: root)
    monkeypatch.setattr(provisioner, "_home", lambda name: root / "profiles" / name)
    return home


def _token(home: Path):
    from api.enterprise import integrations
    payload = {
        "type": "authorized_user", "client_id": "cid", "client_secret": "sec",
        "refresh_token": "rt", "token": "old", "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    }
    path = home / integrations.GOOGLE_TOKEN
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def test_drive_status_separates_browse_and_write(profile_home):
    from api.enterprise import drive
    _token(profile_home)
    assert drive.status("u-anna", can_browse=True, can_write=False)["write_allowed"] is False
    assert drive.status("u-anna", can_browse=True, can_write=True)["write_allowed"] is True
    assert drive.status("u-anna", can_browse=False, can_write=True)["write_allowed"] is False


def test_drive_refuses_another_users_profile(profile_home, tmp_path):
    from api.enterprise import drive, directory
    _token(profile_home)
    with pytest.raises(directory.DirectoryError, match="another person's"):
        drive.roots("u-anna", "not-anna")


def test_drive_list_builds_shared_drive_query(profile_home):
    from api.enterprise import drive
    _token(profile_home)
    calls = []

    def fake_request(profile, method, path, *, query=None, body=None, content_type=None, raw=False):
        calls.append((method, path, query))
        if path == "/files":
            return {"files": [{"id": "f1", "name": "Note.md", "mimeType": "text/markdown"}]}
        return {}

    with mock.patch.object(drive, "_drive_request", side_effect=fake_request):
        with mock.patch.object(drive, "_access_token", return_value="atok"):
            out = drive.list_files("u-anna", "anna", corpus="drive", drive_id="sd1", folder_id="sd1")
    assert out["items"][0]["name"] == "Note.md"
    assert calls[-1][1] == "/files"
    assert calls[-1][2]["corpora"] == "drive"
    assert calls[-1][2]["driveId"] == "sd1"
    assert "sd1" in calls[-1][2]["q"]


def test_upload_requires_workspace_file(profile_home):
    from api.enterprise import drive, directory
    _token(profile_home)
    (profile_home / "workspace" / "offer.md").write_text("# hi")
    with mock.patch.object(drive, "_access_token", return_value="atok"):
        with mock.patch("urllib.request.urlopen") as urlopen:
            resp = mock.MagicMock()
            resp.__enter__.return_value = resp
            resp.read.return_value = json.dumps({
                "id": "up1", "name": "offer.md", "webViewLink": "https://drive.google.com/file/d/up1"
            }).encode()
            urlopen.return_value = resp
            out = drive.upload_from_workspace("u-anna", "anna", relative_path="offer.md", parent_id="folder1")
    assert out["ok"] and out["id"] == "up1"
    with pytest.raises(directory.DirectoryError):
        drive.upload_from_workspace("u-anna", "anna", relative_path="../escape.md")


def test_provisioner_sets_drive_upload_only_with_drive_write():
    from api.enterprise.provisioner import render_config
    off = render_config({}, ["memory"], "u-x", ["integration.google"], "a@x.com",
                        permissions={"integration.google"})
    caps = off["plugins"]["entries"]["google-workspace-tools"]["settings"]["capabilities"]
    assert caps["drive_read"] is True and caps["drive_upload"] is False
    assert caps["calendar_create"] is True and caps["docs_create"] is True
    assert caps["sheets_create"] is True and caps["calendar_read"] is True
    on = render_config({}, ["memory"], "u-x", ["integration.google"], "a@x.com",
                       permissions={"integration.google", "integration.google.drive_write"})
    assert on["plugins"]["entries"]["google-workspace-tools"]["settings"]["capabilities"]["drive_upload"] is True


def test_permission_catalog_includes_drive_write():
    from api.enterprise.directory import PERMISSIONS
    assert "integration.google.drive_write" in PERMISSIONS
