"""Tests for Customer Onboarding Wizard (api.customer_wizard)."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.customer_wizard import (
    CAPABILITY_REGISTRY,
    SERVICE_CONNECTORS,
    apply_wizard_setup,
    evaluate_capabilities,
    get_customer_wizard_status,
    get_service_status,
    is_existing_install_configured,
    load_wizard_state,
    probe_service_connection,
    reset_customer_wizard,
    save_wizard_state,
    save_wizard_step,
    validate_setup,
)


@pytest.fixture
def mock_hermes_env(tmp_path, monkeypatch):
    """Set up an isolated Hermes home for testing."""
    profile_home = tmp_path / "test_profile"
    profile_home.mkdir(parents=True)
    (profile_home / "webui").mkdir(parents=True)
    (profile_home / "cron").mkdir(parents=True)
    (profile_home / ".env").write_text("HERMES_ENV=1\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setattr("api.customer_wizard.get_active_hermes_home", lambda: profile_home)
    monkeypatch.setattr("api.profiles.get_active_hermes_home", lambda: profile_home)
    monkeypatch.setattr("api.customer_wizard.get_active_profile_name", lambda: "test_profile")
    return profile_home


def test_fresh_setup_vs_existing_bypass(mock_hermes_env, monkeypatch):
    """Verify that fresh installs detect onboarding needed, while configured installs bypass it."""
    # Initially fresh with no config.yaml or provider:
    monkeypatch.delenv("HERMES_WEBUI_SKIP_ONBOARDING", raising=False)
    monkeypatch.setattr("api.customer_wizard.load_settings", lambda: {})
    assert not is_existing_install_configured()

    # Now add config.yaml with provider + model:
    cfg_path = mock_hermes_env / "config.yaml"
    cfg_path.write_text("model:\n  provider: openrouter\n  default: anthropic/claude-sonnet-4.6\n", encoding="utf-8")
    assert is_existing_install_configured()

    # Operator skip env bypass:
    cfg_path.unlink()
    assert not is_existing_install_configured()
    monkeypatch.setenv("HERMES_WEBUI_SKIP_ONBOARDING", "1")
    assert is_existing_install_configured()


def test_save_and_resume_wizard_steps(mock_hermes_env):
    """Verify that saving progress per step works, persists to disk, and resumes."""
    # Step A: Company
    res = save_wizard_step("company", {
        "company_name": "Acme Corp",
        "assistant_name": "Lisa",
        "timezone": "Europe/Vienna",
        "language": "en",
    })
    assert res["ok"]
    assert res["saved_step"] == "company"

    state = load_wizard_state()
    assert state["company_name"] == "Acme Corp"
    assert state["assistant_name"] == "Lisa"
    assert state["current_step"] == 1

    # Step B: Provider
    res = save_wizard_step("provider", {
        "provider": "openrouter",
        "model": "anthropic/claude-sonnet-4.6",
        "api_key": "sk-or-v1-secret-test-key",
    })
    assert res["ok"]
    state = load_wizard_state()
    assert state["provider"] == "openrouter"
    assert state["api_key_draft"] == "sk-or-v1-secret-test-key"
    assert state["current_step"] == 2

    # Step C: Capabilities
    res = save_wizard_step("capabilities", {
        "capabilities": ["company_knowledge", "task_assistant"],
    })
    assert res["ok"]
    state = load_wizard_state()
    assert "task_assistant" in state["capabilities"]

    # Step F: Approvals
    res = save_wizard_step("approvals", {
        "approvals": {
            "calendar_write": "ask",
            "email_send": "ask",
            "clickup_write": "automatic",
        }
    })
    assert res["ok"]
    state = load_wizard_state()
    assert state["approvals"]["calendar_write"] == "ask"
    assert state["approvals"]["clickup_write"] == "automatic"

    # Step G: Briefing
    res = save_wizard_step("briefing", {
        "enabled": True,
        "channel": "telegram",
        "recipient": "12345678",
        "time": "09:00",
        "timezone": "Europe/Vienna",
    })
    assert res["ok"]
    state = load_wizard_state()
    assert state["briefing"]["enabled"] is True
    assert state["briefing"]["recipient"] == "12345678"
    assert state["briefing"]["time"] == "09:00"


def test_secret_redaction_in_status_payload(mock_hermes_env):
    """Verify that secrets are never returned in plaintext in the status payload."""
    # Write a secret API key in .env
    (mock_hermes_env / ".env").write_text("CLICKUP_API_KEY=pk_secret_1234567890\n", encoding="utf-8")
    save_wizard_step("provider", {
        "provider": "openrouter",
        "model": "test-model",
        "api_key": "sk-secret-draft-key",
    })

    status = get_customer_wizard_status()
    raw_json = json.dumps(status)

    # Neither draft nor real API keys should appear in plaintext:
    assert "sk-secret-draft-key" not in raw_json
    assert "pk_secret_1234567890" not in raw_json


def test_capability_registry_evaluation(mock_hermes_env):
    """Verify capability status reflects actual dependencies and connectors."""
    caps = evaluate_capabilities(["company_knowledge", "task_assistant"])
    assert len(caps) == len(CAPABILITY_REGISTRY)

    task_cap = next(c for c in caps if c["id"] == "task_assistant")
    # Initially ClickUp is not configured:
    assert task_cap["status"] in {"requires_configuration", "missing_dependency"}


def test_service_connection_probe_with_mocks(mock_hermes_env):
    """Verify service probe handles success, failures, and timeouts gracefully with mocks."""
    # ClickUp probe success
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"user": {"username": "john_doe"}}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = probe_service_connection("clickup", {"CLICKUP_API_KEY": "test_key"})
        assert res["ok"] is True
        assert "john_doe" in res["message"]

    # Telegram probe failure
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = Exception("Telegram network error")
        res = probe_service_connection("telegram", {"TELEGRAM_BOT_TOKEN": "bot123:token"})
        assert res["ok"] is False
        assert "Telegram network error" in res["detail"]

    # Mattermost unsupported
    m_stat = get_service_status("mattermost")
    assert m_stat["supported"] is False
    assert m_stat["status"] == "unsupported"


def test_idempotent_apply_wizard_setup(mock_hermes_env, monkeypatch):
    """Verify apply updates config.yaml and cron without creating duplicate jobs on reruns."""
    monkeypatch.setattr("api.customer_wizard.reload_config", lambda: None)
    monkeypatch.setattr("api.customer_wizard._reload_dotenv", lambda path: None)

    save_wizard_step("company", {"company_name": "Test Co", "assistant_name": "Lisa"})
    save_wizard_step("provider", {"provider": "openrouter", "model": "test-model", "api_key": "sk-test-key"})
    save_wizard_step("briefing", {
        "enabled": True,
        "channel": "telegram",
        "recipient": "987654321",
        "time": "08:15",
    })

    # Apply first time
    res1 = apply_wizard_setup()
    assert res1["ok"] is True

    # Check config.yaml
    cfg = json.loads(json.dumps(load_wizard_state()))
    jobs_file = mock_hermes_env / "cron" / "jobs.json"
    assert jobs_file.is_file()
    jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
    assert len(jobs) == 1
    assert jobs[0]["name"] == "customer-morning-briefing"
    assert jobs[0]["delivery"]["recipient"] == "987654321"

    # Apply second time (rerun)
    res2 = apply_wizard_setup()
    assert res2["ok"] is True

    # Ensure still exactly 1 job (no duplicate!)
    jobs_after = json.loads(jobs_file.read_text(encoding="utf-8"))
    assert len(jobs_after) == 1


def test_validate_setup_preflight(mock_hermes_env):
    """Verify preflight validation produces detailed checks without throwing."""
    save_wizard_step("company", {"company_name": "Preflight Co"})
    save_wizard_step("provider", {"provider": "openrouter", "model": "test-model"})

    val = validate_setup()
    assert "checks" in val
    assert any(c["check"] == "profile_exists" for c in val["checks"])
    assert any(c["check"] == "provider_connection" for c in val["checks"])


def test_reset_wizard(mock_hermes_env):
    """Verify resetting wizard resets step and completion flag."""
    save_wizard_step("company", {"company_name": "Reset Co"})
    res = reset_customer_wizard()
    assert res["ok"] is True
    state = load_wizard_state()
    assert state["current_step"] == 0


def test_routes_integration_get_and_post(mock_hermes_env, monkeypatch):
    """Verify handle_get and handle_post dispatch customer wizard routes correctly."""
    import io
    from urllib.parse import urlparse
    from api.routes import handle_get, handle_post

    class MockHandler:
        def __init__(self, body_dict=None, client_ip="127.0.0.1"):
            body_bytes = json.dumps(body_dict or {}).encode("utf-8")
            self.headers = {
                "Host": "localhost",
                "Content-Type": "application/json",
                "Content-Length": str(len(body_bytes)),
            }
            self.rfile = io.BytesIO(body_bytes)
            self.client_address = (client_ip, 12345)
            self.response_status = 200
            self.response_headers = {}
            self.response_data = b""

        def send_response(self, code, message=None):
            self.response_status = code

        def send_header(self, key, val):
            self.response_headers[key] = val

        def end_headers(self):
            pass

        @property
        def wfile(self):
            class WFile:
                def __init__(self, outer):
                    self.outer = outer
                def write(self, data):
                    self.outer.response_data += data
                def flush(self):
                    pass
            return WFile(self)

    # 1. GET /api/customer-wizard/status
    h1 = MockHandler()
    handled = handle_get(h1, urlparse("/api/customer-wizard/status"))
    assert handled is not False
    assert h1.response_status == 200
    data = json.loads(h1.response_data.decode("utf-8"))
    assert "capabilities" in data
    assert "company" in data

    # 2. POST /api/customer-wizard/step
    h2 = MockHandler({"step": "company", "data": {"company_name": "Route Test Co"}})
    handled = handle_post(h2, urlparse("/api/customer-wizard/step"))
    assert handled is not False
    assert h2.response_status == 200
    res_data = json.loads(h2.response_data.decode("utf-8"))
    assert res_data.get("ok") is True

    # 3. POST /api/customer-wizard/validate
    h3 = MockHandler({})
    handled = handle_post(h3, urlparse("/api/customer-wizard/validate"))
    assert handled is not False
    assert h3.response_status == 200
    val_data = json.loads(h3.response_data.decode("utf-8"))
    assert "checks" in val_data

    # 4. POST /api/customer-wizard/reset
    h4 = MockHandler({})
    handled = handle_post(h4, urlparse("/api/customer-wizard/reset"))
    assert handled is not False
    assert h4.response_status == 200
    reset_data = json.loads(h4.response_data.decode("utf-8"))
    assert reset_data.get("ok") is True

    # 5. POST /api/customer-wizard/configure-service
    h5 = MockHandler({
        "service_id": "telegram",
        "env_vars": {"TELEGRAM_BOT_TOKEN": "12345:mock_token"},
    })
    with patch("api.customer_wizard.urllib.request.urlopen") as mock_url:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"ok": True, "result": {"username": "test_bot"}}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_url.return_value = mock_resp

        handled = handle_post(h5, urlparse("/api/customer-wizard/configure-service"))
        assert handled is not False
        assert h5.response_status == 200
        conf_data = json.loads(h5.response_data.decode("utf-8"))
        assert conf_data.get("ok") is True
        assert "Verified" in conf_data.get("message", "")

    # 6. POST /api/customer-wizard/scaffold-obsidian
    h6 = MockHandler({"vault_path": str(mock_hermes_env / "test_obsidian")})
    handled = handle_post(h6, urlparse("/api/customer-wizard/scaffold-obsidian"))
    assert handled is not False
    assert h6.response_status == 200
    scaffold_data = json.loads(h6.response_data.decode("utf-8"))
    assert scaffold_data.get("ok") is True
    assert "00-Inbox" in scaffold_data.get("created_dirs", [])

    # 7. GET /api/customer-wizard/team
    h7 = MockHandler()
    handled = handle_get(h7, urlparse("/api/customer-wizard/team"))
    assert handled is not False
    assert h7.response_status == 200
    team_get_data = json.loads(h7.response_data.decode("utf-8"))
    assert "people" in team_get_data

    # 8. POST /api/customer-wizard/team
    h8 = MockHandler({"people": [{"name": "Alex", "role": "PM", "email": "alex@corp.com"}]})
    handled = handle_post(h8, urlparse("/api/customer-wizard/team"))
    assert handled is not False
    assert h8.response_status == 200
    team_post_data = json.loads(h8.response_data.decode("utf-8"))
    assert team_post_data.get("ok") is True

    # 9. POST /api/customer-wizard/google-auth-url
    h9 = MockHandler({})
    handled = handle_post(h9, urlparse("/api/customer-wizard/google-auth-url"))
    assert handled is not False
    assert h9.response_status == 200

    # 10. POST /api/customer-wizard/google-auth-code (empty code returns 400 Bad Request)
    h10 = MockHandler({"code": ""})
    handled = handle_post(h10, urlparse("/api/customer-wizard/google-auth-code"))
    assert handled is not False
    assert h10.response_status == 400
    code_res = json.loads(h10.response_data.decode("utf-8"))
    assert "error" in code_res


def test_configure_service_with_files_and_env(mock_hermes_env):
    """Verify that configure_service securely saves token files and env vars, then probes."""
    from api.customer_wizard import configure_service

    # Configure Google Workspace with Google's default downloaded client_secret_*.json file upload
    google_client_json = json.dumps({
        "installed": {
            "client_id": "mock-test-client-id.apps.googleusercontent.com",
            "project_id": "lisa-matika",
            "client_secret": "mock-test-gocspx-secret",
        }
    })
    long_filename = "client_secret_mock_test_apps.json"
    res_google = configure_service(
        "google_workspace",
        env_vars={},
        files={long_filename: google_client_json},
    )
    # When ONLY client secret is present, user must complete OAuth consent -> status is needs_auth
    assert res_google["ok"] is False
    assert res_google["status"] == "needs_auth"
    assert res_google.get("needs_google_auth") is True
    assert "OAuth Client Configured" in res_google["message"]

    # Check that original file, credentials.json, and google_client_secret.json are all saved
    assert (mock_hermes_env / long_filename).is_file()
    assert (mock_hermes_env / "credentials.json").is_file()
    assert (mock_hermes_env / "google_client_secret.json").is_file()
    assert os.environ.get("GOOGLE_CLIENT_ID") == "mock-test-client-id.apps.googleusercontent.com"
    assert os.environ.get("GOOGLE_CLIENT_SECRET") == "mock-test-gocspx-secret"

    # Now simulate user completing OAuth with google_token.json
    mock_token = json.dumps({"token": "ya29.test-mock-token", "refresh_token": "1//test-mock-refresh"})
    with patch("api.customer_wizard.urllib.request.urlopen") as mock_url:
        mock_userinfo = MagicMock()
        mock_userinfo.status = 200
        mock_userinfo.read.return_value = json.dumps({"email": "admin@matika.at"}).encode("utf-8")
        mock_userinfo.__enter__.return_value = mock_userinfo
        mock_url.return_value = mock_userinfo

        res_token = configure_service(
            "google_workspace",
            env_vars={},
            files={"google_token.json": mock_token},
        )
        assert res_token["ok"] is True
        assert res_token["status"] == "verified"
        assert "admin@matika.at" in res_token["message"]

    # Configure ClickUp with env var and mock probe
    with patch("api.customer_wizard.urllib.request.urlopen") as mock_url:
        mock_user = MagicMock()
        mock_user.status = 200
        mock_user.read.return_value = json.dumps({"user": {"username": "Davood", "email": "davood@example.com"}}).encode("utf-8")
        mock_user.__enter__.return_value = mock_user

        mock_team = MagicMock()
        mock_team.status = 200
        mock_team.read.return_value = json.dumps({"teams": [{"id": "999888", "name": "Matika Workspace"}]}).encode("utf-8")
        mock_team.__enter__.return_value = mock_team

        mock_url.side_effect = [mock_user, mock_team]

        res = configure_service("clickup", env_vars={"CLICKUP_API_KEY": "pk_12345"})
        assert res["ok"] is True
        assert "Matika Workspace" in res["message"]
        # Check team_id was auto-persisted
        assert os.environ.get("CLICKUP_TEAM_ID") == "999888"


def test_knowledge_step_and_obsidian_scaffolding(mock_hermes_env, tmp_path):
    """Verify that knowledge step saves SOUL, USER, MEMORY and scaffolds Obsidian vault."""
    from api.customer_wizard import scaffold_obsidian_vault, save_wizard_step, get_customer_wizard_status

    vault_dir = tmp_path / "test_obsidian_vault"
    res = scaffold_obsidian_vault(str(vault_dir))
    assert res["ok"] is True
    # Verify canonical enterprise folders
    assert (vault_dir / "00-Inbox" / "README.md").is_file()
    assert (vault_dir / "01-People" / "README.md").is_file()
    assert (vault_dir / "02-Companies" / "README.md").is_file()
    assert (vault_dir / "03-Projects" / "README.md").is_file()
    assert (vault_dir / "04-Meetings" / "README.md").is_file()
    assert (vault_dir / "05-Decisions" / "README.md").is_file()
    assert (vault_dir / "06-Knowledge" / "README.md").is_file()
    assert (vault_dir / "07-Daily" / "README.md").is_file()
    assert (vault_dir / "08-Team-Briefing" / "README.md").is_file()
    assert (vault_dir / "files" / "README.md").is_file()
    assert (vault_dir / "Company_Handbook.md").is_file()

    # Save knowledge step with immediate file persistence
    soul_content = "You are Lisa, an executive assistant."
    user_content = "# Team Roster\n- John Doe (CEO)"
    mem_content = "# Important Facts\n- Company founded 2024"
    save_res = save_wizard_step("knowledge", {
        "soul": soul_content,
        "user": user_content,
        "memory": mem_content,
        "obsidian_vault_path": str(vault_dir),
        "obsidian_team_link": "https://github.com/company/obsidian-vault",
        "save_files_now": True,
    })
    assert save_res["ok"] is True
    assert (mock_hermes_env / "SOUL.md").read_text(encoding="utf-8").strip() == soul_content
    assert (mock_hermes_env / "memories" / "USER.md").read_text(encoding="utf-8").strip() == user_content
    assert (mock_hermes_env / "memories" / "MEMORY.md").read_text(encoding="utf-8").strip() == mem_content

    # Status returns knowledge info
    stat = get_customer_wizard_status()
    assert "knowledge" in stat
    assert stat["knowledge"]["soul"] == soul_content
    assert stat["knowledge"]["obsidian_vault_path"] == str(vault_dir)
    assert stat["knowledge"]["vault_exists"] is True


def test_team_identity_mapping_and_obsidian_notes(mock_hermes_env, tmp_path):
    """Verify team identity mapping stores identity-mapping.json, syncs USER.md, and creates Obsidian notes."""
    from api.customer_wizard import (
        get_identity_mapping,
        save_identity_mapping,
        scaffold_obsidian_vault,
        apply_wizard_setup,
    )

    vault_dir = tmp_path / "obsidian_team_test"
    scaffold_obsidian_vault(str(vault_dir))

    # Save wizard state with vault path
    save_wizard_step("knowledge", {"obsidian_vault_path": str(vault_dir)})

    sample_team = {
        "people": [
            {
                "name": "Davood",
                "role": "CEO",
                "email": "davood@matika.at",
                "clickup": {"user_id": "12345678"},
                "telegram": {"user_id": "892695430", "username": "davood_thinker"},
            },
            {
                "name": "Sarah",
                "role": "Operations Lead",
                "email": "sarah@matika.at",
                "clickup": {"user_id": "87654321"},
                "telegram": {"user_id": "11223344", "username": "sarah_ops"},
            },
        ],
        "aliases": {
            "davood": "Davood",
            "boss": "Davood",
            "sarah": "Sarah",
        },
    }

    res = save_identity_mapping(sample_team)
    assert res["ok"] is True
    assert res["count"] == 2

    # Check identity-mapping.json file
    mapping_file = mock_hermes_env / "config" / "identity-mapping.json"
    assert mapping_file.is_file()
    saved_data = json.loads(mapping_file.read_text(encoding="utf-8"))
    assert len(saved_data["people"]) == 2
    assert saved_data["people"][0]["name"] == "Davood"
    assert saved_data["people"][0]["canonical_name"] == "Davood"
    assert "davood@matika.at" in saved_data["people"][0]["emails"]
    assert str(saved_data["people"][0]["clickup"]["user_id"]) == "12345678"
    assert saved_data["people"][0]["telegram"]["user_id"] == "892695430"

    # Check USER.md sync
    user_md = mock_hermes_env / "memories" / "USER.md"
    assert user_md.is_file()
    user_content = user_md.read_text(encoding="utf-8")
    assert "Davood" in user_content
    assert "CEO" in user_content
    assert "12345678" in user_content
    assert "892695430" in user_content

    # Check Obsidian 01-People notes
    davood_note = vault_dir / "01-People" / "Davood.md"
    assert davood_note.is_file()
    davood_text = davood_note.read_text(encoding="utf-8")
    assert "name: Davood" in davood_text
    assert "12345678" in davood_text
    assert "892695430" in davood_text

    sarah_note = vault_dir / "01-People" / "Sarah.md"
    assert sarah_note.is_file()

    # Check Company Handbook has links
    handbook = (vault_dir / "Company_Handbook.md").read_text(encoding="utf-8")
    assert "[[01-People/Davood|Davood]]" in handbook
    assert "[[01-People/Sarah|Sarah]]" in handbook

    # Check get_identity_mapping
    retrieved = get_identity_mapping()
    assert len(retrieved["people"]) == 2


def test_google_oauth_url_and_code_exchange(mock_hermes_env):
    """Verify Google OAuth URL generation and code exchange helpers."""
    from api.customer_wizard import (
        generate_google_auth_url,
        exchange_google_auth_code,
    )

    # 1. URL generation without client secret
    fail_url = generate_google_auth_url()
    assert fail_url["ok"] is False

    # 2. Save client secret
    google_creds = {
        "installed": {
            "client_id": "test-client-id.apps.googleusercontent.com",
            "project_id": "test-project",
            "client_secret": "test-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    (mock_hermes_env / "credentials.json").write_text(json.dumps(google_creds), encoding="utf-8")

    url_res = generate_google_auth_url()
    assert url_res["ok"] is True
    assert "auth_url" in url_res
    assert "test-client-id.apps.googleusercontent.com" in url_res["auth_url"]
    assert "accounts.google.com" in url_res["auth_url"]

    # 3. Exchange code mock
    with patch("api.customer_wizard.urllib.request.urlopen") as mock_url:
        mock_token_resp = MagicMock()
        mock_token_resp.status = 200
        mock_token_resp.read.return_value = json.dumps({
            "access_token": "ya29.mock-access",
            "refresh_token": "1//mock-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_token_resp.__enter__.return_value = mock_token_resp

        mock_info_resp = MagicMock()
        mock_info_resp.status = 200
        mock_info_resp.read.return_value = json.dumps({
            "email": "verified.user@matika.at"
        }).encode("utf-8")
        mock_info_resp.__enter__.return_value = mock_info_resp

        mock_url.side_effect = [mock_token_resp, mock_info_resp]

        exchange_res = exchange_google_auth_code("4/0Aean-mock-code")
        assert exchange_res["ok"] is True
        assert exchange_res["status"] == "verified"
        assert "verified.user@matika.at" in exchange_res["message"]

        # google_token.json was created
        token_file = mock_hermes_env / "google_token.json"
        assert token_file.is_file()
        token_data = json.loads(token_file.read_text(encoding="utf-8"))
        assert token_data["token"] == "ya29.mock-access"
        assert token_data["refresh_token"] == "1//mock-refresh"


def test_canonical_multi_email_identity_mapping(mock_hermes_env, tmp_path):
    """Verify that identity mapping natively supports canonical_name, multiple emails, and integer ClickUp IDs."""
    from api.customer_wizard import save_identity_mapping, get_identity_mapping, scaffold_obsidian_vault

    vault_dir = tmp_path / "obsidian_canonical_test"
    scaffold_obsidian_vault(str(vault_dir))

    # Exact payload structure from customer production
    customer_record = {
        "person_id": "8335bca1-fb1d-4bb1-909a-a46382a039af",
        "canonical_name": "Davood Fouladi",
        "aliases": [
            "Davood",
            "david",
            "Davood Fouladi",
        ],
        "emails": [
            "davood@matika.at",
            "davood.thinker@gmail.com",
            "davoodf1995@gmail.com",
        ],
        "telegram": {
            "user_id": "59597486",
            "display_name": "Davood Fouladi",
        },
        "clickup": {
            "member_name": "davood",
            "user_id": 57259309,
        },
    }

    res = save_identity_mapping({"people": [customer_record]})
    assert res["ok"] is True
    assert res["count"] == 1

    mapping_file = mock_hermes_env / "config" / "identity-mapping.json"
    assert mapping_file.is_file()
    saved = json.loads(mapping_file.read_text(encoding="utf-8"))

    person = saved["people"][0]
    assert person["person_id"] == "8335bca1-fb1d-4bb1-909a-a46382a039af"
    assert person["canonical_name"] == "Davood Fouladi"
    assert len(person["emails"]) == 3
    assert "davood.thinker@gmail.com" in person["emails"]
    assert person["telegram"]["user_id"] == "59597486"
    assert person["telegram"]["display_name"] == "Davood Fouladi"
    assert person["clickup"]["member_name"] == "davood"
    assert person["clickup"]["user_id"] == 57259309

    # Check aliases map covers all aliases and emails
    aliases_map = saved["aliases"]
    assert aliases_map["davood"] == "8335bca1-fb1d-4bb1-909a-a46382a039af"
    assert aliases_map["david"] == "8335bca1-fb1d-4bb1-909a-a46382a039af"
    assert aliases_map["davood@matika.at"] == "8335bca1-fb1d-4bb1-909a-a46382a039af"
    assert aliases_map["davood.thinker@gmail.com"] == "8335bca1-fb1d-4bb1-909a-a46382a039af"

    # Check USER.md table contains all emails and IDs
    user_md = (mock_hermes_env / "memories" / "USER.md").read_text(encoding="utf-8")
    assert "Davood Fouladi" in user_md
    assert "davood@matika.at" in user_md
    assert "59597486" in user_md
    assert "57259309" in user_md


def test_deepseek_and_active_model_resolution(mock_hermes_env):
    """Verify that deepseek/deepseek-v4-flash-0731 and active models from config.yaml are in supported_providers."""
    # Write active model into config.yaml
    cfg_file = mock_hermes_env / "config.yaml"
    cfg_file.write_text("model:\n  provider: openrouter\n  default: deepseek/deepseek-v4-flash-0731\n", encoding="utf-8")

    status = get_customer_wizard_status()
    assert status["provider"]["provider"] == "openrouter"
    assert status["provider"]["model"] == "deepseek/deepseek-v4-flash-0731"

    openrouter_meta = next(p for p in status["provider"]["supported_providers"] if p["id"] == "openrouter")
    model_ids = [m["id"] for m in openrouter_meta["models"]]
    assert "deepseek/deepseek-v4-flash-0731" in model_ids
    assert "deepseek/deepseek-v4-flash" in model_ids
    assert "deepseek/deepseek-v4.1-flash" in model_ids
    assert "deepseek/deepseek-v4-pro" in model_ids



