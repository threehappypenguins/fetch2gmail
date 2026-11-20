"""Delivery must confirm every Gmail destination before skip/delete."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from fetcher.run import (
    _deliver_to_gmail_destinations,
    gmail_destination_identities,
    gmail_destination_identity,
)
from fetcher.state import StateStore


def _ctx(*dest_keys: str):
    keys = list(dest_keys) if dest_keys else ["gmail:a@example.com"]
    return (MagicMock(name="service"), "LBL", "INBOX", "UNREAD", keys)


RAW = b"From: a@example.com\r\nMessage-ID: <m1@example.com>\r\n\r\nbody"
RAW_NO_MID = b"From: a@example.com\r\n\r\nbody"
HASH = "abc123hash"


@patch("fetcher.run.import_message", return_value="gid-new")
@patch("fetcher.run.gmail_has_message_with_id")
def test_imports_missing_destination_even_if_other_already_has(mock_has, mock_import):
    # Dest 0 already has it; dest 1 does not — must import to dest 1, not treat as skip-all.
    mock_has.side_effect = [True, False]
    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx("gmail:a@example.com"), _ctx("gmail:b@example.com")],
        raw=RAW,
        uid=10,
        account_id="isp1",
        message_hash=HASH,
        never_mark_spam=True,
        mark_unread=True,
    )
    assert result.all_succeeded is True
    assert result.all_already_had is False
    assert result.any_imported is True
    assert mock_import.call_count == 1


@patch("fetcher.run.import_message")
@patch("fetcher.run.gmail_has_message_with_id", return_value=True)
def test_all_destinations_already_had(mock_has, mock_import):
    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx(), _ctx("gmail:b@example.com")],
        raw=RAW,
        uid=10,
        account_id="isp1",
        message_hash=HASH,
        never_mark_spam=True,
        mark_unread=True,
    )
    assert result.all_already_had is True
    assert result.all_succeeded is True
    assert result.any_imported is False
    mock_import.assert_not_called()


@patch("fetcher.run.import_message", return_value="gid")
@patch("fetcher.run.gmail_has_message_with_id", return_value=False)
def test_partial_failure_does_not_claim_success(mock_has, mock_import):
    mock_import.side_effect = [Exception("boom")]
    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx(), _ctx("gmail:b@example.com")],
        raw=RAW,
        uid=10,
        account_id="isp1",
        message_hash=HASH,
        never_mark_spam=True,
        mark_unread=True,
    )
    assert result.all_succeeded is False
    assert result.all_already_had is False


@patch("fetcher.run.import_message", return_value="gid")
@patch("fetcher.run.gmail_has_message_with_id", return_value=False)
def test_without_message_id_imports_when_no_hash_delivery(mock_has, mock_import):
    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx()],
        raw=RAW_NO_MID,
        uid=10,
        account_id="isp1",
        message_hash=HASH,
        never_mark_spam=True,
        mark_unread=True,
    )
    mock_has.assert_not_called()
    assert result.all_already_had is False
    assert result.any_imported is True


@patch("fetcher.run.import_message", return_value="gid-new")
@patch("fetcher.run.gmail_has_message_with_id", return_value=False)
def test_hash_delivery_skips_known_dest_imports_new_dest(mock_has, mock_import, tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.connect()
    store.record_hash_delivery(HASH, "gmail:a@example.com", "gid-old", account_id="isp1")

    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx("gmail:a@example.com"), _ctx("gmail:b@example.com")],
        raw=RAW_NO_MID,
        uid=10,
        account_id="isp1",
        message_hash=HASH,
        never_mark_spam=True,
        mark_unread=True,
        state=store,
    )
    assert result.all_succeeded is True
    assert result.all_already_had is False
    assert result.any_imported is True
    assert mock_import.call_count == 1
    assert store.has_delivered_hash_to(HASH, "gmail:a@example.com", account_id="isp1")
    assert store.has_delivered_hash_to(HASH, "gmail:b@example.com", account_id="isp1")
    store.close()


@patch("fetcher.run.import_message")
@patch("fetcher.run.gmail_has_message_with_id", return_value=False)
def test_hash_delivery_makes_retry_without_message_id_idempotent(
    mock_has, mock_import, tmp_path
):
    store = StateStore(tmp_path / "state.db")
    store.connect()
    store.record_hash_delivery(HASH, "gmail:a@example.com", "gid", account_id="isp1")

    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx("gmail:a@example.com")],
        raw=RAW_NO_MID,
        uid=10,
        account_id="isp1",
        message_hash=HASH,
        never_mark_spam=True,
        mark_unread=True,
        state=store,
    )
    assert result.all_already_had is True
    assert result.any_imported is False
    mock_import.assert_not_called()
    store.close()


@patch("fetcher.run.import_message", return_value="gid-a")
@patch("fetcher.run.gmail_has_message_with_id", return_value=False)
def test_partial_success_records_dest_so_retry_skips_it(mock_has, mock_import, tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.connect()
    mock_import.side_effect = ["gid-a", Exception("boom")]

    first = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx("gmail:a@example.com"), _ctx("gmail:b@example.com")],
        raw=RAW_NO_MID,
        uid=10,
        account_id="isp1",
        message_hash=HASH,
        never_mark_spam=True,
        mark_unread=True,
        state=store,
    )
    assert first.all_succeeded is False
    assert store.has_delivered_hash_to(HASH, "gmail:a@example.com", account_id="isp1")
    assert not store.has_delivered_hash_to(HASH, "gmail:b@example.com", account_id="isp1")

    mock_import.reset_mock()
    mock_import.side_effect = ["gid-b"]
    second = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx("gmail:a@example.com"), _ctx("gmail:b@example.com")],
        raw=RAW_NO_MID,
        uid=10,
        account_id="isp1",
        message_hash=HASH,
        never_mark_spam=True,
        mark_unread=True,
        state=store,
    )
    assert second.all_succeeded is True
    assert second.any_imported is True
    assert mock_import.call_count == 1
    assert store.has_delivered_hash_to(HASH, "gmail:b@example.com", account_id="isp1")
    store.close()


def test_gmail_destination_identity_uses_email_not_filename(tmp_path):
    token = tmp_path / "token2.json"
    token.write_text(json.dumps({"refresh_token": "rt-same"}), encoding="utf-8")
    service = MagicMock()
    service.users().getProfile().execute.return_value = {
        "emailAddress": "New.User@Example.com"
    }
    assert gmail_destination_identity(service, token) == "gmail:new.user@example.com"
    aliases = gmail_destination_identities(service, token)
    assert aliases[0] == "gmail:new.user@example.com"
    assert any(a.startswith("oauth:") for a in aliases)


def test_reused_token_filename_does_not_inherit_prior_account_history(tmp_path):
    """Old account's hash records under a filename must not skip for a new Gmail user."""
    store = StateStore(tmp_path / "state.db")
    store.connect()
    # Legacy path-keyed history from the previous owner of token2.json
    store.record_hash_delivery(HASH, "token2.json", "gid-old", account_id="isp1")
    store.record_hash_delivery(HASH, "gmail:old@example.com", "gid-old", account_id="isp1")

    with (
        patch("fetcher.run.import_message", return_value="gid-new") as mock_import,
        patch("fetcher.run.gmail_has_message_with_id", return_value=False),
    ):
        result = _deliver_to_gmail_destinations(
            gmail_contexts=[_ctx("gmail:new@example.com")],
            raw=RAW_NO_MID,
            uid=10,
            account_id="isp1",
            message_hash=HASH,
            never_mark_spam=True,
            mark_unread=True,
            state=store,
        )
    assert result.any_imported is True
    assert mock_import.call_count == 1
    assert store.has_delivered_hash_to(HASH, "gmail:new@example.com", account_id="isp1")
    store.close()


def test_clear_hash_deliveries_for_destinations(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.connect()
    store.record_hash_delivery(HASH, "gmail:old@example.com", "gid", account_id="isp1")
    store.record_hash_delivery(HASH, "token2.json", "gid", account_id="isp1")
    store.record_hash_delivery(HASH, "gmail:keep@example.com", "gid", account_id="isp1")
    removed = store.clear_hash_deliveries_for_destinations(
        {"gmail:old@example.com", "token2.json"}
    )
    assert removed == 2
    assert not store.has_delivered_hash_to(HASH, "gmail:old@example.com", account_id="isp1")
    assert store.has_delivered_hash_to(HASH, "gmail:keep@example.com", account_id="isp1")
    store.close()


def test_oauth_fingerprint_fallback_when_profile_unavailable(tmp_path):
    token = tmp_path / "token2.json"
    token.write_text(json.dumps({"refresh_token": "rt-secret-1"}), encoding="utf-8")
    service = MagicMock()
    service.users().getProfile().execute.side_effect = RuntimeError("offline")
    key = gmail_destination_identity(service, token)
    assert key.startswith("oauth:")
    # Different refresh token → different identity (reused filename, new account).
    token.write_text(json.dumps({"refresh_token": "rt-secret-2"}), encoding="utf-8")
    key2 = gmail_destination_identity(service, token)
    assert key2.startswith("oauth:")
    assert key2 != key


def test_profile_failure_alias_does_not_reimport_after_success(tmp_path):
    """
    Success under gmail:+oauth: must still skip when a later run only has oauth:
    (transient getProfile failure), and vice versa after recovery.
    """
    store = StateStore(tmp_path / "state.db")
    store.connect()
    email_key = "gmail:user@example.com"
    oauth_key = "oauth:deadbeefcafebabe0123456789abcdef"

    with (
        patch("fetcher.run.import_message", return_value="gid-1") as mock_import,
        patch("fetcher.run.gmail_has_message_with_id", return_value=False),
    ):
        first = _deliver_to_gmail_destinations(
            gmail_contexts=[_ctx(email_key, oauth_key)],
            raw=RAW_NO_MID,
            uid=10,
            account_id="isp1",
            message_hash=HASH,
            never_mark_spam=True,
            mark_unread=True,
            state=store,
        )
    assert first.any_imported is True
    assert mock_import.call_count == 1
    assert store.has_delivered_hash_to(HASH, email_key, account_id="isp1")
    assert store.has_delivered_hash_to(HASH, oauth_key, account_id="isp1")

    # Profile down: only oauth alias available — must not re-import.
    with (
        patch("fetcher.run.import_message", return_value="gid-2") as mock_import,
        patch("fetcher.run.gmail_has_message_with_id", return_value=False),
    ):
        second = _deliver_to_gmail_destinations(
            gmail_contexts=[_ctx(oauth_key)],
            raw=RAW_NO_MID,
            uid=10,
            account_id="isp1",
            message_hash=HASH,
            never_mark_spam=True,
            mark_unread=True,
            state=store,
        )
    assert second.all_already_had is True
    assert second.any_imported is False
    mock_import.assert_not_called()

    # Profile recovers with both aliases again — still skip.
    with (
        patch("fetcher.run.import_message", return_value="gid-3") as mock_import,
        patch("fetcher.run.gmail_has_message_with_id", return_value=False),
    ):
        third = _deliver_to_gmail_destinations(
            gmail_contexts=[_ctx(email_key, oauth_key)],
            raw=RAW_NO_MID,
            uid=10,
            account_id="isp1",
            message_hash=HASH,
            never_mark_spam=True,
            mark_unread=True,
            state=store,
        )
    assert third.all_already_had is True
    mock_import.assert_not_called()
    store.close()
