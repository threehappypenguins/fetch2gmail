"""Tests for per-account IDLE pause gates."""

from __future__ import annotations

import threading
import time

from fetcher.idle_watcher import (
    _idle_suspend_requested,
    _set_idle_connection_held,
    pause_idle_connection,
)


def test_pause_is_isolated_per_account():
    assert _idle_suspend_requested("a") is False
    assert _idle_suspend_requested("b") is False
    with pause_idle_connection(account_id="a", timeout_seconds=2):
        assert _idle_suspend_requested("a") is True
        assert _idle_suspend_requested("b") is False
    assert _idle_suspend_requested("a") is False


def test_pause_waits_for_held_connection_on_same_account():
    held_cleared = threading.Event()

    def release_later():
        time.sleep(0.2)
        _set_idle_connection_held(False, "isp1")
        held_cleared.set()

    _set_idle_connection_held(True, "isp1")
    threading.Thread(target=release_later, daemon=True).start()
    started = time.monotonic()
    with pause_idle_connection(account_id="isp1", timeout_seconds=5):
        assert held_cleared.is_set() or (time.monotonic() - started) >= 0.15
    _set_idle_connection_held(False, "isp1")
