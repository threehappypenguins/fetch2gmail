"""
IMAP over TLS (IMAPS) client.

- Uses UID, not sequence numbers or UNSEEN.
- Fetches raw RFC822 for import.
- Handles UIDVALIDITY; caller must use it for state.
"""

import hashlib
import imaplib
import logging
import ssl
from dataclasses import dataclass
from typing import Iterator

logger = logging.getLogger(__name__)


def _extract_rfc822(parts: list | None) -> bytes:
    """Extract raw RFC822 message bytes from IMAP FETCH response parts."""
    if not parts:
        return b""
    # parts can be [(b'1 (RFC822 {N}', b'...literal...')] or [b'...', b'...']
    part = parts[0]
    if isinstance(part, tuple):
        if len(part) >= 2 and isinstance(part[1], bytes):
            return part[1]
        return b""
    if isinstance(part, bytes):
        # Single chunk: server may send response and literal in one
        if b"From " in part or b"Return-Path" in part:
            return part
        # Literal may be second element
        if len(parts) >= 2 and isinstance(parts[1], bytes):
            return parts[1]
    return b""


def _extract_flags_seen(parts: list | None) -> bool:
    """Extract FLAGS from IMAP FETCH response; return True if \\Seen is set."""
    if not parts:
        return False
    first = parts[0]
    if isinstance(first, tuple):
        first = first[0] if first else b""
    if isinstance(first, bytes):
        return b"\\Seen" in first
    return "\\Seen" in str(first)


@dataclass
class FetchedMessage:
    """A message fetched by UID with its raw bytes and hash for deduplication."""

    uid: int
    uid_validity: int
    raw: bytes
    message_hash: str
    is_seen: bool = False

    @classmethod
    def from_raw(
        cls,
        uid: int,
        uid_validity: int,
        raw: bytes,
        is_seen: bool = False,
    ) -> "FetchedMessage":
        h = hashlib.sha256(raw).hexdigest()
        return cls(
            uid=uid,
            uid_validity=uid_validity,
            raw=raw,
            message_hash=h,
            is_seen=is_seen,
        )


def get_uid_validity(
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str = "INBOX",
    use_ssl: bool = True,
) -> int:
    """Connect, login, and return UIDVALIDITY for the mailbox (without selecting)."""
    ssl_context = ssl.create_default_context() if use_ssl else None
    if use_ssl:
        conn = imaplib.IMAP4_SSL(host, port=port, ssl_context=ssl_context)
    else:
        conn = imaplib.IMAP4(host, port=port)
    try:
        conn.login(username, password)
        status = conn.status(mailbox, "(UIDVALIDITY)")
        uid_validity = int(status[1][0].decode().split("UIDVALIDITY")[1].strip(" ()"))
        return uid_validity
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def fetch_messages(
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str = "INBOX",
    use_ssl: bool = True,
    last_processed_uid: int | None = None,
) -> tuple[int, Iterator[FetchedMessage]]:
    """
    Connect via IMAPS, select mailbox, return (uid_validity, iterator of new messages).

    Messages are those with UID > last_processed_uid. Ordered by ascending UID.
    Caller must handle UIDVALIDITY changes (e.g. reset state when it changes).
    """
    ssl_context = ssl.create_default_context() if use_ssl else None
    conn = None
    try:
        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port=port, ssl_context=ssl_context)
        else:
            conn = imaplib.IMAP4(host, port=port)
        conn.login(username, password)
        # Use read-only so the server does not set \Seen when we FETCH (preserves unread in Gmail).
        conn.select(mailbox, readonly=True)
        # Get UIDVALIDITY for this mailbox
        status = conn.status(mailbox, "(UIDVALIDITY)")
        # e.g. STATUS INBOX (UIDVALIDITY 12345)
        uid_validity = int(status[1][0].decode().split("UIDVALIDITY")[1].strip(" ()"))
        # Search UIDs > last_processed_uid (all if last_processed_uid is None)
        if last_processed_uid is not None:
            search_criteria = f"UID {last_processed_uid + 1}:*"
        else:
            search_criteria = "UID 1:*"
        _, data = conn.uid("SEARCH", None, search_criteria)
        if not data or not data[0]:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
            return uid_validity, iter(())
        uids = [int(x) for x in data[0].split()]
        uids.sort()
        if not uids:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
            return uid_validity, iter(())

        def fetch_one() -> Iterator[FetchedMessage]:
            try:
                for uid in uids:
                    _, parts = conn.uid("FETCH", str(uid), "(FLAGS RFC822)")
                    raw = _extract_rfc822(parts)
                    if not raw:
                        continue
                    is_seen = _extract_flags_seen(parts)
                    yield FetchedMessage.from_raw(
                        uid, uid_validity, raw, is_seen=is_seen
                    )
            finally:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001
                    pass

        return uid_validity, fetch_one()
    except Exception:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        raise


def delete_and_expunge(
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str,
    uid: int,
    use_ssl: bool = True,
) -> None:
    """Mark message by UID as deleted and expunge (only call after Gmail import succeeded)."""
    ssl_context = ssl.create_default_context() if use_ssl else None
    if use_ssl:
        conn = imaplib.IMAP4_SSL(host, port=port, ssl_context=ssl_context)
    else:
        conn = imaplib.IMAP4(host, port=port)
    try:
        conn.login(username, password)
        conn.select(mailbox, readonly=False)
        conn.uid("STORE", str(uid), "+FLAGS", "\\Deleted")
        conn.expunge()
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
