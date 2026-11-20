"""Tests for StateStore account_id schema and migration."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from fetcher.state import DEFAULT_ACCOUNT_ID, StateStore


def test_fresh_db_has_account_id(tmp_path: Path):
    db = tmp_path / "state.db"
    store = StateStore(db)
    store.connect()
    store.set_last_processed_uid("INBOX", 1, 10, account_id="isp1")
    assert store.get_last_processed_uid("INBOX", 1, account_id="isp1") == 10
    assert store.get_last_processed_uid("INBOX", 1, account_id="isp2") is None
    store.record_import("hash1", "gid", "INBOX", 1, 10, account_id="isp1")
    assert store.seen_hash("hash1", account_id="isp1") is True
    assert store.seen_hash("hash1", account_id="isp2") is False
    store.close()


def test_concurrent_connect_creates_schema_once(tmp_path: Path):
    """Two connections must not race on CREATE TABLE uid_state."""
    db = tmp_path / "concurrent.db"
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            store = StateStore(db)
            store.connect()
            store.set_last_processed_uid("INBOX", 1, 1, account_id="a")
            store.close()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, errors
    store = StateStore(db)
    store.connect()
    assert "account_id" in store._table_columns("uid_state")
    store.close()


def test_migrate_legacy_schema(tmp_path: Path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE uid_state (
            mailbox TEXT NOT NULL,
            uid_validity INTEGER NOT NULL,
            last_processed_uid INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (mailbox, uid_validity)
        );
        CREATE TABLE message_hashes (
            message_hash TEXT NOT NULL PRIMARY KEY,
            gmail_message_id TEXT,
            uid_validity INTEGER NOT NULL,
            mailbox TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE uid_to_gmail (
            mailbox TEXT NOT NULL,
            uid_validity INTEGER NOT NULL,
            uid INTEGER NOT NULL,
            gmail_message_id TEXT NOT NULL,
            message_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            PRIMARY KEY (mailbox, uid_validity, uid),
            FOREIGN KEY (message_hash) REFERENCES message_hashes(message_hash)
        );
        INSERT INTO uid_state (mailbox, uid_validity, last_processed_uid)
        VALUES ('INBOX', 42, 99);
        INSERT INTO message_hashes (message_hash, gmail_message_id, uid_validity, mailbox)
        VALUES ('abc', 'g1', 42, 'INBOX');
        INSERT INTO uid_to_gmail (mailbox, uid_validity, uid, gmail_message_id, message_hash)
        VALUES ('INBOX', 42, 99, 'g1', 'abc');
        """
    )
    conn.commit()
    conn.close()

    store = StateStore(db)
    store.connect()
    assert store.get_last_processed_uid("INBOX", 42, account_id=DEFAULT_ACCOUNT_ID) == 99
    assert store.seen_hash("abc", account_id=DEFAULT_ACCOUNT_ID) is True
    # New account_id namespace is separate
    assert store.seen_hash("abc", account_id="other") is False
    store.close()
