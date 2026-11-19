"""
SQLite state store for idempotent fetch.

Stores (scoped by account_id):
- uid_validity per mailbox (to detect server resets)
- last_processed_uid per (account_id, mailbox, uid_validity)
- message_hash -> gmail_message_id (to dedupe when UID/UIDVALIDITY change)
- uid -> gmail_message_id (optional, for quick lookup)
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_ACCOUNT_ID = "default"


class StateStore:
    """Thread-safe SQLite state for UID tracking and hash-based deduplication."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), timeout=30)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._create_tables()

    def _table_columns(self, table: str) -> set[str]:
        assert self._conn is not None
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r[1]) for r in rows}

    def _needs_account_id_migration(self) -> bool:
        assert self._conn is not None
        tables = {
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "uid_state" not in tables:
            return False
        return "account_id" not in self._table_columns("uid_state")

    def _migrate_add_account_id(self) -> None:
        """Rebuild tables to include account_id; existing rows become account_id='default'."""
        assert self._conn is not None
        logger.info("Migrating state.db to add account_id column (default=%r)", DEFAULT_ACCOUNT_ID)
        self._conn.executescript(
            f"""
            BEGIN;
            CREATE TABLE uid_state_new (
                account_id TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                uid_validity INTEGER NOT NULL,
                last_processed_uid INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (account_id, mailbox, uid_validity)
            );
            INSERT INTO uid_state_new (account_id, mailbox, uid_validity, last_processed_uid, updated_at)
            SELECT '{DEFAULT_ACCOUNT_ID}', mailbox, uid_validity, last_processed_uid, updated_at
            FROM uid_state;

            CREATE TABLE message_hashes_new (
                account_id TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                gmail_message_id TEXT,
                uid_validity INTEGER NOT NULL,
                mailbox TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (account_id, message_hash)
            );
            INSERT INTO message_hashes_new (account_id, message_hash, gmail_message_id, uid_validity, mailbox, created_at)
            SELECT '{DEFAULT_ACCOUNT_ID}', message_hash, gmail_message_id, uid_validity, mailbox, created_at
            FROM message_hashes;

            CREATE TABLE uid_to_gmail_new (
                account_id TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                uid_validity INTEGER NOT NULL,
                uid INTEGER NOT NULL,
                gmail_message_id TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (account_id, mailbox, uid_validity, uid),
                FOREIGN KEY (account_id, message_hash)
                    REFERENCES message_hashes_new(account_id, message_hash)
            );
            INSERT INTO uid_to_gmail_new (
                account_id, mailbox, uid_validity, uid, gmail_message_id, message_hash, created_at
            )
            SELECT '{DEFAULT_ACCOUNT_ID}', mailbox, uid_validity, uid, gmail_message_id, message_hash, created_at
            FROM uid_to_gmail;

            DROP TABLE uid_to_gmail;
            DROP TABLE message_hashes;
            DROP TABLE uid_state;
            ALTER TABLE uid_state_new RENAME TO uid_state;
            ALTER TABLE message_hashes_new RENAME TO message_hashes;
            ALTER TABLE uid_to_gmail_new RENAME TO uid_to_gmail;
            CREATE INDEX IF NOT EXISTS idx_uid_to_gmail_hash ON uid_to_gmail(message_hash);
            COMMIT;
            """
        )

    def _create_tables(self) -> None:
        assert self._conn is not None
        tables = {
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "uid_state" not in tables:
            self._conn.executescript(
                """
                CREATE TABLE uid_state (
                    account_id TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    uid_validity INTEGER NOT NULL,
                    last_processed_uid INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    PRIMARY KEY (account_id, mailbox, uid_validity)
                );
                CREATE TABLE message_hashes (
                    account_id TEXT NOT NULL,
                    message_hash TEXT NOT NULL,
                    gmail_message_id TEXT,
                    uid_validity INTEGER NOT NULL,
                    mailbox TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    PRIMARY KEY (account_id, message_hash)
                );
                CREATE TABLE uid_to_gmail (
                    account_id TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    uid_validity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    gmail_message_id TEXT NOT NULL,
                    message_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    PRIMARY KEY (account_id, mailbox, uid_validity, uid),
                    FOREIGN KEY (account_id, message_hash)
                        REFERENCES message_hashes(account_id, message_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_uid_to_gmail_hash ON uid_to_gmail(message_hash);
                """
            )
            self._conn.commit()
            return

        if self._needs_account_id_migration():
            self._migrate_add_account_id()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_uid_to_gmail_hash ON uid_to_gmail(message_hash)"
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def get_last_processed_uid(
        self,
        mailbox: str,
        uid_validity: int,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> Optional[int]:
        """Return last processed UID for this account/mailbox/UIDVALIDITY, or None."""
        self.connect()
        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT last_processed_uid FROM uid_state
            WHERE account_id = ? AND mailbox = ? AND uid_validity = ?
            """,
            (account_id, mailbox, uid_validity),
        ).fetchone()
        return int(row[0]) if row else None

    def set_last_processed_uid(
        self,
        mailbox: str,
        uid_validity: int,
        uid: int,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> None:
        """Update last processed UID (call after successful Gmail import and ISP delete)."""
        self.connect()
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT INTO uid_state (account_id, mailbox, uid_validity, last_processed_uid, updated_at)
            VALUES (?, ?, ?, ?, datetime('now', 'localtime'))
            ON CONFLICT (account_id, mailbox, uid_validity) DO UPDATE SET
                last_processed_uid = excluded.last_processed_uid,
                updated_at = datetime('now', 'localtime')
            """,
            (account_id, mailbox, uid_validity, uid),
        )
        self._conn.commit()

    def seen_hash(
        self,
        message_hash: str,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> bool:
        """Return True if we have already imported a message with this SHA256 hash for this account."""
        self.connect()
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT 1 FROM message_hashes WHERE account_id = ? AND message_hash = ?",
            (account_id, message_hash),
        ).fetchone()
        return row is not None

    def record_import(
        self,
        message_hash: str,
        gmail_message_id: str,
        mailbox: str,
        uid_validity: int,
        uid: int,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> None:
        """Record a successful import: hash -> Gmail ID and UID -> Gmail ID."""
        self.connect()
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT OR IGNORE INTO message_hashes
                (account_id, message_hash, gmail_message_id, uid_validity, mailbox, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
            """,
            (account_id, message_hash, gmail_message_id, uid_validity, mailbox),
        )
        self._conn.execute(
            """
            INSERT OR REPLACE INTO uid_to_gmail
                (account_id, mailbox, uid_validity, uid, gmail_message_id, message_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            """,
            (account_id, mailbox, uid_validity, uid, gmail_message_id, message_hash),
        )
        self._conn.commit()

    def get_last_fetch_time(
        self,
        mailbox: str,
        uid_validity: int,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> Optional[str]:
        """Return last updated_at for this account/mailbox/uid_validity (for UI)."""
        self.connect()
        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT updated_at FROM uid_state
            WHERE account_id = ? AND mailbox = ? AND uid_validity = ?
            """,
            (account_id, mailbox, uid_validity),
        ).fetchone()
        return row[0] if row else None

    def get_last_fetch_time_any(
        self,
        mailbox: str,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> Optional[tuple[str, int]]:
        """Return (updated_at, uid_validity) for the most recent row for this account/mailbox."""
        self.connect()
        assert self._conn is not None
        row = self._conn.execute(
            """
            SELECT updated_at, uid_validity FROM uid_state
            WHERE account_id = ? AND mailbox = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (account_id, mailbox),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def get_all_last_fetch_times(self) -> list[dict[str, object]]:
        """Return latest fetch row per account_id for status UI."""
        self.connect()
        assert self._conn is not None
        rows = self._conn.execute(
            """
            SELECT account_id, mailbox, updated_at, uid_validity
            FROM uid_state
            ORDER BY updated_at DESC
            """
        ).fetchall()
        seen: set[str] = set()
        out: list[dict[str, object]] = []
        for account_id, mailbox, updated_at, uid_validity in rows:
            if account_id in seen:
                continue
            seen.add(str(account_id))
            out.append(
                {
                    "account_id": account_id,
                    "mailbox": mailbox,
                    "last_fetch_time": updated_at,
                    "uid_validity": uid_validity,
                }
            )
        return out
