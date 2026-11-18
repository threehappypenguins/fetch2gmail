"""Unit tests for IMAP IDLE watcher helpers."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from fetcher.idle_watcher import (
    idle_responses_indicate_new_mail,
    next_backoff_seconds,
    run_idle_session,
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


def test_run_idle_session_wakes_on_exists_and_calls_noop():
    client = MagicMock()
    client.idle_check.return_value = [(4, b"EXISTS")]
    wakes: list[int] = []
    stop = threading.Event()

    run_idle_session(
        client,
        stop_event=stop,
        idle_refresh_seconds=60,
        on_wake=lambda: wakes.append(1),
        check_chunk_seconds=1,
    )

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

    run_idle_session(
        client,
        stop_event=stop,
        idle_refresh_seconds=60,
        on_wake=lambda: wakes.append(1),
        check_chunk_seconds=1,
    )

    client.idle_done.assert_called_once()
    client.noop.assert_not_called()
    assert wakes == []
