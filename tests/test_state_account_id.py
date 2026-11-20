"""Tests for StateStore account_id schema and migration."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from fetcher.state import DEFAULT_ACCOUNT_ID, StateStore


def _write_legacy_schema(db: Path) -> None:
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
    store.record_hash_delivery("hash1", "token.json", "gid", account_id="isp1")
    assert store.has_delivered_hash_to("hash1", "token.json", account_id="isp1")
    assert not store.has_delivered_hash_to("hash1", "token2.json", account_id="isp1")
    assert "hash_deliveries" in store._list_tables()
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
    _write_legacy_schema(db)

    store = StateStore(db)
    store.connect()
    assert store.get_last_processed_uid("INBOX", 42, account_id=DEFAULT_ACCOUNT_ID) == 99
    assert store.seen_hash("abc", account_id=DEFAULT_ACCOUNT_ID) is True
    # New account_id namespace is separate
    assert store.seen_hash("abc", account_id="other") is False
    store.close()


def test_failed_migration_rolls_back_uid_state_new(tmp_path: Path):
    """Mid-rebuild failure must not leave uid_state_new for the next connect."""
    db = tmp_path / "legacy_fail.db"
    _write_legacy_schema(db)

    def failing_migrate(self: StateStore) -> None:
        assert self._conn is not None
        # Create the first *_new table, then fail — previously this survived rollback
        # because the migration txn had already been committed.
        self._conn.execute(
            """
            CREATE TABLE uid_state_new (
                account_id TEXT NOT NULL,
                mailbox TEXT NOT NULL,
                uid_validity INTEGER NOT NULL,
                last_processed_uid INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                PRIMARY KEY (account_id, mailbox, uid_validity)
            )
            """
        )
        raise sqlite3.OperationalError("simulated migration failure")

    store = StateStore(db)
    with patch.object(StateStore, "_migrate_add_account_id", failing_migrate):
        with pytest.raises(sqlite3.OperationalError, match="simulated migration failure"):
            store.connect()
    store.close()

    # Leftover *_new tables would make the next migration fail.
    probe = sqlite3.connect(str(db))
    tables = {
        r[0]
        for r in probe.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    probe.close()
    assert "uid_state_new" not in tables
    assert "message_hashes_new" not in tables
    assert "uid_to_gmail_new" not in tables
    assert "uid_state" in tables  # legacy table still present

    # Retry migration successfully.
    store2 = StateStore(db)
    store2.connect()
    assert store2.get_last_processed_uid("INBOX", 42, account_id=DEFAULT_ACCOUNT_ID) == 99
    assert "account_id" in store2._table_columns("uid_state")
    store2.close()
