"""
Main fetch run: IMAP -> Gmail import -> state update -> delete from ISP.

Idempotency: UID + per-destination checks via RFC822 Message-ID and/or persisted
(source hash, destination) delivery records. A source hash alone never proves
delivery to every current route — newly added destinations still receive mail.
Dry-run skips Gmail and delete. Supports multiple Gmail accounts (per-IMAP routing
via gmail_token_paths).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_IMAP_ACCOUNT_ID,
    _token_path_identity,
    get_gmail_destinations_for_imap,
    get_imap_account,
    get_imap_accounts,
    load_config,
)
from .gmail_client import (
    SkipMessageError,
    _parse_message_id_from_raw,
    get_gmail_service,
    get_inbox_label_id,
    get_unread_label_id,
    gmail_has_message_with_id,
    import_message,
)
from .imap_client import ImapImportSession
from .state import StateStore

logger = logging.getLogger(__name__)

# Gmail API user id
USER_ID = "me"

# (service, label_id, inbox_label_id, unread_label_id, destination_keys)
# destination_keys lists aliases for one mailbox (e.g. gmail:email + oauth:fingerprint).
GmailContext = tuple[Any, str | None, str, str, list[str]]


def _ensure_label(service: Any, label_name: str) -> str:
    """Return label ID for given name; create if missing."""
    labels = service.users().labels().list(userId=USER_ID).execute()
    for lab in labels.get("labels", []):
        if lab.get("name") == label_name:
            return lab["id"]
    # Create label
    body = {"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
    created = service.users().labels().create(userId=USER_ID, body=body).execute()
    return created["id"]


def _oauth_token_fingerprint(token_path: Path) -> str | None:
    """Stable id for one OAuth grant; changes when a different account reuses the file."""
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    secret = str(data.get("refresh_token") or data.get("token") or "").strip()
    if not secret:
        return None
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:32]
    return f"oauth:{digest}"


def gmail_destination_identities(service: Any, token_path: Path) -> list[str]:
    """
    All delivery-history identities for one Gmail mailbox.

    Always includes the OAuth-token fingerprint when available, and the mailbox
    email when getProfile succeeds. Check/record against every alias so a
    transient profile failure (gmail:... ↔ oauth:...) cannot miss a prior
    successful delivery and re-import mail without a Message-ID.
    """
    keys: list[str] = []
    seen: set[str] = set()

    def _add(key: str | None) -> None:
        if not key or key in seen:
            return
        seen.add(key)
        keys.append(key)

    try:
        profile = service.users().getProfile(userId=USER_ID).execute()
        email = str(profile.get("emailAddress") or "").strip().lower()
        if email:
            _add(f"gmail:{email}")
    except Exception:  # noqa: BLE001
        logger.debug("Could not resolve Gmail email for %s", token_path, exc_info=True)

    _add(_oauth_token_fingerprint(token_path))
    if not keys:
        _add(f"path:{_token_path_identity(token_path) or token_path.name}")
    return keys


def gmail_destination_identity(service: Any, token_path: Path) -> str:
    """Preferred (first) identity for logging / invalidation helpers."""
    return gmail_destination_identities(service, token_path)[0]


def destination_keys_for_token_invalidation(
    token_path: str | Path,
    config_dir: Path,
    *,
    credentials_path: str | Path | None = None,
) -> set[str]:
    """
    Keys whose delivery history should be cleared when a token is removed/replaced.

    Includes path-based legacy keys plus best-effort email/oauth identities.
    """
    raw = _token_path_identity(token_path) or "token.json"
    tp = Path(str(token_path))
    if not tp.is_absolute():
        tp = config_dir / tp
    keys: set[str] = {raw, tp.name, f"path:{raw}", f"path:{tp.name}"}
    fingerprint = _oauth_token_fingerprint(tp)
    if fingerprint:
        keys.add(fingerprint)
    try:
        cred = Path(credentials_path or "credentials.json")
        if not cred.is_absolute():
            cred = config_dir / cred
        if tp.exists() and cred.exists():
            service = get_gmail_service(cred, tp)
            keys.update(gmail_destination_identities(service, tp))
    except Exception:  # noqa: BLE001
        logger.debug("Could not resolve Gmail identity to invalidate for %s", tp, exc_info=True)
    return {k for k in keys if k}


def _build_gmail_contexts(
    gmail_accounts: list[dict[str, Any]],
    config_dir: Path,
) -> list[GmailContext]:
    """
    Build list of (service, label_id, inbox_label_id, unread_label_id, destination_keys).
    Paths in account dicts are resolved relative to config_dir.
    destination_keys are stable Gmail-account identities (email + oauth aliases).
    """
    contexts: list[GmailContext] = []
    for acct in gmail_accounts:
        cred_path = Path(acct.get("credentials_path", "credentials.json"))
        token_path_raw = acct.get("token_path", "token.json")
        token_path = Path(token_path_raw)
        if not cred_path.is_absolute():
            cred_path = config_dir / cred_path
        if not token_path.is_absolute():
            token_path = config_dir / token_path
        use_label = acct.get("use_label") if "use_label" in acct else bool((acct.get("label") or "").strip())
        label_name = (acct.get("label") or "ISP Mail").strip() if use_label else None
        service = get_gmail_service(cred_path, token_path)
        destination_keys = gmail_destination_identities(service, token_path)
        inbox_label_id = get_inbox_label_id(service, USER_ID)
        unread_label_id = get_unread_label_id(service, USER_ID)
        label_id = _ensure_label(service, label_name) if label_name else None
        contexts.append((service, label_id, inbox_label_id, unread_label_id, destination_keys))
    return contexts


def _empty_result(
    *,
    error: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "error": error,
        "imported": 0,
        "skipped_duplicate": 0,
        "deleted": 0,
    }
    if account_id is not None:
        out["account_id"] = account_id
    return out


@dataclass
class _DeliverResult:
    first_gmail_id: str | None
    all_succeeded: bool
    any_imported: bool
    all_already_had: bool
    skip_entire_message: bool
    message_id_header: str | None


def _deliver_to_gmail_destinations(
    *,
    gmail_contexts: list[GmailContext],
    raw: bytes,
    uid: int,
    account_id: str,
    message_hash: str,
    never_mark_spam: bool,
    mark_unread: bool,
    state: StateStore | None = None,
    log_prefix: str = "",
) -> _DeliverResult:
    """
    Ensure every current Gmail destination has this message.

    A destination is skipped when Gmail already has the RFC822 Message-ID, or when
    state records a prior successful delivery of this source hash under any of
    that destination's identity aliases (gmail:email and/or oauth:fingerprint).
    Aggregate hash history alone never skips a destination that has not yet
    received the message (so new routes still get mail).
    """
    prefix = f"{log_prefix} " if log_prefix else ""
    message_id_header = _parse_message_id_from_raw(raw)
    first_gmail_id: str | None = None
    all_succeeded = True
    any_imported = False
    all_already_had = True
    skip_entire_message = False

    for idx, (service, label_id, inbox_label_id, unread_label_id, destination_keys) in enumerate(
        gmail_contexts
    ):
        try:
            dest_keys = list(destination_keys) if destination_keys else []
            dest_label = dest_keys[0] if dest_keys else f"account-{idx + 1}"
            already_here = False
            if message_id_header:
                already_here = gmail_has_message_with_id(
                    service, USER_ID, message_id_header
                )
            if (
                not already_here
                and state is not None
                and message_hash
                and state.has_delivered_hash_to_any(
                    message_hash, dest_keys, account_id=account_id
                )
            ):
                already_here = True
                logger.debug(
                    "%suid=%s already delivered to %s (hash+dest) [%s]",
                    prefix,
                    uid,
                    dest_label,
                    account_id,
                )
            if already_here:
                if state is not None and message_hash:
                    # Backfill every current alias so profile recovery stays linked.
                    state.record_hash_delivery_aliases(
                        message_hash,
                        dest_keys,
                        "already-present",
                        account_id=account_id,
                    )
                logger.debug(
                    "%suid=%s already in Gmail account %s [%s]",
                    prefix,
                    uid,
                    idx + 1,
                    account_id,
                )
                continue

            all_already_had = False
            label_ids = [label_id] if label_id else []
            gmail_id = import_message(
                service,
                USER_ID,
                raw,
                label_ids=label_ids,
                inbox_label_id=inbox_label_id,
                unread_label_id=unread_label_id,
                mark_unread=mark_unread,
                never_mark_spam=never_mark_spam,
            )
            any_imported = True
            if first_gmail_id is None:
                first_gmail_id = gmail_id
            if state is not None and message_hash:
                state.record_hash_delivery_aliases(
                    message_hash,
                    dest_keys,
                    gmail_id,
                    account_id=account_id,
                )
            logger.info(
                "%sImported uid=%s -> Gmail account %s id=%s [%s]",
                prefix,
                uid,
                idx + 1,
                gmail_id,
                account_id,
            )
        except SkipMessageError as e:
            logger.warning(
                "Skipping uid=%s (no From/Sender/Reply-To) [%s]: %s",
                uid,
                account_id,
                e,
            )
            skip_entire_message = True
            all_succeeded = False
            break
        except Exception as e:
            logger.exception(
                "Gmail import failed for uid=%s account %s [%s]: %s",
                uid,
                idx + 1,
                account_id,
                e,
            )
            all_succeeded = False
            break

    return _DeliverResult(
        first_gmail_id=first_gmail_id,
        all_succeeded=all_succeeded,
        any_imported=any_imported,
        all_already_had=all_already_had and all_succeeded,
        skip_entire_message=skip_entire_message,
        message_id_header=message_id_header,
    )


def run_once(
    config_path: str | None = None,
    dry_run: bool = False,
    account_id: str | None = None,
) -> dict[str, Any]:
    """
    Run one fetch cycle for one IMAP account: fetch NEW mail only (UID > last_processed),
    import to routed Gmail destinations, update state, delete from ISP.
    New messages are always imported as unread (used by Run fetch now and by the polling interval).

    If account_id is None, uses the first/sole IMAP account.
    If dry_run is True: fetch from ISP and simulate import (log only), do not delete.
    Returns dict with counts and last_fetch_time for UI.
    """
    if config_path is None:
        from .config import get_config_path

        config_path = str(get_config_path())
    # Resolve password only for this account so a bad sibling account cannot abort us.
    try:
        cfg = load_config(config_path, resolve_password=True, account_id=account_id)
    except ValueError as e:
        return _empty_result(error=str(e), account_id=account_id)
    config_dir = Path(config_path).resolve().parent
    try:
        imap_cfg = get_imap_account(cfg, account_id)
    except ValueError as e:
        return _empty_result(error=str(e), account_id=account_id)
    acct_id = str(imap_cfg.get("id") or DEFAULT_IMAP_ACCOUNT_ID)
    try:
        gmail_accounts = get_gmail_destinations_for_imap(cfg, imap_cfg)
    except ValueError as e:
        return _empty_result(error=str(e), account_id=acct_id)
    if not gmail_accounts:
        return _empty_result(
            error="No Gmail account configured (add 'gmail' or 'gmail_accounts' in config)",
            account_id=acct_id,
        )
    state_cfg = cfg.get("state", {})
    raw_db_path = state_cfg.get("db_path", "state.db")
    db_path = Path(raw_db_path)
    if not db_path.is_absolute():
        db_path = config_dir / db_path
    mailbox = imap_cfg.get("mailbox", "INBOX")
    delete_after_import = imap_cfg.get("delete_after_import", True)
    never_mark_spam = bool(imap_cfg.get("never_mark_spam", True))
    since_date = imap_cfg.get("since_date")
    if since_date == "":
        since_date = None
    password = imap_cfg.get("password")
    if not password:
        return _empty_result(
            error=f"IMAP password not set for account {acct_id!r}",
            account_id=acct_id,
        )

    state = StateStore(str(db_path))
    state.connect()

    imported = 0
    skipped_duplicate = 0
    deleted = 0
    run_error: str | None = None
    gmail_contexts: list[GmailContext] = []
    uid_validity = 0

    try:
        with ImapImportSession(
            host=imap_cfg["host"],
            port=int(imap_cfg.get("port", 993)),
            username=imap_cfg["username"],
            password=password,
            mailbox=mailbox,
            use_ssl=imap_cfg.get("use_ssl", True),
        ) as imap:
            try:
                uid_validity = imap.get_uid_validity()
            except Exception as e:
                logger.exception("IMAP connect (UIDVALIDITY) failed [%s]: %s", acct_id, e)
                return _empty_result(error=str(e), account_id=acct_id)

            last_uid = state.get_last_processed_uid(mailbox, uid_validity, account_id=acct_id)
            try:
                uids = imap.search_uids(last_processed_uid=last_uid, since=since_date)
            except Exception as e:
                logger.exception("IMAP fetch failed [%s]: %s", acct_id, e)
                return _empty_result(error=str(e), account_id=acct_id)

            for uid in uids:
                try:
                    msg = imap.fetch_uid(uid, uid_validity)
                except Exception as e:
                    logger.exception("IMAP FETCH failed for uid=%s [%s]: %s", uid, acct_id, e)
                    return {
                        "error": str(e),
                        "imported": imported,
                        "skipped_duplicate": skipped_duplicate,
                        "deleted": deleted,
                        "account_id": acct_id,
                    }
                if msg is None:
                    logger.warning(
                        "IMAP FETCH returned no body for uid=%s [%s]; stopping this run to retry later",
                        uid,
                        acct_id,
                    )
                    break

                # Source hash alone must not skip/delete: routing may have added
                # destinations that never received this message (UIDVALIDITY revisit).
                if dry_run:
                    logger.info(
                        "[DRY-RUN] Would import uid=%s (hash=%s) [%s]",
                        msg.uid,
                        msg.message_hash[:16],
                        acct_id,
                    )
                    imported += 1
                    continue

                if not gmail_contexts:
                    try:
                        gmail_contexts = _build_gmail_contexts(gmail_accounts, config_dir)
                    except Exception as e:
                        logger.exception("Gmail API init failed [%s]: %s", acct_id, e)
                        return _empty_result(error=str(e), account_id=acct_id)

                deliver = _deliver_to_gmail_destinations(
                    gmail_contexts=gmail_contexts,
                    raw=msg.raw,
                    uid=msg.uid,
                    account_id=acct_id,
                    message_hash=msg.message_hash,
                    never_mark_spam=never_mark_spam,
                    mark_unread=True,
                    state=state,
                )

                if deliver.skip_entire_message:
                    state.set_last_processed_uid(
                        mailbox, uid_validity, msg.uid, account_id=acct_id
                    )
                    continue

                if not deliver.all_succeeded:
                    # Incomplete delivery: leave watermark so missing dests can retry.
                    # Successful dests already recorded in hash_deliveries.
                    run_error = (
                        f"Incomplete Gmail delivery for uid={msg.uid} "
                        f"(one or more destinations failed)"
                    )
                    logger.warning(
                        "Stopping fetch run after incomplete delivery for uid=%s [%s]",
                        msg.uid,
                        acct_id,
                    )
                    break

                if deliver.all_already_had:
                    skipped_duplicate += 1
                    state.record_import(
                        msg.message_hash,
                        "already-present",
                        mailbox,
                        uid_validity,
                        msg.uid,
                        account_id=acct_id,
                    )
                    state.set_last_processed_uid(
                        mailbox, uid_validity, msg.uid, account_id=acct_id
                    )
                    if delete_after_import:
                        try:
                            imap.delete_uid(msg.uid)
                            deleted += 1
                        except Exception as e:
                            logger.warning(
                                "Delete (already in Gmail) failed for uid=%s [%s]: %s",
                                msg.uid,
                                acct_id,
                                e,
                            )
                    continue

                if not deliver.any_imported or deliver.first_gmail_id is None:
                    run_error = f"Empty Gmail import result for uid={msg.uid}"
                    logger.warning(
                        "Stopping fetch run after empty import result for uid=%s [%s]",
                        msg.uid,
                        acct_id,
                    )
                    break

                state.record_import(
                    msg.message_hash,
                    deliver.first_gmail_id,
                    mailbox,
                    uid_validity,
                    msg.uid,
                    account_id=acct_id,
                )
                state.set_last_processed_uid(
                    mailbox, uid_validity, msg.uid, account_id=acct_id
                )
                imported += 1

                if delete_after_import:
                    try:
                        imap.delete_uid(msg.uid)
                        deleted += 1
                    except Exception as e:
                        logger.exception(
                            "Delete failed for uid=%s (already in Gmail) [%s]: %s",
                            msg.uid,
                            acct_id,
                            e,
                        )
    except Exception as e:
        logger.exception("IMAP session failed [%s]: %s", acct_id, e)
        return _empty_result(error=str(e), account_id=acct_id)

    last_fetch = state.get_last_fetch_time(mailbox, uid_validity, account_id=acct_id)
    state.close()
    return {
        "error": run_error,
        "imported": imported,
        "skipped_duplicate": skipped_duplicate,
        "deleted": deleted,
        "last_fetch_time": last_fetch,
        "account_id": acct_id,
    }


def run_copy_all(
    config_path: str | None = None,
    delete_after_import: bool = False,
    dry_run: bool = False,
    account_id: str | None = None,
) -> dict[str, Any]:
    """
    Copy all messages from one ISP mailbox to its routed Gmail destinations.
    Preserves read/unread from IMAP \\Seen. Skips messages already in Gmail (by hash or Message-ID).
    delete_after_import: if True, delete from ISP after successful import (or after skip when already in Gmail).
    Returns same shape as run_once.
    """
    if config_path is None:
        from .config import get_config_path

        config_path = str(get_config_path())
    try:
        cfg = load_config(config_path, resolve_password=True, account_id=account_id)
    except ValueError as e:
        return _empty_result(error=str(e), account_id=account_id)
    config_dir = Path(config_path).resolve().parent
    try:
        imap_cfg = get_imap_account(cfg, account_id)
    except ValueError as e:
        return _empty_result(error=str(e), account_id=account_id)
    acct_id = str(imap_cfg.get("id") or DEFAULT_IMAP_ACCOUNT_ID)
    try:
        gmail_accounts = get_gmail_destinations_for_imap(cfg, imap_cfg)
    except ValueError as e:
        return _empty_result(error=str(e), account_id=acct_id)
    if not gmail_accounts:
        return _empty_result(
            error="No Gmail account configured (add 'gmail' or 'gmail_accounts' in config)",
            account_id=acct_id,
        )
    state_cfg = cfg.get("state", {})
    raw_db_path = state_cfg.get("db_path", "state.db")
    db_path = Path(raw_db_path)
    if not db_path.is_absolute():
        db_path = config_dir / db_path
    mailbox = imap_cfg.get("mailbox", "INBOX")
    never_mark_spam = bool(imap_cfg.get("never_mark_spam", True))
    since_date = imap_cfg.get("since_date")
    if since_date == "":
        since_date = None
    password = imap_cfg.get("password")
    if not password:
        return _empty_result(
            error=f"IMAP password not set for account {acct_id!r}",
            account_id=acct_id,
        )

    state = StateStore(str(db_path))
    state.connect()

    imported = 0
    skipped_duplicate = 0
    deleted = 0
    run_error: str | None = None
    gmail_contexts: list[GmailContext] = []
    uid_validity = 0

    try:
        with ImapImportSession(
            host=imap_cfg["host"],
            port=int(imap_cfg.get("port", 993)),
            username=imap_cfg["username"],
            password=password,
            mailbox=mailbox,
            use_ssl=imap_cfg.get("use_ssl", True),
        ) as imap:
            try:
                uid_validity = imap.get_uid_validity()
            except Exception as e:
                logger.exception("IMAP connect (UIDVALIDITY) failed [%s]: %s", acct_id, e)
                return _empty_result(error=str(e), account_id=acct_id)

            try:
                uids = imap.search_uids(last_processed_uid=None, since=since_date)
            except Exception as e:
                logger.exception("IMAP fetch failed [%s]: %s", acct_id, e)
                return _empty_result(error=str(e), account_id=acct_id)

            for uid in uids:
                try:
                    msg = imap.fetch_uid(uid, uid_validity)
                except Exception as e:
                    logger.exception("IMAP FETCH failed for uid=%s [%s]: %s", uid, acct_id, e)
                    return {
                        "error": str(e),
                        "imported": imported,
                        "skipped_duplicate": skipped_duplicate,
                        "deleted": deleted,
                        "account_id": acct_id,
                    }
                if msg is None:
                    logger.warning(
                        "IMAP FETCH returned no body for uid=%s [%s]; stopping copy-all to retry later",
                        uid,
                        acct_id,
                    )
                    break

                # Never skip/delete from source hash alone — confirm every destination.
                if dry_run:
                    logger.info(
                        "[DRY-RUN] Copy-all would import uid=%s (hash=%s) [%s]",
                        msg.uid,
                        msg.message_hash[:16],
                        acct_id,
                    )
                    imported += 1
                    continue

                if not gmail_contexts:
                    try:
                        gmail_contexts = _build_gmail_contexts(gmail_accounts, config_dir)
                    except Exception as e:
                        logger.exception("Gmail API init failed [%s]: %s", acct_id, e)
                        return _empty_result(error=str(e), account_id=acct_id)

                deliver = _deliver_to_gmail_destinations(
                    gmail_contexts=gmail_contexts,
                    raw=msg.raw,
                    uid=msg.uid,
                    account_id=acct_id,
                    message_hash=msg.message_hash,
                    never_mark_spam=never_mark_spam,
                    mark_unread=not msg.is_seen,
                    state=state,
                    log_prefix="Copy-all",
                )

                if deliver.skip_entire_message:
                    state.set_last_processed_uid(
                        mailbox, uid_validity, msg.uid, account_id=acct_id
                    )
                    continue

                if not deliver.all_succeeded:
                    # Successful dests already recorded in hash_deliveries for retry.
                    run_error = (
                        f"Incomplete Gmail delivery for uid={msg.uid} "
                        f"(one or more destinations failed)"
                    )
                    logger.warning(
                        "Stopping copy-all after incomplete delivery for uid=%s [%s]",
                        msg.uid,
                        acct_id,
                    )
                    break

                if deliver.all_already_had:
                    skipped_duplicate += 1
                    state.record_import(
                        msg.message_hash,
                        "already-present",
                        mailbox,
                        uid_validity,
                        msg.uid,
                        account_id=acct_id,
                    )
                    state.set_last_processed_uid(
                        mailbox, uid_validity, msg.uid, account_id=acct_id
                    )
                    if delete_after_import:
                        try:
                            imap.delete_uid(msg.uid)
                            deleted += 1
                        except Exception as e:
                            logger.warning(
                                "Delete (already in Gmail) failed for uid=%s [%s]: %s",
                                msg.uid,
                                acct_id,
                                e,
                            )
                    continue

                if not deliver.any_imported and deliver.first_gmail_id is None:
                    run_error = f"Empty Gmail import result for uid={msg.uid}"
                    logger.warning(
                        "Stopping copy-all after empty import result for uid=%s [%s]",
                        msg.uid,
                        acct_id,
                    )
                    break

                state.record_import(
                    msg.message_hash,
                    deliver.first_gmail_id or "already-present",
                    mailbox,
                    uid_validity,
                    msg.uid,
                    account_id=acct_id,
                )
                state.set_last_processed_uid(
                    mailbox, uid_validity, msg.uid, account_id=acct_id
                )
                imported += 1

                if delete_after_import:
                    try:
                        imap.delete_uid(msg.uid)
                        deleted += 1
                    except Exception as e:
                        logger.exception(
                            "Delete failed for uid=%s (already in Gmail) [%s]: %s",
                            msg.uid,
                            acct_id,
                            e,
                        )
    except Exception as e:
        logger.exception("IMAP session failed [%s]: %s", acct_id, e)
        return _empty_result(error=str(e), account_id=acct_id)

    last_fetch = state.get_last_fetch_time(mailbox, uid_validity, account_id=acct_id)
    state.close()
    return {
        "error": run_error,
        "imported": imported,
        "skipped_duplicate": skipped_duplicate,
        "deleted": deleted,
        "last_fetch_time": last_fetch,
        "account_id": acct_id,
    }


def run_all_accounts(
    config_path: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run run_once for every IMAP account; aggregate counts."""
    if config_path is None:
        from .config import get_config_path

        config_path = str(get_config_path())
    cfg = load_config(config_path, resolve_password=False)
    accounts = get_imap_accounts(cfg)
    if not accounts:
        return _empty_result(error="No IMAP account configured")

    totals = {"imported": 0, "skipped_duplicate": 0, "deleted": 0}
    per_account: list[dict[str, Any]] = []
    errors: list[str] = []
    last_fetch_time: str | None = None

    for acct in accounts:
        aid = str(acct.get("id") or DEFAULT_IMAP_ACCOUNT_ID)
        result = run_once(config_path=config_path, dry_run=dry_run, account_id=aid)
        per_account.append(result)
        totals["imported"] += int(result.get("imported") or 0)
        totals["skipped_duplicate"] += int(result.get("skipped_duplicate") or 0)
        totals["deleted"] += int(result.get("deleted") or 0)
        if result.get("last_fetch_time"):
            last_fetch_time = str(result["last_fetch_time"])
        if result.get("error"):
            errors.append(f"{aid}: {result['error']}")

    return {
        "error": "; ".join(errors) if errors else None,
        "imported": totals["imported"],
        "skipped_duplicate": totals["skipped_duplicate"],
        "deleted": totals["deleted"],
        "last_fetch_time": last_fetch_time,
        "accounts": per_account,
    }


def setup_logging() -> None:
    """Configure logging to systemd journal (stdout) and structured format."""
    import sys

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    # Suppress noisy googleapiclient message about file_cache and oauth2client
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.WARNING)
