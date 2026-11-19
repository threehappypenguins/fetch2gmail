"""Configuration loading from JSON and environment variables."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env from current dir and common locations
load_dotenv()
for p in (Path.cwd(), Path.home(), Path(__file__).resolve().parents[2]):
    load_dotenv(p / ".env")

DEFAULT_IMAP_ACCOUNT_ID = "default"


def password_env_key_for_account_id(account_id: str) -> str:
    """Derive IMAP_PASSWORD_<ID> from account id (uppercase; non-alnum → _)."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", (account_id or "").strip()).strip("_")
    if not cleaned:
        cleaned = "DEFAULT"
    return f"IMAP_PASSWORD_{cleaned.upper()}"


def _resolve_imap_password(imap: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Return imap dict with password filled from env / encrypted .env when needed."""
    if imap.get("password") not in (None, ""):
        return imap
    key = imap.get("password_env")
    if not key:
        return imap
    enc_val = os.environ.get(f"{key}_ENC")
    if enc_val:
        from .env_file import decrypt_env_value

        return {**imap, "password": decrypt_env_value(config_dir, enc_val)}
    plain_val = os.environ.get(key)
    if not plain_val:
        raise ValueError(
            f"Environment variable {key} or {key}_ENC is not set (required for IMAP password)"
        )
    return {**imap, "password": plain_val}


def load_config(path: str | Path, resolve_password: bool = True) -> dict[str, Any]:
    """Load config from JSON file. If resolve_password is True, resolve IMAP passwords from env."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    config_dir = path.parent
    load_dotenv(config_dir / ".env")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not resolve_password:
        return cfg

    # Resolve passwords for all normalized IMAP accounts and write back into cfg shape.
    accounts = get_imap_accounts(cfg)
    resolved: list[dict[str, Any]] = []
    for acct in accounts:
        resolved.append(_resolve_imap_password(dict(acct), config_dir))

    if isinstance(cfg.get("imap_accounts"), list) and len(cfg["imap_accounts"]) > 0:
        cfg["imap_accounts"] = resolved
        # Keep legacy `imap` in sync with the first account when present.
        if "imap" in cfg and resolved:
            legacy = {k: v for k, v in resolved[0].items() if k not in ("id", "gmail_token_paths")}
            cfg["imap"] = legacy
    elif resolved:
        # Legacy single-imap config
        legacy = {k: v for k, v in resolved[0].items() if k not in ("id", "gmail_token_paths")}
        cfg["imap"] = legacy
    return cfg


def get_config_path() -> Path:
    """Default config path: FETCH2GMAIL_CONFIG or ./config.json."""
    env = os.environ.get("FETCH2GMAIL_CONFIG")
    if env:
        return Path(env)
    return Path.cwd() / "config.json"


def get_gmail_accounts(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return list of Gmail account configs for import.
    Supports multi-account: use "gmail_accounts" (array) or legacy "gmail" (single object).
    If both present, "gmail_accounts" wins. Each entry has credentials_path, token_path, label, use_label.
    """
    accounts = cfg.get("gmail_accounts")
    if isinstance(accounts, list) and len(accounts) > 0:
        return accounts
    gmail = cfg.get("gmail")
    if gmail and isinstance(gmail, dict):
        return [gmail]
    return []


def get_imap_accounts(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return list of IMAP account configs.

    Prefer "imap_accounts" (array). Fall back to legacy single "imap" object wrapped as
    one account with id "default". Requires unique non-empty ids when using the array.
    """
    accounts = cfg.get("imap_accounts")
    if isinstance(accounts, list) and len(accounts) > 0:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, raw in enumerate(accounts):
            if not isinstance(raw, dict):
                raise ValueError(f"imap_accounts[{i}] must be an object")
            acct_id = str(raw.get("id") or "").strip()
            if not acct_id:
                raise ValueError(f"imap_accounts[{i}] is missing a non-empty 'id'")
            if acct_id in seen:
                raise ValueError(f"Duplicate IMAP account id: {acct_id!r}")
            seen.add(acct_id)
            out.append(dict(raw))
        return out

    imap = cfg.get("imap")
    if imap and isinstance(imap, dict):
        wrapped = dict(imap)
        wrapped.setdefault("id", DEFAULT_IMAP_ACCOUNT_ID)
        return [wrapped]
    return []


def get_imap_account(cfg: dict[str, Any], account_id: str | None = None) -> dict[str, Any]:
    """Return one IMAP account by id, or the sole/first account when account_id is None."""
    accounts = get_imap_accounts(cfg)
    if not accounts:
        raise ValueError("No IMAP account configured (add 'imap' or 'imap_accounts' in config)")
    if account_id is None:
        return accounts[0]
    for acct in accounts:
        if str(acct.get("id")) == account_id:
            return acct
    raise ValueError(f"Unknown IMAP account id: {account_id!r}")


def _normalize_token_path(token_path: str | Path) -> str:
    """Normalize token path for comparison (use basename when relative-looking)."""
    p = Path(str(token_path).strip())
    return p.name if not p.is_absolute() else str(p)


def get_gmail_destinations_for_imap(
    cfg: dict[str, Any],
    imap_account: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Resolve Gmail destinations for one IMAP account.

    If gmail_token_paths is omitted or empty → all gmail_accounts.
    Otherwise → only matching token_path entries. Unknown paths raise ValueError.
    """
    all_gmail = get_gmail_accounts(cfg)
    raw_paths = imap_account.get("gmail_token_paths")
    if raw_paths is None or (isinstance(raw_paths, list) and len(raw_paths) == 0):
        return list(all_gmail)
    if not isinstance(raw_paths, list):
        raise ValueError("gmail_token_paths must be a list of token_path strings")

    by_name: dict[str, dict[str, Any]] = {}
    by_full: dict[str, dict[str, Any]] = {}
    for acct in all_gmail:
        tp = str(acct.get("token_path", "token.json")).strip()
        by_full[tp] = acct
        by_name[_normalize_token_path(tp)] = acct

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_paths:
        key = str(raw).strip()
        if not key:
            continue
        match = by_full.get(key) or by_name.get(_normalize_token_path(key))
        if match is None:
            raise ValueError(
                f"Unknown gmail_token_path {key!r} for IMAP account "
                f"{imap_account.get('id')!r}; not found in gmail_accounts"
            )
        marker = str(match.get("token_path", key))
        if marker in seen:
            continue
        seen.add(marker)
        selected.append(match)
    if not selected:
        # Explicit empty after filtering blanks → treat as all (same as omit).
        return list(all_gmail)
    return selected
