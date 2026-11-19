"""Tests for multi-IMAP config helpers."""

from __future__ import annotations

import pytest

from fetcher.config import (
    get_gmail_destinations_for_imap,
    get_imap_accounts,
    password_env_key_for_account_id,
)


def test_password_env_key_for_account_id():
    assert password_env_key_for_account_id("isp1") == "IMAP_PASSWORD_ISP1"
    assert password_env_key_for_account_id("my-isp") == "IMAP_PASSWORD_MY_ISP"
    assert password_env_key_for_account_id("  ") == "IMAP_PASSWORD_DEFAULT"


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
