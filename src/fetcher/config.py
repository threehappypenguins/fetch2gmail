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


def password_env_for_account_id(account_id: str) -> str:
    """Env var name used to store this account's IMAP password."""
    acct_id = (account_id or "").strip()
    if acct_id == DEFAULT_IMAP_ACCOUNT_ID:
        return "IMAP_PASSWORD"
    return password_env_key_for_account_id(acct_id)


def validate_unique_password_env_keys(account_ids: list[str]) -> None:
    """
    Raise ValueError if distinct account ids derive the same credential key.

    IDs that differ only by punctuation or letter case (e.g. my-isp / my_isp / MY_ISP)
    all map to IMAP_PASSWORD_MY_ISP and must not be saved together.
    """
    seen: dict[str, str] = {}
    for raw in account_ids:
        acct_id = (raw or "").strip()
        if not acct_id:
            continue
        key = password_env_for_account_id(acct_id)
        prev = seen.get(key)
        if prev is not None and prev != acct_id:
            raise ValueError(
                f"IMAP account ids {prev!r} and {acct_id!r} both map to credential key {key}; "
                "choose ids that differ by more than punctuation or letter case"
            )
        seen[key] = acct_id


def _resolve_imap_password(imap: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    """Return imap dict with password filled from env / encrypted .env when needed."""
    if imap.get("password") not in (None, ""):
        return imap
    key = imap.get("password_env")
    if not key:
        return imap
    acct_id = str(imap.get("id") or DEFAULT_IMAP_ACCOUNT_ID)
    enc_val = os.environ.get(f"{key}_ENC")
    if enc_val:
        from .env_file import decrypt_env_value

        try:
            return {**imap, "password": decrypt_env_value(config_dir, enc_val)}
        except Exception as e:
            raise ValueError(
                f"IMAP password for account {acct_id!r} could not be decrypted ({key}_ENC): {e}"
            ) from e
    plain_val = os.environ.get(key)
    if not plain_val:
        raise ValueError(
            f"IMAP password for account {acct_id!r} is not set "
            f"(environment variable {key} or {key}_ENC required)"
        )
    return {**imap, "password": plain_val}


def _store_resolved_imap_account(cfg: dict[str, Any], resolved: dict[str, Any]) -> None:
    """Write one password-resolved IMAP account back into cfg."""
    acct_id = str(resolved.get("id") or DEFAULT_IMAP_ACCOUNT_ID)
    if isinstance(cfg.get("imap_accounts"), list) and len(cfg["imap_accounts"]) > 0:
        cfg["imap_accounts"] = [
            resolved if str(a.get("id")) == acct_id else a for a in cfg["imap_accounts"]
        ]
        # Keep legacy `imap` in sync when it is the first / matching account.
        first_id = str(cfg["imap_accounts"][0].get("id") or DEFAULT_IMAP_ACCOUNT_ID)
        if "imap" in cfg and acct_id == first_id:
            cfg["imap"] = {
                k: v for k, v in resolved.items() if k not in ("id", "gmail_token_paths")
            }
        return
    # Legacy single-imap config
    cfg["imap"] = {
        k: v for k, v in resolved.items() if k not in ("id", "gmail_token_paths")
    }


def load_config(
    path: str | Path,
    resolve_password: bool = True,
    *,
    account_id: str | None = None,
) -> dict[str, Any]:
    """
    Load config from JSON file.

    If resolve_password is True, resolve credentials only for the selected IMAP
    account (account_id), or the first/sole account when account_id is None.
    Other accounts are left unresolved so a missing/undecryptable password on one
    account cannot block watchers or fetches for healthy accounts.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    config_dir = path.parent
    load_dotenv(config_dir / ".env")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not resolve_password:
        return cfg

    try:
        target = get_imap_account(cfg, account_id)
    except ValueError:
        return cfg
    resolved = _resolve_imap_password(dict(target), config_dir)
    _store_resolved_imap_account(cfg, resolved)
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


def prune_stale_gmail_routes(
    imap_accounts: list[dict[str, Any]],
    valid_token_names: set[str],
) -> None:
    """
    Remove gmail_token_paths entries that are no longer in gmail_accounts.

    Mutates imap_accounts in place. An omitted or already-empty route (meaning
    "all destinations") is left alone. If an *explicit* non-empty route would
    become empty after pruning, raise ValueError — empty means "all remaining
    Gmail accounts", which would silently broaden the user's selection.
    """
    updates: list[tuple[dict[str, Any], list[Any]]] = []
    for acct in imap_accounts:
        paths = acct.get("gmail_token_paths")
        if not isinstance(paths, list) or len(paths) == 0:
            continue
        pruned = [
            p
            for p in paths
            if str(p).strip()
            and (
                _normalize_token_path(p) in valid_token_names
                or str(p).strip() in valid_token_names
            )
        ]
        if not pruned:
            raise ValueError(
                f"IMAP account {acct.get('id')!r} is explicitly routed only to "
                f"Gmail destination(s) being removed. Update that account's "
                f"Gmail destinations before removing them."
            )
        updates.append((acct, pruned))
    for acct, pruned in updates:
        acct["gmail_token_paths"] = pruned
