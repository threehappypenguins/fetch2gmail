"""Tests for multi-IMAP config helpers."""

from __future__ import annotations

import pytest

from fetcher.config import (
    get_gmail_destinations_for_imap,
    get_imap_accounts,
    password_env_for_account_id,
    password_env_key_for_account_id,
    validate_unique_password_env_keys,
)


def test_password_env_key_for_account_id():
    assert password_env_key_for_account_id("isp1") == "IMAP_PASSWORD_ISP1"
    assert password_env_key_for_account_id("my-isp") == "IMAP_PASSWORD_MY_ISP"
    assert password_env_key_for_account_id("  ") == "IMAP_PASSWORD_DEFAULT"
    assert password_env_for_account_id("default") == "IMAP_PASSWORD"
    assert password_env_for_account_id("my-isp") == "IMAP_PASSWORD_MY_ISP"
    assert password_env_for_account_id("my_isp") == "IMAP_PASSWORD_MY_ISP"


def test_validate_unique_password_env_keys_rejects_normalized_collisions():
    validate_unique_password_env_keys(["isp1", "isp2"])  # ok
    with pytest.raises(ValueError, match="my-isp"):
        validate_unique_password_env_keys(["my-isp", "my_isp"])
    with pytest.raises(ValueError, match="credential key"):
        validate_unique_password_env_keys(["WorkMail", "workmail"])


def test_get_imap_accounts_legacy_single():
    cfg = {"imap": {"host": "h", "username": "u", "password_env": "IMAP_PASSWORD"}}
    accounts = get_imap_accounts(cfg)
    assert len(accounts) == 1
    assert accounts[0]["id"] == "default"
    assert accounts[0]["host"] == "h"
    assert accounts[0]["password_env"] == "IMAP_PASSWORD"


def test_get_imap_accounts_array():
    cfg = {
        "imap_accounts": [
            {"id": "a", "host": "h1"},
            {"id": "b", "host": "h2"},
        ]
    }
    accounts = get_imap_accounts(cfg)
    assert [a["id"] for a in accounts] == ["a", "b"]


def test_get_imap_accounts_duplicate_id_rejected():
    cfg = {"imap_accounts": [{"id": "a"}, {"id": "a"}]}
    with pytest.raises(ValueError, match="Duplicate"):
        get_imap_accounts(cfg)


def test_get_imap_accounts_missing_id_rejected():
    cfg = {"imap_accounts": [{"host": "h"}]}
    with pytest.raises(ValueError, match="id"):
        get_imap_accounts(cfg)


def test_gmail_destinations_omit_means_all():
    cfg = {
        "gmail_accounts": [
            {"token_path": "token.json"},
            {"token_path": "token2.json"},
        ]
    }
    imap = {"id": "isp1"}
    dest = get_gmail_destinations_for_imap(cfg, imap)
    assert len(dest) == 2


def test_gmail_destinations_empty_list_means_all():
    cfg = {
        "gmail_accounts": [
            {"token_path": "token.json"},
            {"token_path": "token2.json"},
        ]
    }
    imap = {"id": "isp1", "gmail_token_paths": []}
    dest = get_gmail_destinations_for_imap(cfg, imap)
    assert len(dest) == 2


def test_gmail_destinations_subset():
    cfg = {
        "gmail_accounts": [
            {"token_path": "token.json", "label": "A"},
            {"token_path": "token2.json", "label": "B"},
        ]
    }
    imap = {"id": "isp1", "gmail_token_paths": ["token2.json"]}
    dest = get_gmail_destinations_for_imap(cfg, imap)
    assert len(dest) == 1
    assert dest[0]["token_path"] == "token2.json"


def test_gmail_destinations_unknown_path_raises():
    cfg = {"gmail_accounts": [{"token_path": "token.json"}]}
    imap = {"id": "isp1", "gmail_token_paths": ["missing.json"]}
    with pytest.raises(ValueError, match="Unknown gmail_token_path"):
        get_gmail_destinations_for_imap(cfg, imap)


def test_prune_stale_routes_keeps_remaining():
    from fetcher.config import prune_stale_gmail_routes

    accounts = [
        {"id": "isp1", "gmail_token_paths": ["token.json", "token2.json"]},
        {"id": "isp2", "gmail_token_paths": []},
    ]
    prune_stale_gmail_routes(accounts, {"token.json"})
    assert accounts[0]["gmail_token_paths"] == ["token.json"]
    assert accounts[1]["gmail_token_paths"] == []


def test_prune_stale_routes_rejects_emptying_explicit_route():
    from fetcher.config import prune_stale_gmail_routes

    accounts = [{"id": "isp1", "gmail_token_paths": ["token2.json"]}]
    with pytest.raises(ValueError, match="explicitly routed"):
        prune_stale_gmail_routes(accounts, {"token.json"})
    # Unchanged on failure
    assert accounts[0]["gmail_token_paths"] == ["token2.json"]


def test_load_config_resolves_only_selected_account(tmp_path, monkeypatch):
    from fetcher.config import load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        """
        {
          "imap_accounts": [
            {
              "id": "good",
              "host": "h1",
              "username": "u1",
              "password_env": "IMAP_PASSWORD_GOOD",
              "mailbox": "INBOX"
            },
            {
              "id": "bad",
              "host": "h2",
              "username": "u2",
              "password_env": "IMAP_PASSWORD_BAD",
              "mailbox": "INBOX"
            }
          ],
          "gmail_accounts": [{"token_path": "token.json"}]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("IMAP_PASSWORD_GOOD", "secret-good")
    monkeypatch.delenv("IMAP_PASSWORD_BAD", raising=False)
    monkeypatch.delenv("IMAP_PASSWORD_BAD_ENC", raising=False)

    cfg = load_config(cfg_path, resolve_password=True, account_id="good")
    accounts = {a["id"]: a for a in cfg["imap_accounts"]}
    assert accounts["good"].get("password") == "secret-good"
    assert "password" not in accounts["bad"] or not accounts["bad"].get("password")

    with pytest.raises(ValueError, match="bad"):
        load_config(cfg_path, resolve_password=True, account_id="bad")


def test_run_all_accounts_aggregates_credential_errors(tmp_path, monkeypatch):
    from fetcher.run import run_all_accounts

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        """
        {
          "imap_accounts": [
            {
              "id": "good",
              "host": "h1",
              "username": "u1",
              "password_env": "IMAP_PASSWORD_GOOD",
              "mailbox": "INBOX"
            },
            {
              "id": "bad",
              "host": "h2",
              "username": "u2",
              "password_env": "IMAP_PASSWORD_BAD",
              "mailbox": "INBOX"
            }
          ],
          "gmail_accounts": [{"token_path": "token.json"}],
          "state": {"db_path": "state.db"}
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("IMAP_PASSWORD_GOOD", "secret-good")
    monkeypatch.delenv("IMAP_PASSWORD_BAD", raising=False)
    monkeypatch.delenv("IMAP_PASSWORD_BAD_ENC", raising=False)

    # Avoid real IMAP/Gmail for the healthy account; assert credential isolation.
    def fake_run_once(*, config_path=None, dry_run=False, account_id=None):
        from fetcher.config import load_config, get_imap_account

        try:
            cfg = load_config(config_path, resolve_password=True, account_id=account_id)
            imap = get_imap_account(cfg, account_id)
            if not imap.get("password"):
                return {
                    "error": f"IMAP password not set for account {account_id!r}",
                    "imported": 0,
                    "skipped_duplicate": 0,
                    "deleted": 0,
                    "account_id": account_id,
                }
            return {
                "error": None,
                "imported": 1,
                "skipped_duplicate": 0,
                "deleted": 0,
                "account_id": account_id,
                "last_fetch_time": "2026-01-01T00:00:00",
            }
        except ValueError as e:
            return {
                "error": str(e),
                "imported": 0,
                "skipped_duplicate": 0,
                "deleted": 0,
                "account_id": account_id,
            }

    monkeypatch.setattr("fetcher.run.run_once", fake_run_once)
    result = run_all_accounts(config_path=str(cfg_path))
    assert result["imported"] == 1
    assert result["error"] is not None
    assert "bad:" in result["error"]
    assert "good:" not in result["error"]
    assert len(result["accounts"]) == 2
