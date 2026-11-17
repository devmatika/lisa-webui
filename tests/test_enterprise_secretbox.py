"""Lisa Enterprise: passwords and tokens Lisa stores on disk are encrypted, older plain files get
encrypted on start, and adding LISA_SECRET_KEY to an install that ran without it loses nothing."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("cryptography")


@pytest.fixture()
def box(tmp_path, monkeypatch):
    from api import config
    from api.enterprise import secretbox
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("LISA_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.delenv("LISA_SECRET_KEY", raising=False)
    return secretbox


def test_a_stored_mail_password_is_not_readable_on_disk(box, tmp_path):
    from api.enterprise import mail
    mail._save_secret("a" * 32, {"password": "Sommer-2026!"})
    on_disk = (tmp_path / "knowledge" / ".mail" / ("a" * 32 + ".json")).read_bytes()
    assert b"Sommer-2026!" not in on_disk and box.is_sealed(on_disk)
    assert mail._secret("a" * 32) == {"password": "Sommer-2026!"}


def test_plain_files_from_an_older_version_are_encrypted_on_start(box, tmp_path):
    from api.enterprise import mail, org_setup, sources
    legacy = tmp_path / "knowledge" / ".mail" / ("b" * 32 + ".json")
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"password": "old-plain"}))
    cred = sources.store_token("glpat-123")
    token_file = sources._cred_dir(cred) / "token"
    token_file.write_text("glpat-legacy")  # as an older version wrote it
    assert org_setup.seal_stored_secrets() == 2
    assert b"old-plain" not in legacy.read_bytes() and b"glpat-legacy" not in token_file.read_bytes()
    assert mail._secret("b" * 32) == {"password": "old-plain"}
    assert org_setup.seal_stored_secrets() == 0


def test_adding_the_env_key_keeps_secrets_written_without_it(box, monkeypatch):
    from api.enterprise import mail, org_setup
    mail._save_secret("c" * 32, {"password": "before"})
    monkeypatch.setenv("LISA_SECRET_KEY", "k" * 64)
    assert mail._secret("c" * 32) == {"password": "before"}
    assert org_setup.seal_stored_secrets() == 1  # re-encrypted with the env key
    box._key_file().unlink()
    assert mail._secret("c" * 32) == {"password": "before"}
    monkeypatch.setenv("LISA_SECRET_KEY", "z" * 64)
    with pytest.raises(RuntimeError, match="cannot be decrypted"):
        mail._secret("c" * 32)


def test_ssh_gets_the_deploy_key_in_plain_only_while_git_runs(box, monkeypatch):
    import shutil
    import subprocess
    from api.enterprise import sources
    if not shutil.which("ssh-keygen"):
        pytest.skip("needs ssh-keygen")
    made = sources.new_deploy_key()
    stored = sources._cred_dir(made["credential_id"]) / "deploy_key"
    assert b"PRIVATE KEY" not in stored.read_bytes()
    seen = {}
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[0] != "git":
            return real_run(cmd, **kw)
        key = kw["env"]["GIT_SSH_COMMAND"].split()[2]
        seen["key"], seen["text"] = key, open(key).read()
        return subprocess.CompletedProcess(cmd, 0, "ok", "")
    monkeypatch.setattr(sources.subprocess, "run", fake_run)
    sources._git(["ls-remote", "git@example.com:x/y.git"], credential_id=made["credential_id"])
    assert "PRIVATE KEY" in seen["text"]
    import os
    assert not os.path.exists(seen["key"])
