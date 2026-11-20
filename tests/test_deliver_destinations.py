"""Delivery must confirm every Gmail destination before skip/delete."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fetcher.run import _deliver_to_gmail_destinations


def _ctx():
    return (MagicMock(name="service"), "LBL", "INBOX", "UNREAD")


RAW = b"From: a@example.com\r\nMessage-ID: <m1@example.com>\r\n\r\nbody"


@patch("fetcher.run.import_message", return_value="gid-new")
@patch("fetcher.run.gmail_has_message_with_id")
def test_imports_missing_destination_even_if_other_already_has(mock_has, mock_import):
    # Dest 0 already has it; dest 1 does not — must import to dest 1, not treat as skip-all.
    mock_has.side_effect = [True, False]
    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx(), _ctx()],
        raw=RAW,
        uid=10,
        account_id="isp1",
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
        gmail_contexts=[_ctx(), _ctx()],
        raw=RAW,
        uid=10,
        account_id="isp1",
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
        gmail_contexts=[_ctx(), _ctx()],
        raw=RAW,
        uid=10,
        account_id="isp1",
        never_mark_spam=True,
        mark_unread=True,
    )
    assert result.all_succeeded is False
    assert result.all_already_had is False


@patch("fetcher.run.import_message", return_value="gid")
@patch("fetcher.run.gmail_has_message_with_id")
def test_without_message_id_never_all_already_had(mock_has, mock_import):
    raw = b"From: a@example.com\r\n\r\nbody"
    result = _deliver_to_gmail_destinations(
        gmail_contexts=[_ctx()],
        raw=raw,
        uid=10,
        account_id="isp1",
        never_mark_spam=True,
        mark_unread=True,
    )
    mock_has.assert_not_called()
    assert result.all_already_had is False
    assert result.any_imported is True
