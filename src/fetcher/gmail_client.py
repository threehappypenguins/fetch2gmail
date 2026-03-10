"""
Gmail API client: import messages (users.messages.import), preserve headers/date, apply label.

Uses OAuth2 refresh token. Exponential backoff on transient errors.
"""

import base64
import logging
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

# Raised when a message cannot be normalized for Gmail (e.g. no From/Sender/Reply-To).
class SkipMessageError(ValueError):
    """Message should be skipped for import (e.g. missing From)."""
    pass

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def get_gmail_service(credentials_path: str | Path, token_path: str | Path):
    """
    Build Gmail API service using stored credentials and token.
    Refreshes access token from refresh token when needed (no interactive login after setup).
    """
    cred_path = Path(credentials_path)
    tok_path = Path(token_path)
    creds = None
    if tok_path.exists():
        creds = Credentials.from_authorized_user_file(str(tok_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not cred_path.exists():
                raise FileNotFoundError(
                    f"Credentials file not found: {cred_path}. "
                    "Create OAuth client and download credentials.json. See README."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
            # For headless: run local server once to get refresh token, then save token.json
            creds = flow.run_local_server(port=0)
        with open(tok_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _normalize_from_header(raw: bytes) -> bytes:
    """
    Ensure the message has exactly one From header. Gmail import requires it.
    - Multiple From: keep the first.
    - No From: use Sender or Reply-To if present; otherwise raise SkipMessageError
      so the message is skipped (never inject a fake address).
    """
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        from_vals = msg.get_all("From") or []
        if len(from_vals) == 0:
            fallback = (msg.get("Sender") or msg.get("Reply-To") or "").strip()
            if not fallback:
                raise SkipMessageError("Message has no From, Sender, or Reply-To header")
            msg["From"] = fallback
            logger.debug("Message had no From header; using %s", fallback[:50])
        elif len(from_vals) > 1:
            msg["From"] = from_vals[0]
            logger.debug("Message had %s From headers; kept first", len(from_vals))
        else:
            return raw
        return msg.as_bytes()
    except SkipMessageError:
        raise
    except Exception:  # noqa: BLE001
        return raw


def _parse_date_from_raw(raw: bytes) -> str | None:
    """Extract Date header from raw message for Gmail internalDate."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        date_str = msg.get("Date")
        if not date_str:
            return None
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(date_str)
        return str(int(dt.timestamp() * 1000))
    except Exception:  # noqa: BLE001
        return None


def _get_system_label_id(service: Any, user_id: str, name: str, fallback: str) -> str:
    """Return a system label ID (e.g. INBOX, UNREAD). Resolve from API in case it differs."""
    try:
        labels = service.users().labels().list(userId=user_id).execute()
        for lab in labels.get("labels", []):
            if lab.get("type") == "system" and lab.get("name") == name:
                return lab["id"]
    except Exception:  # noqa: BLE001
        pass
    return fallback


def get_inbox_label_id(service: Any, user_id: str = "me") -> str:
    """Return the INBOX label ID so imported messages appear in Inbox."""
    return _get_system_label_id(service, user_id, "INBOX", "INBOX")


def get_unread_label_id(service: Any, user_id: str = "me") -> str:
    """Return the UNREAD label ID so imported messages appear as unread."""
    return _get_system_label_id(service, user_id, "UNREAD", "UNREAD")


def import_message(
    service: Any,
    user_id: str,
    raw: bytes,
    label_ids: list[str],
    inbox_label_id: str | None = None,
    unread_label_id: str | None = None,
    mark_unread: bool = True,
) -> str:
    """
    Import a single message using users.messages.import.
    Preserves original headers and sets internalDate when possible.
    If mark_unread is False (message was read on ISP), the message is imported as read.
    Returns Gmail message ID.
    """
    raw = _normalize_from_header(raw)
    body = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
    internal_date = _parse_date_from_raw(raw)
    if internal_date:
        body["internalDate"] = internal_date
    inbox_id = inbox_label_id or get_inbox_label_id(service, user_id)
    unread_id = unread_label_id or get_unread_label_id(service, user_id)
    body["labelIds"] = list(label_ids) if label_ids else []
    if inbox_id not in body["labelIds"]:
        body["labelIds"].append(inbox_id)
    if mark_unread and unread_id not in body["labelIds"]:
        body["labelIds"].append(unread_id)

    msg = _execute_with_backoff(
        lambda: service.users().messages().import_(
            userId=user_id,
            body=body,
        ).execute()
    )
    return msg["id"]


def _parse_message_id_from_raw(raw: bytes) -> str | None:
    """Extract Message-ID header from raw message. Returns None if missing."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        mid = msg.get("Message-ID")
        return mid.strip() if mid else None
    except Exception:  # noqa: BLE001
        return None


def gmail_has_message_with_id(
    service: Any, user_id: str, rfc822_message_id: str
) -> bool:
    """
    Return True if Gmail already has a message with the given RFC822 Message-ID
    (checks the account, so we can skip re-importing). Used for copy-all deduplication.
    """
    if not rfc822_message_id or not rfc822_message_id.strip():
        return False
    # Gmail search: rfc822msgid:<value>. Value may contain angle brackets; keep as-is.
    q = f"rfc822msgid:{rfc822_message_id.strip()}"
    try:
        resp = _execute_with_backoff(
            lambda: service.users()
            .messages()
            .list(userId=user_id, q=q, maxResults=1)
            .execute()
        )
        return bool(resp.get("messages"))
    except Exception:  # noqa: BLE001
        return False


def _execute_with_backoff(request_fn, max_retries: int = 5):
    """Execute Gmail API request with exponential backoff on 5xx and rate limits."""
    delay = 1.0
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return request_fn()
        except HttpError as e:
            last_error = e
            status = e.resp.status if e.resp else 0
            if status in (429, 500, 502, 503) and attempt < max_retries:
                logger.warning(
                    "Gmail API error %s (attempt %s/%s), retrying in %.1fs",
                    status,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
            else:
                raise
        except Exception:
            raise
    if last_error:
        raise last_error
    raise RuntimeError("Unexpected retry loop exit")
