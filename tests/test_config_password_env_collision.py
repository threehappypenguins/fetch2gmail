"""Config save credential-key validation and blank-password preservation."""

from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from fetcher.web_ui import ConfigUpdate, ImapAccountUpdate, api_config_update


def _acct(acct_id: str, password: str | None = "secret", **kwargs) -> ImapAccountUpdate:
    return ImapAccountUpdate(
        id=acct_id,
        host=kwargs.get("host", "imap.example.com"),
        port=kwargs.get("port", 993),
        username=kwargs.get("username", "user"),
        password=password,
        mailbox=kwargs.get("mailbox", "INBOX"),
    )


def _config_api_stack(cfg_path, tmp_path, capture_write=None):
    stack = ExitStack()
    stack.enter_context(patch("fetcher.web_ui._require_auth", return_value=True))
    stack.enter_context(patch("fetcher.web_ui._get_config_path", return_value=cfg_path))
    stack.enter_context(patch("fetcher.web_ui._config_dir", return_value=tmp_path))
    stack.enter_context(
        patch(
            "fetcher.web_ui.set_encrypted_env",
            side_effect=capture_write or (lambda *_a, **_k: None),
        )
    )
    stack.enter_context(patch("fetcher.web_ui._verify_imap_credentials"))
    stack.enter_context(patch("fetcher.web_ui._sync_watcher_threads"))
    return stack


def test_api_config_update_rejects_derived_key_collision_before_password_writes(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        '{"imap_accounts":[{"id":"a","host":"h","username":"u","password_env":"IMAP_PASSWORD_A"}],'
        '"gmail_accounts":[{"token_path":"token.json"}],'
        '"poll":{"interval_minutes":5},"state":{"db_path":"state.db"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("FETCH2GMAIL_CONFIG", str(cfg_path))

    update = ConfigUpdate(imap_accounts=[_acct("my-isp"), _acct("my_isp")])
    writes: list[tuple[str, str]] = []

    with _config_api_stack(cfg_path, tmp_path, lambda _d, k, v: writes.append((k, v))):
        with pytest.raises(HTTPException) as exc:
            api_config_update(MagicMock(), update)

    assert exc.value.status_code == 400
    assert "credential key" in str(exc.value.detail)
    assert writes == []


def test_blank_password_preserves_custom_password_env_and_inline_password(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg = {
        "imap_accounts": [
            {
                "id": "legacy",
                "host": "old.example.com",
                "port": 993,
                "username": "olduser",
                "password": "inline-secret",
                "password_env": "ISP_SECRET",
                "mailbox": "INBOX",
            }
        ],
        "gmail_accounts": [{"token_path": "token.json"}],
        "poll": {"interval_minutes": 5},
        "state": {"db_path": "state.db"},
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FETCH2GMAIL_CONFIG", str(cfg_path))

    update = ConfigUpdate(
        imap_accounts=[
            _acct("legacy", password=None, host="new.example.com", username="olduser")
        ]
    )
    writes: list[tuple[str, str]] = []

    with _config_api_stack(cfg_path, tmp_path, lambda _d, k, v: writes.append((k, v))):
        result = api_config_update(MagicMock(), update)

    assert result == {"status": "ok"}
    assert writes == []
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    acct = saved["imap_accounts"][0]
    assert acct["host"] == "new.example.com"
    assert acct["password_env"] == "ISP_SECRET"
    assert acct["password"] == "inline-secret"


def test_replacement_password_keeps_custom_password_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg = {
        "imap_accounts": [
            {
                "id": "legacy",
                "host": "imap.example.com",
                "port": 993,
                "username": "user",
                "password_env": "ISP_SECRET",
                "mailbox": "INBOX",
            }
        ],
        "gmail_accounts": [{"token_path": "token.json"}],
        "poll": {"interval_minutes": 5},
        "state": {"db_path": "state.db"},
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FETCH2GMAIL_CONFIG", str(cfg_path))

    update = ConfigUpdate(imap_accounts=[_acct("legacy", password="new-secret")])
    writes: list[tuple[str, str]] = []

    with _config_api_stack(cfg_path, tmp_path, lambda _d, k, v: writes.append((k, v))):
        api_config_update(MagicMock(), update)

    assert writes == [("ISP_SECRET", "new-secret")]
    saved = json.loads(cfg_path.read_text(encoding="utf-8"))
    acct = saved["imap_accounts"][0]
    assert acct["password_env"] == "ISP_SECRET"
    assert "password" not in acct
