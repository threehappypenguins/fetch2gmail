"""
Main fetch run: IMAP -> Gmail import -> state update -> delete from ISP.

Idempotency: UID + message SHA256 hash. On UIDVALIDITY change, hashes prevent duplicates.
Only delete from ISP after Gmail confirms import. Dry-run skips Gmail and delete.
Supports multiple Gmail accounts: import to all in one run; delete from ISP only if all imports succeed.
"""

import logging
import os
from pathlib import Path
from typing import Any

from .config import get_gmail_accounts, load_config
from .gmail_client import (
    SkipMessageError,
    _parse_message_id_from_raw,
    get_gmail_service,
    get_inbox_label_id,
    get_unread_label_id,
    gmail_has_message_with_id,
    import_message,
)
from .imap_client import delete_and_expunge, fetch_messages, get_uid_validity
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


def run_once(config_path: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """
    Run one fetch cycle: fetch NEW mail only (UID > last_processed), import to Gmail, update state, delete from ISP.
    New messages are always imported as unread (used by Run fetch now and by the polling interval).

    If dry_run is True: fetch from ISP and simulate import (log only), do not delete.
    Returns dict with counts and last_fetch_time for UI.
    """
    if config_path is None:
        from .config import get_config_path
        config_path = str(get_config_path())
    cfg = load_config(config_path)
    config_dir = Path(config_path).resolve().parent
    imap_cfg = cfg["imap"]
    gmail_accounts = get_gmail_accounts(cfg)
    if not gmail_accounts:
        return {"error": "No Gmail account configured (add 'gmail' or 'gmail_accounts' in config)", "imported": 0, "skipped_duplicate": 0, "deleted": 0}
    state_cfg = cfg.get("state", {})
    raw_db_path = state_cfg.get("db_path", "state.db")
    db_path = Path(raw_db_path)
    if not db_path.is_absolute():
        db_path = config_dir / db_path
    mailbox = imap_cfg.get("mailbox", "INBOX")
    delete_after_import = imap_cfg.get("delete_after_import", True)

    state = StateStore(str(db_path))
    state.connect()

    try:
        uid_validity = get_uid_validity(
            host=imap_cfg["host"],
            port=int(imap_cfg.get("port", 993)),
            username=imap_cfg["username"],
            password=imap_cfg["password"],
            mailbox=mailbox,
            use_ssl=imap_cfg.get("use_ssl", True),
        )
    except Exception as e:
        logger.exception("IMAP connect (UIDVALIDITY) failed: %s", e)
        return {"error": str(e), "imported": 0, "skipped_duplicate": 0, "deleted": 0}

    last_uid = state.get_last_processed_uid(mailbox, uid_validity)
    try:
        uid_validity, messages_iter = fetch_messages(
            host=imap_cfg["host"],
            port=int(imap_cfg.get("port", 993)),
            username=imap_cfg["username"],
            password=imap_cfg["password"],
            mailbox=mailbox,
            use_ssl=imap_cfg.get("use_ssl", True),
            last_processed_uid=last_uid,
        )
    except Exception as e:
        logger.exception("IMAP fetch failed: %s", e)
        return {"error": str(e), "imported": 0, "skipped_duplicate": 0, "deleted": 0}

    imported = 0
    skipped_duplicate = 0
    deleted = 0
    gmail_contexts: list[tuple[Any, str | None, str, str]] = []

    if not dry_run:
        try:
            gmail_contexts = _build_gmail_contexts(gmail_accounts, config_dir)
        except Exception as e:
            logger.exception("Gmail API init failed: %s", e)
            return {"error": str(e), "imported": 0, "skipped_duplicate": 0, "deleted": 0}

    for msg in messages_iter:
        if state.seen_hash(msg.message_hash):
            logger.debug("Skipping duplicate (hash): uid=%s", msg.uid)
            skipped_duplicate += 1
            if not dry_run and delete_after_import:
                try:
                    delete_and_expunge(
                        imap_cfg["host"],
                        int(imap_cfg.get("port", 993)),
                        imap_cfg["username"],
                        imap_cfg["password"],
                        mailbox,
                        msg.uid,
                        imap_cfg.get("use_ssl", True),
                    )
                    state.set_last_processed_uid(mailbox, uid_validity, msg.uid)
                    deleted += 1
                except Exception as e:
                    logger.warning("Delete (duplicate) failed for uid=%s: %s", msg.uid, e)
            continue

        if dry_run:
            logger.info("[DRY-RUN] Would import uid=%s (hash=%s)", msg.uid, msg.message_hash[:16])
            imported += 1
            continue

        # Import to all Gmail accounts. Record state when at least one import succeeds; only
        # delete from ISP when *all* imports succeed. This avoids duplicate imports when a
        # subset of accounts succeeded but others failed.
        first_gmail_id: str | None = None
        all_succeeded = True
        any_imported = False
        skip_entire_message = False
        for idx, (service, label_id, inbox_label_id, unread_label_id) in enumerate(gmail_contexts):
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
                )
                any_imported = True
                if first_gmail_id is None:
                    first_gmail_id = gmail_id
                logger.info("Imported uid=%s -> Gmail account %s id=%s", msg.uid, idx + 1, gmail_id)
            except SkipMessageError as e:
                logger.warning("Skipping uid=%s (no From/Sender/Reply-To): %s", msg.uid, e)
                # Message is structurally invalid for Gmail (missing From/Sender/Reply-To). Treat it
                # as permanently unimportable for *all* accounts: do not record a partial success,
                # but do advance last_processed_uid so we don't get stuck retrying this UID forever.
                skip_entire_message = True
                all_succeeded = False
                break
            except Exception as e:
                logger.exception("Gmail import failed for uid=%s account %s: %s", msg.uid, idx + 1, e)
                all_succeeded = False
                break

        if skip_entire_message:
            state.set_last_processed_uid(mailbox, uid_validity, msg.uid)
            # Do not record_import and do not delete from ISP; the message remains only on ISP.
            continue

        if not any_imported or first_gmail_id is None:
            # No successful import this run; do not record state or delete from ISP.
            # Next run will retry for all accounts.
            continue

        state.record_import(msg.message_hash, first_gmail_id, mailbox, uid_validity, msg.uid)
        state.set_last_processed_uid(mailbox, uid_validity, msg.uid)
        imported += 1

        if delete_after_import and all_succeeded:
            try:
                delete_and_expunge(
                    imap_cfg["host"],
                    int(imap_cfg.get("port", 993)),
                    imap_cfg["username"],
                    imap_cfg["password"],
                    mailbox,
                    msg.uid,
                    imap_cfg.get("use_ssl", True),
                )
                deleted += 1
            except Exception as e:
                # Count only successful deletions; failures are logged but not included.
                logger.exception("Delete failed for uid=%s (already in Gmail): %s", msg.uid, e)

    last_fetch = state.get_last_fetch_time(mailbox, uid_validity)
    state.close()
    return {
        "error": None,
        "imported": imported,
        "skipped_duplicate": skipped_duplicate,
        "deleted": deleted,
        "last_fetch_time": last_fetch,
    }


def run_copy_all(
    config_path: str | None = None,
    delete_after_import: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Copy all messages from ISP mailbox to Gmail (not just UID > last_processed).
    Preserves read/unread from IMAP \\Seen. Skips messages already in Gmail (by hash or Message-ID).
    delete_after_import: if True, delete from ISP after successful import (or after skip when already in Gmail).
    Supports multiple Gmail accounts: import to all; delete from ISP only if all imports succeed.
    Returns same shape as run_once.
    """
    if config_path is None:
        from .config import get_config_path
        config_path = str(get_config_path())
    cfg = load_config(config_path)
    config_dir = Path(config_path).resolve().parent
    imap_cfg = cfg["imap"]
    gmail_accounts = get_gmail_accounts(cfg)
    if not gmail_accounts:
        return {"error": "No Gmail account configured (add 'gmail' or 'gmail_accounts' in config)", "imported": 0, "skipped_duplicate": 0, "deleted": 0}
    state_cfg = cfg.get("state", {})
    raw_db_path = state_cfg.get("db_path", "state.db")
    db_path = Path(raw_db_path)
    if not db_path.is_absolute():
        db_path = config_dir / db_path
    mailbox = imap_cfg.get("mailbox", "INBOX")

    state = StateStore(str(db_path))
    state.connect()

    try:
        uid_validity = get_uid_validity(
            host=imap_cfg["host"],
            port=int(imap_cfg.get("port", 993)),
            username=imap_cfg["username"],
            password=imap_cfg["password"],
            mailbox=mailbox,
            use_ssl=imap_cfg.get("use_ssl", True),
        )
    except Exception as e:
        logger.exception("IMAP connect (UIDVALIDITY) failed: %s", e)
        return {"error": str(e), "imported": 0, "skipped_duplicate": 0, "deleted": 0}

    try:
        uid_validity, messages_iter = fetch_messages(
            host=imap_cfg["host"],
            port=int(imap_cfg.get("port", 993)),
            username=imap_cfg["username"],
            password=imap_cfg["password"],
            mailbox=mailbox,
            use_ssl=imap_cfg.get("use_ssl", True),
            last_processed_uid=None,
        )
    except Exception as e:
        logger.exception("IMAP fetch failed: %s", e)
        return {"error": str(e), "imported": 0, "skipped_duplicate": 0, "deleted": 0}

    imported = 0
    skipped_duplicate = 0
    deleted = 0
    gmail_contexts: list[tuple[Any, str | None, str, str]] = []

    if not dry_run:
        try:
            gmail_contexts = _build_gmail_contexts(gmail_accounts, config_dir)
        except Exception as e:
            logger.exception("Gmail API init failed: %s", e)
            return {"error": str(e), "imported": 0, "skipped_duplicate": 0, "deleted": 0}

    for msg in messages_iter:
        # Copy-all is also used as a "repair" tool. With multiple Gmail accounts, we must not
        # skip a message just because one account already has it; we should import to the missing
        # accounts. Therefore:
        # - For single-account setups, we keep the existing hash-based dedupe fast-path.
        # - For multi-account setups, we rely on per-account Message-ID checks when available.
        # IMPORTANT: use gmail_accounts length here (not gmail_contexts) so behavior is correct
        # even in dry-run, where gmail_contexts is intentionally empty.
        if len(gmail_accounts) <= 1 and state.seen_hash(msg.message_hash):
            logger.debug("Copy-all: skipping duplicate (hash): uid=%s", msg.uid)
            skipped_duplicate += 1
            if not dry_run and delete_after_import:
                try:
                    delete_and_expunge(
                        imap_cfg["host"],
                        int(imap_cfg.get("port", 993)),
                        imap_cfg["username"],
                        imap_cfg["password"],
                        mailbox,
                        msg.uid,
                        imap_cfg.get("use_ssl", True),
                    )
                    state.set_last_processed_uid(mailbox, uid_validity, msg.uid)
                    deleted += 1
                except Exception as e:
                    logger.warning("Delete (duplicate) failed for uid=%s: %s", msg.uid, e)
            continue

        if dry_run:
            logger.info(
                "[DRY-RUN] Copy-all would import uid=%s (hash=%s)",
                msg.uid,
                msg.message_hash[:16],
            )
            imported += 1
            continue

        message_id_header = None
        if not dry_run:
            message_id_header = _parse_message_id_from_raw(msg.raw)

        first_gmail_id: str | None = None
        all_succeeded = True
        any_imported = False
        all_already_had = True

        for idx, (service, label_id, inbox_label_id, unread_label_id) in enumerate(gmail_contexts):
            try:
                already_here = False
                if message_id_header:
                    already_here = gmail_has_message_with_id(service, USER_ID, message_id_header)
                if already_here:
                    logger.debug("Copy-all: uid=%s already in Gmail account %s (Message-ID)", msg.uid, idx + 1)
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
                    )
                    any_imported = True
                    if first_gmail_id is None:
                        first_gmail_id = gmail_id
                    logger.info(
                        "Copy-all imported uid=%s -> Gmail account %s id=%s",
                        msg.uid,
                        idx + 1,
                        gmail_id,
                    )
            except SkipMessageError as e:
                logger.warning("Skipping uid=%s (no From/Sender/Reply-To): %s", msg.uid, e)
                all_succeeded = False
                break
            except Exception as e:
                logger.exception("Gmail import failed for uid=%s account %s: %s", msg.uid, idx + 1, e)
                all_succeeded = False
                break

        if not all_succeeded:
            continue

        if all_already_had and message_id_header:
            skipped_duplicate += 1
            # Even when every Gmail account already has the message, we still consider it "handled"
            # for copy-all bookkeeping so we don't re-process it on subsequent copy-all runs.
            state.record_import(
                msg.message_hash,
                "already-present",
                mailbox,
                uid_validity,
                msg.uid,
            )
            state.set_last_processed_uid(mailbox, uid_validity, msg.uid)
            if delete_after_import:
                try:
                    delete_and_expunge(
                        imap_cfg["host"],
                        int(imap_cfg.get("port", 993)),
                        imap_cfg["username"],
                        imap_cfg["password"],
                        mailbox,
                        msg.uid,
                        imap_cfg.get("use_ssl", True),
                    )
                    deleted += 1
                except Exception as e:
                    logger.warning("Delete (already in Gmail) failed for uid=%s: %s", msg.uid, e)
            continue

        if not any_imported and first_gmail_id is None:
            # No Message-ID to check, and no import happened (shouldn't occur); be safe.
            continue

        # Consider the message handled when every account either already had it (by Message-ID)
        # or we successfully imported it. Record a single hash marker for copy-all bookkeeping.
        state.record_import(msg.message_hash, first_gmail_id or "already-present", mailbox, uid_validity, msg.uid)
        state.set_last_processed_uid(mailbox, uid_validity, msg.uid)
        imported += 1

        if delete_after_import:
            try:
                delete_and_expunge(
                    imap_cfg["host"],
                    int(imap_cfg.get("port", 993)),
                    imap_cfg["username"],
                    imap_cfg["password"],
                    mailbox,
                    msg.uid,
                    imap_cfg.get("use_ssl", True),
                )
                deleted += 1
            except Exception as e:
                # Only count successful deletions; failures are logged but not included.
                logger.exception(
                    "Delete failed for uid=%s (already in Gmail): %s", msg.uid, e
                )

    last_fetch = state.get_last_fetch_time(mailbox, uid_validity)
    state.close()
    return {
        "error": None,
        "imported": imported,
        "skipped_duplicate": skipped_duplicate,
        "deleted": deleted,
        "last_fetch_time": last_fetch,
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
