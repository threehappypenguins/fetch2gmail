"""
Long-lived IMAP IDLE watcher with polling fallback.

- Uses IMAPClient for IDLE only; import still goes through run_once / imaplib.
- Inspects CAPABILITY for IDLE; falls back to poll_interval_minutes if missing.
- Renews IDLE with NOOP about every idle_refresh_minutes (RFC 2177 ~29 min rule).
- Enables SO_KEEPALIVE and reconnects with exponential backoff on errors.
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable
from typing import Any

from imapclient import IMAPClient

logger = logging.getLogger(__name__)

# Socket / reconnect defaults
_SOCKET_TIMEOUT_SECONDS = 60.0
_IDLE_CHECK_CHUNK_SECONDS = 30.0
_BACKOFF_BASE_SECONDS = 5.0
_BACKOFF_CAP_SECONDS = 300.0  # 5 minutes
_AUTH_BACKOFF_SECONDS = 300.0
_TCP_KEEPIDLE = 60
_TCP_KEEPINTVL = 30
_TCP_KEEPCNT = 5


def server_supports_idle(capabilities: Any) -> bool:
    """Return True if CAPABILITY list includes IDLE (bytes or str tokens)."""
    if not capabilities:
        return False
    for cap in capabilities:
        if isinstance(cap, bytes):
            if cap.upper() == b"IDLE":
                return True
        elif isinstance(cap, str) and cap.upper() == "IDLE":
            return True
    return False


def idle_responses_indicate_new_mail(responses: list[Any] | None) -> bool:
    """True if IDLE untagged responses suggest mailbox message count/activity."""
    if not responses:
        return False
    for item in responses:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        kind = item[1]
        if isinstance(kind, bytes):
            kind_u = kind.upper()
        else:
            kind_u = str(kind).upper().encode("ascii", errors="ignore")
        if kind_u in (b"EXISTS", b"RECENT"):
            return True
    return False


def next_backoff_seconds(attempt: int, base: float = _BACKOFF_BASE_SECONDS, cap: float = _BACKOFF_CAP_SECONDS) -> float:
    """Exponential backoff: base * 2^attempt, capped. attempt is 0-based."""
    if attempt < 0:
        attempt = 0
    return min(cap, base * (2**attempt))


def enable_socket_keepalive(sock: socket.socket) -> None:
    """Enable SO_KEEPALIVE and Linux TCP keepalive knobs when available."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, _TCP_KEEPIDLE)
    if hasattr(socket, "TCP_KEEPINTVL"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, _TCP_KEEPINTVL)
    if hasattr(socket, "TCP_KEEPCNT"):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _TCP_KEEPCNT)


def _close_client(client: IMAPClient | None) -> None:
    if client is None:
        return
    try:
        client.logout()
    except Exception:  # noqa: BLE001
        try:
            client.shutdown()
        except Exception:  # noqa: BLE001
            pass


def connect_idle_client(
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str = "INBOX",
    use_ssl: bool = True,
    timeout: float = _SOCKET_TIMEOUT_SECONDS,
) -> IMAPClient:
    """Connect, login, enable keepalive, select mailbox. Caller must logout/shutdown."""
    ssl_context = ssl.create_default_context() if use_ssl else None
    client = IMAPClient(
        host,
        port=port,
        ssl=use_ssl,
        ssl_context=ssl_context,
        timeout=timeout,
    )
    try:
        enable_socket_keepalive(client.socket())
        client.login(username, password)
        client.select_folder(mailbox, readonly=True)
        return client
    except Exception:
        _close_client(client)
        raise


def _wait_interruptible(stop_event: threading.Event, seconds: float) -> bool:
    """Wait up to seconds; return True if stop was requested."""
    return stop_event.wait(timeout=max(0.0, seconds))


def run_idle_session(
    client: IMAPClient,
    *,
    stop_event: threading.Event,
    idle_refresh_seconds: float,
    on_wake: Callable[[], None],
    check_chunk_seconds: float = _IDLE_CHECK_CHUNK_SECONDS,
) -> None:
    """
    Run IDLE until refresh deadline, new-mail notification, stop, or error.

    Calls on_wake when EXISTS/RECENT is seen. Raises on connection errors.
    """
    idle_started = time.monotonic()
    client.idle()
    try:
        while not stop_event.is_set():
            remaining_refresh = idle_refresh_seconds - (time.monotonic() - idle_started)
            if remaining_refresh <= 0:
                break
            timeout = min(check_chunk_seconds, remaining_refresh)
            responses = client.idle_check(timeout=timeout)
            if stop_event.is_set():
                break
            if idle_responses_indicate_new_mail(responses):
                logger.info("IDLE: mailbox update received; triggering fetch")
                break
        # Exit IDLE before NOOP / fetch
    finally:
        try:
            client.idle_done()
        except Exception:  # noqa: BLE001
            logger.debug("idle_done failed", exc_info=True)

    if stop_event.is_set():
        return

    # Liveness probe: silent NAT drops look like clean idle_check timeouts.
    client.noop()

    # If we woke for new mail OR after refresh, run a fetch (refresh also catches
    # anything missed while IDLE was renewing / silent gaps).
    on_wake()


def run_poll_cycle(
    *,
    stop_event: threading.Event,
    poll_interval_seconds: float,
    on_wake: Callable[[], None],
    first_delay_seconds: float = 30.0,
    last_run_at: float | None,
) -> float:
    """
    Wait until next poll time (interruptible), then call on_wake.
    Returns new last_run_at (monotonic).
    """
    now = time.monotonic()
    if last_run_at is None:
        next_run_at = now + first_delay_seconds
    else:
        next_run_at = last_run_at + poll_interval_seconds
    while not stop_event.is_set() and time.monotonic() < next_run_at:
        _wait_interruptible(stop_event, min(10.0, next_run_at - time.monotonic()))
    if stop_event.is_set():
        return last_run_at if last_run_at is not None else time.monotonic()
    on_wake()
    return time.monotonic()


def watcher_loop(
    stop_event: threading.Event,
    *,
    load_imap_settings: Callable[[], dict[str, Any] | None],
    on_fetch: Callable[[], None],
) -> None:
    """
    Main background loop: connect, IDLE or poll, reconnect with backoff.

    load_imap_settings() should return a dict with:
      host, port, username, password, mailbox, use_ssl,
      use_idle, idle_refresh_minutes, poll_interval_minutes
    or None if config is not ready.
    """
    backoff_attempt = 0
    poll_last_run_at: float | None = None
    logged_no_idle = False

    while not stop_event.is_set():
        settings = None
        try:
            settings = load_imap_settings()
        except Exception as e:
            logger.warning("IDLE watcher could not load config: %s", e)
            _wait_interruptible(stop_event, 60.0)
            continue

        if not settings:
            _wait_interruptible(stop_event, 60.0)
            continue

        host = settings["host"]
        port = int(settings.get("port", 993))
        username = settings["username"]
        password = settings["password"]
        mailbox = settings.get("mailbox") or "INBOX"
        use_ssl = bool(settings.get("use_ssl", True))
        use_idle = bool(settings.get("use_idle", True))
        idle_refresh_minutes = max(1, int(settings.get("idle_refresh_minutes", 25)))
        poll_interval_minutes = max(1, int(settings.get("poll_interval_minutes", 5)))
        idle_refresh_seconds = float(idle_refresh_minutes * 60)
        poll_interval_seconds = float(poll_interval_minutes * 60)

        client: IMAPClient | None = None
        try:
            if use_idle:
                client = connect_idle_client(
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    mailbox=mailbox,
                    use_ssl=use_ssl,
                )
                caps = client.capabilities()
                can_idle = server_supports_idle(caps)
                if not can_idle:
                    if not logged_no_idle:
                        logger.info(
                            "IMAP server does not advertise IDLE; falling back to polling every %s minute(s)",
                            poll_interval_minutes,
                        )
                        logged_no_idle = True
                    _close_client(client)
                    client = None
                else:
                    logged_no_idle = False
                    logger.info(
                        "IMAP IDLE enabled (refresh every %s min; poll fallback interval %s min)",
                        idle_refresh_minutes,
                        poll_interval_minutes,
                    )
                    backoff_attempt = 0
                    # Catch up on anything arrived while disconnected
                    on_fetch()
                    while not stop_event.is_set():
                        try:
                            refreshed = load_imap_settings()
                            if refreshed:
                                idle_refresh_minutes = max(
                                    1,
                                    int(
                                        refreshed.get(
                                            "idle_refresh_minutes", idle_refresh_minutes
                                        )
                                    ),
                                )
                                idle_refresh_seconds = float(idle_refresh_minutes * 60)
                                if not bool(refreshed.get("use_idle", True)):
                                    logger.info(
                                        "IDLE disabled in config; switching to poll mode"
                                    )
                                    break
                        except Exception:  # noqa: BLE001
                            pass

                        run_idle_session(
                            client,
                            stop_event=stop_event,
                            idle_refresh_seconds=idle_refresh_seconds,
                            on_wake=on_fetch,
                        )
                        backoff_attempt = 0
                    else:
                        break
                    _close_client(client)
                    client = None
                    continue

            # Polling fallback (no IDLE capability, use_idle false, or switched off)
            backoff_attempt = 0
            while not stop_event.is_set():
                try:
                    refreshed = load_imap_settings()
                    if refreshed:
                        poll_interval_minutes = max(
                            1,
                            int(
                                refreshed.get(
                                    "poll_interval_minutes", poll_interval_minutes
                                )
                            ),
                        )
                        poll_interval_seconds = float(poll_interval_minutes * 60)
                        # Retry IDLE connect if enabled again and we have not learned
                        # that this server lacks IDLE.
                        if bool(refreshed.get("use_idle", True)) and not logged_no_idle:
                            logger.info("Attempting IMAP IDLE (reconnect)")
                            break
                except Exception:  # noqa: BLE001
                    pass
                poll_last_run_at = run_poll_cycle(
                    stop_event=stop_event,
                    poll_interval_seconds=poll_interval_seconds,
                    on_wake=on_fetch,
                    last_run_at=poll_last_run_at,
                )

        except Exception as e:
            _close_client(client)
            client = None
            msg = str(e).upper()
            is_auth = (
                "AUTHENTICATIONFAILED" in msg
                or "INVALID CREDENTIALS" in msg
                or "LOGIN FAILED" in msg
            )
            if is_auth:
                logger.error("IMAP authentication failed in IDLE watcher: %s", e)
                delay = _AUTH_BACKOFF_SECONDS
            else:
                delay = next_backoff_seconds(backoff_attempt)
                backoff_attempt += 1
                logger.warning(
                    "IDLE watcher connection error (retry in %.0fs): %s",
                    delay,
                    e,
                )
            _wait_interruptible(stop_event, delay)

        finally:
            _close_client(client)
