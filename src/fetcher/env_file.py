"""Write IMAP password to .env next to config; stored encrypted, not plain text."""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .auth_ui import get_or_create_cookie_secret


def _fernet_for_config_dir(config_dir: Path):
    """Fernet instance using .cookie_secret in config_dir (32 bytes -> base64url key)."""
    secret = get_or_create_cookie_secret(config_dir)
    key = base64.urlsafe_b64encode(secret).decode()
    return Fernet(key.encode())


def refresh_credential_environ(key: str, encrypted_value: str) -> None:
    """
    Update process env for one credential key after a successful .env write.

    load_dotenv(override=False) will not replace an existing <key>_ENC, so watchers
    would keep decrypting the old ciphertext until restart. Only the changed key
    is refreshed here.
    """
    enc_key = f"{key}_ENC"
    os.environ[enc_key] = encrypted_value
    # Drop stale plaintext so resolution prefers the new ENC value.
    os.environ.pop(key, None)


def set_encrypted_env(config_dir: Path, key: str, value: str) -> str:
    """
    Encrypt value and set key_ENC=... in .env. Removes any existing key= (plain) line.
    Uses .cookie_secret in config_dir so the password is not stored in plain text.

    Also refreshes os.environ for this key so running watchers pick up the new
    credential without a process restart. Returns the ciphertext written to .env.
    """
    fernet = _fernet_for_config_dir(config_dir)
    encrypted = fernet.encrypt(value.encode("utf-8")).decode("ascii")
    enc_key = f"{key}_ENC"
    env_path = config_dir / ".env"
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        # Update or add key_ENC
        enc_pattern = re.compile(rf"^{re.escape(enc_key)}=.*$", re.MULTILINE)
        plain_pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        line = f"{enc_key}={encrypted}\n"
        if enc_pattern.search(content):
            content = enc_pattern.sub(line.strip(), content)
        else:
            content = content.rstrip() + ("\n" if content else "") + line
        # Remove plain key if present
        content = plain_pattern.sub("", content)
        content = re.sub(r"\n{3,}", "\n\n", content).strip() + "\n"
        env_path.write_text(content, encoding="utf-8")
    else:
        env_path.write_text(f"{enc_key}={encrypted}\n", encoding="utf-8")
    refresh_credential_environ(key, encrypted)
    return encrypted


def decrypt_env_value(config_dir: Path, encrypted_b64: str) -> str:
    """Decrypt a value stored with set_encrypted_env."""
    try:
        fernet = _fernet_for_config_dir(config_dir)
        return fernet.decrypt(encrypted_b64.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ValueError(
            "IMAP password could not be decrypted (encryption key may have changed, e.g. different config dir or .cookie_secret). "
            "Re-enter the IMAP password in the UI Config section to fix."
        ) from None
