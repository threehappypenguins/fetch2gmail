"""Delivery must confirm every Gmail destination before skip/delete."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fetcher.run import _deliver_to_gmail_destinations
from fetcher.state import StateStore


def _ctx(dest: str = "token.json"):
    return (MagicMock(name="service"), "LBL", "INBOX", "UNREAD", dest)


RAW = b"From: a@example.com\r\nMessage-ID: <m1@example.com>\r\n\r\nbody"
RAW_NO_MID = b"From: a@example.com\r\n\r\nbody"
HASH = "abc123hash"


@patch("fetcher.run.import_message", return_value="gid-new")
@patch("fetcher.run.gmail_has_message_with_id")
def test_imports_missing_destination_even_if_other_already_has(mock_has, mock_import):
    # Dest 0 already has it; dest 1 does not — must import to dest 1, not treat as skip-all.
    mock_has.side_effect = [True, False]
    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx("token.json"), _ctx("token2.json")],
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
        gmail_contexts=[_ctx(), _ctx("token2.json")],
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
        gmail_contexts=[_ctx(), _ctx("token2.json")],
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
    store.record_hash_delivery(HASH, "token.json", "gid-old", account_id="isp1")

    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx("token.json"), _ctx("token2.json")],
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
    assert store.has_delivered_hash_to(HASH, "token.json", account_id="isp1")
    assert store.has_delivered_hash_to(HASH, "token2.json", account_id="isp1")
    store.close()


@patch("fetcher.run.import_message")
@patch("fetcher.run.gmail_has_message_with_id", return_value=False)
def test_hash_delivery_makes_retry_without_message_id_idempotent(
    mock_has, mock_import, tmp_path
):
    store = StateStore(tmp_path / "state.db")
    store.connect()
    store.record_hash_delivery(HASH, "token.json", "gid", account_id="isp1")

    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx("token.json")],
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
        gmail_contexts=[_ctx("token.json"), _ctx("token2.json")],
        raw=RAW_NO_MID,
        uid=10,
        account_id="isp1",
        message_hash=HASH,
        never_mark_spam=True,
        mark_unread=True,
        state=store,
    )
    assert first.all_succeeded is False
    assert store.has_delivered_hash_to(HASH, "token.json", account_id="isp1")
    assert not store.has_delivered_hash_to(HASH, "token2.json", account_id="isp1")

    mock_import.reset_mock()
    mock_import.side_effect = ["gid-b"]
    second = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx("token.json"), _ctx("token2.json")],
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
    assert store.has_delivered_hash_to(HASH, "token2.json", account_id="isp1")
    store.close()
