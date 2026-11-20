"""Gmail token load/refresh must be serialized per path and written atomically."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from fetcher.gmail_client import (
    _lock_for_token,
    _token_lock_key,
    atomic_write_token,
    get_gmail_service,
)


def test_atomic_write_token_replaces_without_truncating_existing(tmp_path):
    token = tmp_path / "token.json"
    token.write_text('{"old": true}', encoding="utf-8")
    payload = json.dumps({"access_token": "new", "refresh_token": "r"})

    atomic_write_token(token, payload)

    assert token.read_text(encoding="utf-8") == payload
    assert not list(tmp_path.glob(".token.json.*.tmp"))


def test_token_lock_key_same_for_relative_and_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rel = Path("token.json")
    abs_path = (tmp_path / "token.json")
    assert _token_lock_key(rel) == _token_lock_key(abs_path)
    assert _lock_for_token(rel) is _lock_for_token(abs_path)


def test_get_gmail_service_serializes_refresh_and_writes_once(tmp_path):
    token = tmp_path / "token.json"
    token.write_text('{"token": "expired"}', encoding="utf-8")
    cred_path = tmp_path / "credentials.json"
    cred_path.write_text("{}", encoding="utf-8")

    refresh_calls = {"n": 0}
    barrier = threading.Barrier(2)
    write_events: list[str] = []

    class FakeCreds:
        def __init__(self, *, valid: bool, expired: bool = True):
            self.valid = valid
            self.expired = expired
            self.refresh_token = "refresh-me"

        def refresh(self, _request):
            refresh_calls["n"] += 1
            # Hold the lock briefly so the second thread must wait.
            time.sleep(0.05)
            self.valid = True
            self.expired = False

        def to_json(self):
            write_events.append("write")
            return json.dumps({"token": "fresh", "refresh_token": "refresh-me"})

    def fake_from_file(path, scopes):
        # First load sees expired; after a refresh write, later loads are valid.
        if token.exists() and "fresh" in token.read_text(encoding="utf-8"):
            return FakeCreds(valid=True, expired=False)
        return FakeCreds(valid=False, expired=True)

    results: list[object] = []
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=2)
            results.append(get_gmail_service(cred_path, token))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    with (
        patch(
            "fetcher.gmail_client.Credentials.from_authorized_user_file",
            side_effect=fake_from_file,
        ),
        patch("fetcher.gmail_client.Request", return_value=MagicMock()),
        patch("fetcher.gmail_client.build", return_value=MagicMock(name="service")),
    ):
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

    assert not errors
    assert len(results) == 2
    assert refresh_calls["n"] == 1
    assert write_events == ["write"]
    assert "fresh" in token.read_text(encoding="utf-8")
