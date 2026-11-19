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


def connection_identity(settings: dict[str, Any]) -> tuple[Any, ...]:
    """Stable tuple of IMAP connection fields; change implies reconnect."""
    return (
        settings.get("host"),
        int(settings.get("port", 993)),
        settings.get("username"),
        settings.get("password"),
        settings.get("mailbox") or "INBOX",
        bool(settings.get("use_ssl", True)),
    )


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
    should_abort: Callable[[], bool] | None = None,
) -> bool:
    """
    Run IDLE until refresh deadline, new-mail notification, stop, abort, or error.

    Calls on_wake when EXISTS/RECENT is seen or after a normal refresh.
    should_abort is checked between idle_check chunks (e.g. connection settings changed).
    Returns False if aborted early (no on_wake); True after a normal wake/refresh fetch.
    Raises on connection errors.
    """
    aborted = False
    idle_started = time.monotonic()
    client.idle()
    try:
        while not stop_event.is_set():
            if should_abort is not None:
                try:
                    if should_abort():
                        aborted = True
                        break
                except Exception:  # noqa: BLE001
                    pass
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

    if stop_event.is_set() or aborted:
        return False

    # Liveness probe: silent NAT drops look like clean idle_check timeouts.
    client.noop()

    # If we woke for new mail OR after refresh, run a fetch (refresh also catches
    # anything missed while IDLE was renewing / silent gaps).
    on_wake()
    return True


def run_poll_cycle(
    *,
    stop_event: threading.Event,
    poll_interval_seconds: float,
    on_wake: Callable[[], None],
    first_delay_seconds: float = 30.0,
    last_run_at: float | None,
    reload_during_wait: Callable[[], tuple[float, bool] | None] | None = None,
    wait_chunk_seconds: float = 10.0,
) -> tuple[float, bool]:
    """
    Wait until next poll time (interruptible), then call on_wake.

    Every wait_chunk_seconds during the wait, optionally call reload_during_wait()
    which may return (new_poll_interval_seconds, switch_to_idle). Interval changes
    update next_run_at from last_run_at immediately; switch_to_idle=True returns
    early without fetching so the caller can reconnect for IDLE.

    Returns (last_run_at monotonic, switch_to_idle).
    """
    now = time.monotonic()
    interval = float(poll_interval_seconds)
    chunk = max(0.05, float(wait_chunk_seconds))
    if last_run_at is None:
        next_run_at = now + first_delay_seconds
    else:
        next_run_at = last_run_at + interval

    while not stop_event.is_set() and time.monotonic() < next_run_at:
        _wait_interruptible(stop_event, min(chunk, next_run_at - time.monotonic()))
        if stop_event.is_set():
            break
        if reload_during_wait is None:
            continue
        try:
            refreshed = reload_during_wait()
        except Exception:  # noqa: BLE001
            continue
        if refreshed is None:
            continue
        new_interval, switch_to_idle = refreshed
        if switch_to_idle:
            return (
                last_run_at if last_run_at is not None else time.monotonic(),
                True,
            )
        new_interval = max(1.0, float(new_interval))
        if abs(new_interval - interval) < 1e-9:
            continue
        interval = new_interval
        if last_run_at is None:
            # Still in first-delay window; don't extend the wait on interval change.
            next_run_at = min(next_run_at, time.monotonic() + interval)
        else:
            next_run_at = last_run_at + interval

    if stop_event.is_set():
        return (
            last_run_at if last_run_at is not None else time.monotonic(),
            False,
        )
    on_wake()
    return time.monotonic(), False


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
    # connection_identity for which we last confirmed CAPABILITY lacks IDLE; None = unknown.
    no_idle_conn_id: tuple[Any, ...] | None = None

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
        conn_id = connection_identity(settings)

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
                    if no_idle_conn_id != conn_id:
                        logger.info(
                            "IMAP server does not advertise IDLE; falling back to polling every %s minute(s)",
                            poll_interval_minutes,
                        )
                    no_idle_conn_id = conn_id
                    _close_client(client)
                    client = None
                else:
                    no_idle_conn_id = None
                    logger.info(
                        "IMAP IDLE enabled (refresh every %s min; poll fallback interval %s min)",
                        idle_refresh_minutes,
                        poll_interval_minutes,
                    )
                    idle_conn_id = conn_id
                    leave_idle = False

                    def _idle_should_abort() -> bool:
                        nonlocal idle_refresh_minutes, idle_refresh_seconds
                        nonlocal no_idle_conn_id, leave_idle
                        refreshed = load_imap_settings()
                        if not refreshed:
                            return False
                        idle_refresh_minutes = max(
                            1,
                            int(
                                refreshed.get(
                                    "idle_refresh_minutes", idle_refresh_minutes
                                )
                            ),
                        )
                        idle_refresh_seconds = float(idle_refresh_minutes * 60)
                        if connection_identity(refreshed) != idle_conn_id:
                            logger.info(
                                "IMAP connection settings changed; reconnecting IDLE watcher"
                            )
                            no_idle_conn_id = None
                            leave_idle = True
                            return True
                        if not bool(refreshed.get("use_idle", True)):
                            logger.info(
                                "IDLE disabled in config; switching to poll mode"
                            )
                            leave_idle = True
                            return True
                        return False

                    # Catch up on anything arrived while disconnected
                    on_fetch()
                    while not stop_event.is_set():
                        completed = run_idle_session(
                            client,
                            stop_event=stop_event,
                            idle_refresh_seconds=idle_refresh_seconds,
                            on_wake=on_fetch,
                            should_abort=_idle_should_abort,
                        )
                        if leave_idle:
                            break
                        if not completed:
                            # stop_event or abort without leave_idle flag
                            break
                        # Only clear backoff after a successful IDLE window; login/CAPABILITY
                        # alone must not reset it (IDLE can still fail immediately after).
                        backoff_attempt = 0
                    else:
                        break
                    _close_client(client)
                    client = None
                    continue

            # Polling fallback (no IDLE capability, use_idle false, or switched off)
            backoff_attempt = 0
            poll_conn_id = conn_id
            prev_use_idle = use_idle

            def _reload_poll_wait() -> tuple[float, bool] | None:
                """Re-read poll interval / IDLE toggle / connection id during the wait."""
                nonlocal poll_interval_minutes, poll_interval_seconds
                nonlocal no_idle_conn_id, poll_conn_id, prev_use_idle
                refreshed = load_imap_settings()
                if not refreshed:
                    return None
                poll_interval_minutes = max(
                    1,
                    int(refreshed.get("poll_interval_minutes", poll_interval_minutes)),
                )
                poll_interval_seconds = float(poll_interval_minutes * 60)
                new_conn_id = connection_identity(refreshed)
                new_use_idle = bool(refreshed.get("use_idle", True))
                if new_conn_id != poll_conn_id:
                    # New host/account/mailbox: drop cached "no IDLE" for the old server.
                    no_idle_conn_id = None
                    poll_conn_id = new_conn_id
                if new_use_idle and not prev_use_idle:
                    # Explicit re-enable: force a fresh CAPABILITY check.
                    no_idle_conn_id = None
                prev_use_idle = new_use_idle
                # Retry IDLE unless we already know *this* connection lacks it.
                switch_to_idle = new_use_idle and no_idle_conn_id != new_conn_id
                return poll_interval_seconds, switch_to_idle

            while not stop_event.is_set():
                poll_last_run_at, switch_to_idle = run_poll_cycle(
                    stop_event=stop_event,
                    poll_interval_seconds=poll_interval_seconds,
                    on_wake=on_fetch,
                    last_run_at=poll_last_run_at,
                    reload_during_wait=_reload_poll_wait,
                )
                if switch_to_idle:
                    logger.info("Attempting IMAP IDLE (reconnect)")
                    break

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
