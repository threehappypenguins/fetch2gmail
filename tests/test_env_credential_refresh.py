"""Saving credentials must refresh process env so watchers see the new password."""

from __future__ import annotations

import os
from pathlib import Path

from fetcher.env_file import decrypt_env_value, refresh_credential_environ, set_encrypted_env


def test_set_encrypted_env_overrides_stale_enc_in_process_environ(tmp_path, monkeypatch):
    config_dir = Path(tmp_path)
    key = "IMAP_PASSWORD_ISP1"
    enc_key = f"{key}_ENC"

    # Simulate a previously loaded ciphertext still sitting in os.environ.
    monkeypatch.setenv(enc_key, "stale-ciphertext-not-valid")
    monkeypatch.setenv(key, "stale-plain")

    set_encrypted_env(config_dir, key, "fresh-secret")

    assert os.environ.get(key) is None
    assert os.environ.get(enc_key) not in (None, "stale-ciphertext-not-valid")
    assert decrypt_env_value(config_dir, os.environ[enc_key]) == "fresh-secret"
    # .env file matches process env (no leftover plaintext line)
    lines = (config_dir / ".env").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith(f"{enc_key}=") for line in lines)
    assert not any(line.startswith(f"{key}=") for line in lines)


def test_refresh_credential_environ_only_touches_named_key(monkeypatch):
    monkeypatch.setenv("IMAP_PASSWORD_A_ENC", "keep-me")
    monkeypatch.setenv("IMAP_PASSWORD_B_ENC", "old-b")
    monkeypatch.setenv("IMAP_PASSWORD_B", "plain-b")

    refresh_credential_environ("IMAP_PASSWORD_B", "new-b-enc")

    assert os.environ["IMAP_PASSWORD_A_ENC"] == "keep-me"
    assert os.environ["IMAP_PASSWORD_B_ENC"] == "new-b-enc"
    assert "IMAP_PASSWORD_B" not in os.environ
