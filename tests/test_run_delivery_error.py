"""Failed Gmail destination must return a nonempty error with accumulated counts."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from fetcher.imap_client import FetchedMessage
from fetcher.run import _DeliverResult, run_once


def test_run_once_returns_error_on_incomplete_gmail_delivery(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "imap_accounts": [
                    {
                        "id": "isp1",
                        "host": "imap.example.com",
                        "username": "u",
                        "password": "p",
                        "mailbox": "INBOX",
                        "delete_after_import": False,
                    }
                ],
                "gmail_accounts": [{"token_path": "token.json"}],
                "state": {"db_path": "state.db"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FETCH2GMAIL_CONFIG", str(cfg_path))

    raw = b"From: a@example.com\r\nMessage-ID: <m@x>\r\n\r\nbody"
    msg = FetchedMessage.from_raw(uid=5, uid_validity=1, raw=raw)

    mock_imap = MagicMock()
    mock_imap.get_uid_validity.return_value = 1
    mock_imap.search_uids.return_value = [5]
    mock_imap.fetch_uid.return_value = msg
    mock_imap.__enter__.return_value = mock_imap
    mock_imap.__exit__.return_value = False

    fail_result = _DeliverResult(
        first_gmail_id="gid-partial",
        all_succeeded=False,
        any_imported=True,
        all_already_had=False,
        skip_entire_message=False,
        message_id_header="<m@x>",
    )

    with (
        patch("fetcher.run.ImapImportSession", return_value=mock_imap),
        patch(
            "fetcher.run._build_gmail_contexts",
            return_value=[(MagicMock(), None, "INBOX", "UNREAD", "token.json")],
        ),
        patch(
            "fetcher.run._deliver_to_gmail_destinations",
            return_value=fail_result,
        ),
    ):
        result = run_once(config_path=str(cfg_path), account_id="isp1")

    assert result["error"] is not None
    assert "Incomplete Gmail delivery" in result["error"]
    assert "uid=5" in result["error"]
    assert result["account_id"] == "isp1"
    # Counts from before the failure are preserved (here: none imported yet).
    assert result["imported"] == 0
    assert result["skipped_duplicate"] == 0
    assert result["deleted"] == 0
