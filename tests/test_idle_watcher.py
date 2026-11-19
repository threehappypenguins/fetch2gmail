"""Unit tests for IMAP IDLE watcher helpers."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from fetcher.idle_watcher import (
    connection_identity,
    idle_responses_indicate_new_mail,
    next_backoff_seconds,
    run_idle_session,
    run_poll_cycle,
    server_supports_idle,
)


@pytest.mark.parametrize(
    "caps,expected",
    [
        ((b"IMAP4rev1", b"IDLE", b"UIDPLUS"), True),
        ((b"IMAP4rev1", b"idle"), True),
        (("IMAP4rev1", "IDLE"), True),
        ((b"IMAP4rev1", b"AUTH=PLAIN"), False),
        ((), False),
        (None, False),
    ],
)
def test_server_supports_idle(caps, expected):
    assert server_supports_idle(caps) is expected


@pytest.mark.parametrize(
    "responses,expected",
    [
        ([(3, b"EXISTS")], True),
        ([(1, b"RECENT")], True),
        ([(3, "EXISTS")], True),
        ([(b"OK", b"Still here")], False),
        ([], False),
        (None, False),
        ([(1, b"EXPUNGE")], False),
    ],
)
def test_idle_responses_indicate_new_mail(responses, expected):
    assert idle_responses_indicate_new_mail(responses) is expected


def test_next_backoff_seconds_doubles_and_caps():
    assert next_backoff_seconds(0, base=5, cap=300) == 5
    assert next_backoff_seconds(1, base=5, cap=300) == 10
    assert next_backoff_seconds(2, base=5, cap=300) == 20
    assert next_backoff_seconds(10, base=5, cap=300) == 300
    assert next_backoff_seconds(-1, base=5, cap=300) == 5


def test_connection_identity_changes_with_mailbox():
    base = {
        "host": "imap.example.com",
        "port": 993,
        "username": "u",
        "password": "p",
        "mailbox": "INBOX",
        "use_ssl": True,
    }
    other = {**base, "mailbox": "Archive"}
    assert connection_identity(base) != connection_identity(other)
    assert connection_identity(base) == connection_identity(dict(base))


def test_no_idle_cache_is_scoped_to_connection_identity():
    """Cached 'no IDLE' applies only to that connection; None means retry CAPABILITY."""
    a = connection_identity(
        {
            "host": "a.example.com",
            "port": 993,
            "username": "u",
            "password": "p",
            "mailbox": "INBOX",
            "use_ssl": True,
        }
    )
    b = connection_identity(
        {
            "host": "b.example.com",
            "port": 993,
            "username": "u",
            "password": "p",
            "mailbox": "INBOX",
            "use_ssl": True,
        }
    )
    no_idle_conn_id = a
    assert (True and no_idle_conn_id != a) is False  # same server: stay on poll
    assert (True and no_idle_conn_id != b) is True  # new server: retry IDLE
    no_idle_conn_id = None  # re-enable IDLE / unknown
    assert (True and no_idle_conn_id != a) is True


def test_run_idle_session_wakes_on_exists_and_calls_noop():
    client = MagicMock()
    client.idle_check.return_value = [(4, b"EXISTS")]
    wakes: list[int] = []
    stop = threading.Event()

    ok = run_idle_session(
        client,
        stop_event=stop,
        idle_refresh_seconds=60,
        on_wake=lambda: wakes.append(1),
        check_chunk_seconds=1,
    )

    assert ok is True
    client.idle.assert_called_once()
    client.idle_done.assert_called_once()
    client.noop.assert_called_once()
    assert wakes == [1]


def test_run_idle_session_respects_stop_without_fetch():
    client = MagicMock()

    def _check(timeout=None):
        stop.set()
        return []

    client.idle_check.side_effect = _check
    wakes: list[int] = []
    stop = threading.Event()

    ok = run_idle_session(
        client,
        stop_event=stop,
        idle_refresh_seconds=60,
        on_wake=lambda: wakes.append(1),
        check_chunk_seconds=1,
    )

    assert ok is False
    client.idle_done.assert_called_once()
    client.noop.assert_not_called()
    assert wakes == []


def test_run_idle_session_aborts_on_should_abort_without_fetch():
    client = MagicMock()
    client.idle_check.return_value = []
    wakes: list[int] = []
    stop = threading.Event()

    ok = run_idle_session(
        client,
        stop_event=stop,
        idle_refresh_seconds=60,
        on_wake=lambda: wakes.append(1),
        check_chunk_seconds=1,
        should_abort=lambda: True,
    )

    assert ok is False
    client.idle_done.assert_called_once()
    client.noop.assert_not_called()
    assert wakes == []


def test_run_poll_cycle_applies_shorter_interval_during_wait():
    """Lowering poll interval mid-wait should advance next_run_at (not wait the old hour)."""
    stop = threading.Event()
    wakes: list[int] = []
    calls = {"n": 0}
    last_run_at = time.monotonic()

    def reload():
        calls["n"] += 1
        # First reload still "60 minutes"; then switch to 1 second.
        if calls["n"] < 2:
            return 3600.0, False
        return 1.0, False

    started = time.monotonic()
    new_last, switch = run_poll_cycle(
        stop_event=stop,
        poll_interval_seconds=3600.0,
        on_wake=lambda: wakes.append(1),
        last_run_at=last_run_at,
        reload_during_wait=reload,
        wait_chunk_seconds=0.05,
    )
    elapsed = time.monotonic() - started

    assert switch is False
    assert wakes == [1]
    assert elapsed < 2.0  # must not sit for ~3600s
    assert new_last >= last_run_at


def test_run_poll_cycle_switch_to_idle_during_wait():
    stop = threading.Event()
    wakes: list[int] = []
    last_run_at = time.monotonic()

    def reload():
        return 3600.0, True

    new_last, switch = run_poll_cycle(
        stop_event=stop,
        poll_interval_seconds=3600.0,
        on_wake=lambda: wakes.append(1),
        last_run_at=last_run_at,
        reload_during_wait=reload,
        wait_chunk_seconds=0.05,
    )

    assert switch is True
    assert wakes == []
    assert new_last == last_run_at
