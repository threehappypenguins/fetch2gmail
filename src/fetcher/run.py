"""
Main fetch run: IMAP -> Gmail import -> state update -> delete from ISP.

Idempotency: UID + message SHA256 hash. On UIDVALIDITY change, hashes prevent duplicates.
Only delete from ISP after Gmail confirms import. Dry-run skips Gmail and delete.
Supports multiple Gmail accounts (per-IMAP routing via gmail_token_paths): import to selected
destinations; delete from ISP only if all selected imports succeed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_IMAP_ACCOUNT_ID,
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


def _build_gmail_contexts(
    gmail_accounts: list[dict[str, Any]],
    config_dir: Path,
) -> list[tuple[Any, str | None, str, str]]:
    """
    Build list of (service, label_id, inbox_label_id, unread_label_id) for each account.
    Paths in account dicts are resolved relative to config_dir.
    """
    contexts = []
    for acct in gmail_accounts:
        cred_path = Path(acct.get("credentials_path", "credentials.json"))
        token_path = Path(acct.get("token_path", "token.json"))
        if not cred_path.is_absolute():
            cred_path = config_dir / cred_path
        if not token_path.is_absolute():
            token_path = config_dir / token_path
        use_label = acct.get("use_label") if "use_label" in acct else bool((acct.get("label") or "").strip())
        label_name = (acct.get("label") or "ISP Mail").strip() if use_label else None
        service = get_gmail_service(cred_path, token_path)
        inbox_label_id = get_inbox_label_id(service, USER_ID)
        unread_label_id = get_unread_label_id(service, USER_ID)
        label_id = _ensure_label(service, label_name) if label_name else None
        contexts.append((service, label_id, inbox_label_id, unread_label_id))
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
    cfg = load_config(config_path)
    config_dir = Path(config_path).resolve().parent
    imap_cfg = get_imap_account(cfg, account_id)
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
    gmail_contexts: list[tuple[Any, str | None, str, str]] = []
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

                if state.seen_hash(msg.message_hash, account_id=acct_id):
                    logger.debug("Skipping duplicate (hash): uid=%s [%s]", msg.uid, acct_id)
                    skipped_duplicate += 1
                    if not dry_run:
                        state.set_last_processed_uid(
                            mailbox, uid_validity, msg.uid, account_id=acct_id
                        )
                        if delete_after_import:
                            try:
                                imap.delete_uid(msg.uid)
                                deleted += 1
                            except Exception as e:
                                logger.warning(
                                    "Delete (duplicate) failed for uid=%s [%s]: %s",
                                    msg.uid,
                                    acct_id,
                                    e,
                                )
                    continue

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

                first_gmail_id: str | None = None
                all_succeeded = True
                any_imported = False
                skip_entire_message = False
                for idx, (service, label_id, inbox_label_id, unread_label_id) in enumerate(
                    gmail_contexts
                ):
                    try:
                        label_ids = [label_id] if label_id else []
                        gmail_id = import_message(
                            service,
                            USER_ID,
                            msg.raw,
                            label_ids=label_ids,
                            inbox_label_id=inbox_label_id,
                            unread_label_id=unread_label_id,
                            mark_unread=True,
                            never_mark_spam=never_mark_spam,
                        )
                        any_imported = True
                        if first_gmail_id is None:
                            first_gmail_id = gmail_id
                        logger.info(
                            "Imported uid=%s -> Gmail account %s id=%s [%s]",
                            msg.uid,
                            idx + 1,
                            gmail_id,
                            acct_id,
                        )
                    except SkipMessageError as e:
                        logger.warning(
                            "Skipping uid=%s (no From/Sender/Reply-To) [%s]: %s",
                            msg.uid,
                            acct_id,
                            e,
                        )
                        skip_entire_message = True
                        all_succeeded = False
                        break
                    except Exception as e:
                        logger.exception(
                            "Gmail import failed for uid=%s account %s [%s]: %s",
                            msg.uid,
                            idx + 1,
                            acct_id,
                            e,
                        )
                        all_succeeded = False
                        break

                if skip_entire_message:
                    state.set_last_processed_uid(
                        mailbox, uid_validity, msg.uid, account_id=acct_id
                    )
                    continue

                if not any_imported or first_gmail_id is None:
                    logger.warning(
                        "Stopping fetch run after retryable import failure for uid=%s [%s]",
                        msg.uid,
                        acct_id,
                    )
                    break

                state.record_import(
                    msg.message_hash,
                    first_gmail_id,
                    mailbox,
                    uid_validity,
                    msg.uid,
                    account_id=acct_id,
                )
                state.set_last_processed_uid(
                    mailbox, uid_validity, msg.uid, account_id=acct_id
                )
                imported += 1

                if delete_after_import and all_succeeded:
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
        "error": None,
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
    cfg = load_config(config_path)
    config_dir = Path(config_path).resolve().parent
    imap_cfg = get_imap_account(cfg, account_id)
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
    gmail_contexts: list[tuple[Any, str | None, str, str]] = []
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

                if len(gmail_accounts) <= 1 and state.seen_hash(
                    msg.message_hash, account_id=acct_id
                ):
                    logger.debug(
                        "Copy-all: skipping duplicate (hash): uid=%s [%s]", msg.uid, acct_id
                    )
                    skipped_duplicate += 1
                    if not dry_run:
                        state.set_last_processed_uid(
                            mailbox, uid_validity, msg.uid, account_id=acct_id
                        )
                        if delete_after_import:
                            try:
                                imap.delete_uid(msg.uid)
                                deleted += 1
                            except Exception as e:
                                logger.warning(
                                    "Delete (duplicate) failed for uid=%s [%s]: %s",
                                    msg.uid,
                                    acct_id,
                                    e,
                                )
                    continue

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

                message_id_header = _parse_message_id_from_raw(msg.raw)

                first_gmail_id: str | None = None
                all_succeeded = True
                any_imported = False
                all_already_had = True
                skip_entire_message = False

                for idx, (service, label_id, inbox_label_id, unread_label_id) in enumerate(
                    gmail_contexts
                ):
                    try:
                        already_here = False
                        if message_id_header:
                            already_here = gmail_has_message_with_id(
                                service, USER_ID, message_id_header
                            )
                        if already_here:
                            logger.debug(
                                "Copy-all: uid=%s already in Gmail account %s (Message-ID) [%s]",
                                msg.uid,
                                idx + 1,
                                acct_id,
                            )
                        else:
                            all_already_had = False
                            label_ids = [label_id] if label_id else []
                            gmail_id = import_message(
                                service,
                                USER_ID,
                                msg.raw,
                                label_ids=label_ids,
                                inbox_label_id=inbox_label_id,
                                unread_label_id=unread_label_id,
                                mark_unread=not msg.is_seen,
                                never_mark_spam=never_mark_spam,
                            )
                            any_imported = True
                            if first_gmail_id is None:
                                first_gmail_id = gmail_id
                            logger.info(
                                "Copy-all imported uid=%s -> Gmail account %s id=%s [%s]",
                                msg.uid,
                                idx + 1,
                                gmail_id,
                                acct_id,
                            )
                    except SkipMessageError as e:
                        logger.warning(
                            "Skipping uid=%s (no From/Sender/Reply-To) [%s]: %s",
                            msg.uid,
                            acct_id,
                            e,
                        )
                        skip_entire_message = True
                        all_succeeded = False
                        break
                    except Exception as e:
                        logger.exception(
                            "Gmail import failed for uid=%s account %s [%s]: %s",
                            msg.uid,
                            idx + 1,
                            acct_id,
                            e,
                        )
                        all_succeeded = False
                        break

                if skip_entire_message:
                    state.set_last_processed_uid(
                        mailbox, uid_validity, msg.uid, account_id=acct_id
                    )
                    continue

                if not all_succeeded:
                    logger.warning(
                        "Stopping copy-all after import failure for uid=%s [%s]",
                        msg.uid,
                        acct_id,
                    )
                    break

                if all_already_had and message_id_header:
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

                if not any_imported and first_gmail_id is None:
                    logger.warning(
                        "Stopping copy-all after empty import result for uid=%s [%s]",
                        msg.uid,
                        acct_id,
                    )
                    break

                state.record_import(
                    msg.message_hash,
                    first_gmail_id or "already-present",
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
        "error": None,
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
