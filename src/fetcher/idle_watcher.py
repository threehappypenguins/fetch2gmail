"""
Long-lived IMAP IDLE watcher with polling fallback.

- Uses IMAPClient for IDLE only; import still goes through run_once / imaplib.
- Inspects CAPABILITY for IDLE; falls back to poll_interval_minutes if missing.
- Renews IDLE with NOOP about every idle_refresh_minutes (RFC 2177 ~29 min rule).
- Enables SO_KEEPALIVE and reconnects with exponential backoff on errors.
"""

from __future__ import annotations

import datetime
import logging
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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

# Coordinate releasing the long-lived IDLE connection during imports so ISP
# accounts limited to 2 concurrent IMAP sessions can still fetch+delete.
_idle_gate = threading.Condition()
_idle_suspend_count = 0
_idle_connection_held = False


def _idle_suspend_requested() -> bool:
    with _idle_gate:
        return _idle_suspend_count > 0


def _set_idle_connection_held(held: bool) -> None:
    global _idle_connection_held
    with _idle_gate:
        _idle_connection_held = held
        _idle_gate.notify_all()


def _wait_until_idle_allowed(stop_event: threading.Event) -> bool:
    """Block while imports have paused IDLE. Return False if stop was requested."""
    while not stop_event.is_set():
        with _idle_gate:
            if _idle_suspend_count == 0:
                return True
            _idle_gate.wait(timeout=0.5)
    return False


@contextmanager
def pause_idle_connection(timeout_seconds: float = 60.0) -> Iterator[None]:
    """
    Drop (or wait for) the IDLE watcher connection for the duration of an import.

    Use around run_once / run_copy_all so manual UI fetches do not compete with IDLE
    for scarce IMAP connection slots.
    """
    global _idle_suspend_count
    with _idle_gate:
        _idle_suspend_count += 1
        _idle_gate.notify_all()
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while _idle_connection_held and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            _idle_gate.wait(timeout=min(0.5, max(0.05, remaining)))
    try:
        yield
    finally:
        with _idle_gate:
            _idle_suspend_count = max(0, _idle_suspend_count - 1)
            _idle_gate.notify_all()


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
    """Exponential backoff: base * 2^attempt, capped. attempt is 0-based.

    Doubling stops once the delay reaches cap so large attempt values cannot
    OverflowError (e.g. 2**1024) during a long outage.
    """
    if attempt < 0:
        attempt = 0
    if base <= 0:
        return float(cap)
    delay = float(base)
    for _ in range(attempt):
        if delay >= cap:
            return float(cap)
        delay *= 2.0
    return min(float(cap), delay)


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


def selected_uid_validity(client: IMAPClient, mailbox: str) -> int | None:
    """Return UIDVALIDITY for the selected mailbox, or None on failure."""
    try:
        status = client.folder_status(mailbox, ["UIDVALIDITY"])
    except Exception:  # noqa: BLE001
        logger.debug("folder_status UIDVALIDITY failed", exc_info=True)
        return None
    if not status:
        return None
    raw = status.get(b"UIDVALIDITY", status.get("UIDVALIDITY"))
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def invoke_on_fetch(on_fetch: Callable[[], Any]) -> bool:
    """
    Run the import callback and return True if it reported success.

    Accepts None (legacy void), bool, or a run_once-style dict with optional "error".
    Exceptions are caught and treated as failure so the watcher can back off.
    """
    try:
        result = on_fetch()
    except Exception:  # noqa: BLE001
        logger.exception("Fetch callback failed")
        return False
    if result is None:
        return True
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return not result.get("error")
    return True


def mailbox_has_unprocessed_uids(
    client: IMAPClient,
    *,
    last_processed_uid: int | None,
    since: str | datetime.date | None = None,
) -> bool:
    """
    True if SEARCH finds UIDs that run_once would consider new.

    Call after SELECT and before IDLE so mail that arrived during the previous
    import (or pause) is not missed until the next refresh window.

    Some servers treat UID ranges inclusively such that ``UID (last+1):*`` still
    returns ``last`` when it is the highest UID. Filter to UIDs strictly greater
    than the watermark so a fully processed mailbox can settle into IDLE.
    """
    criteria: list[Any] = []
    if last_processed_uid is not None:
        criteria.extend(["UID", f"{last_processed_uid + 1}:*"])
    else:
        criteria.append("ALL")
    if since not in (None, ""):
        if isinstance(since, str):
            since = datetime.date.fromisoformat(since)
        criteria.extend(["SINCE", since])
    try:
        uids = client.search(criteria)
    except Exception:  # noqa: BLE001
        logger.debug("pre-IDLE UID SEARCH failed", exc_info=True)
        return False
    if not uids:
        return False
    if last_processed_uid is None:
        return True
    return any(int(uid) > last_processed_uid for uid in uids)


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
    idle_refresh_seconds: float | Callable[[], float],
    on_wake: Callable[[], None],
    check_chunk_seconds: float = _IDLE_CHECK_CHUNK_SECONDS,
    should_abort: Callable[[], bool] | None = None,
) -> bool:
    """
    Run IDLE until refresh deadline, new-mail notification, stop, abort, or error.

    Calls on_wake when EXISTS/RECENT is seen or after a normal refresh.
    should_abort is checked between idle_check chunks (e.g. connection settings changed).
    idle_refresh_seconds may be a float or a callable so UI changes to the refresh
    interval apply during the current wait (not only on the next session).
    Returns False if aborted early (no on_wake); True after a normal wake/refresh fetch.
    Raises on connection errors.
    """
    def _refresh_limit() -> float:
        raw = idle_refresh_seconds() if callable(idle_refresh_seconds) else idle_refresh_seconds
        return max(1.0, float(raw))

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
            # Re-read limit each chunk so idle_refresh_minutes changes take effect now.
            remaining_refresh = _refresh_limit() - (time.monotonic() - idle_started)
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
    on_fetch: Callable[[], Any],
    get_last_processed_uid: Callable[[str, int], int | None] | None = None,
) -> None:
    """
    Main background loop: connect, IDLE or poll, reconnect with backoff.

    load_imap_settings() should return a dict with:
      host, port, username, password, mailbox, use_ssl,
      use_idle, idle_refresh_minutes, poll_interval_minutes, since_date
    or None if config is not ready.

    get_last_processed_uid(mailbox, uid_validity) supplies the watermark used to
    detect unprocessed UIDs after SELECT before entering IDLE.

    on_fetch may return None/True on success, False on failure, or a dict with
    an "error" key. Failed or no-progress catch-up fetches use exponential backoff
    so persistent import errors do not spin.
    """
    backoff_attempt = 0
    # Consecutive catch-up fetches that have not yet been followed by a clean IDLE.
    # Used to pace retries when pending UIDs remain after a failed/partial import.
    catchup_streak = 0
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
                    # Do not hold the IDLE connection during catch-up import (2-conn ISPs).
                    _close_client(client)
                    client = None
                    ok = invoke_on_fetch(on_fetch)
                    catchup_streak += 1
                    # Import retry pacing stays on catchup_streak; do not clear
                    # backoff_attempt here (login can succeed while IDLE keeps failing).
                    if not ok:
                        delay = next_backoff_seconds(catchup_streak - 1)
                        logger.warning(
                            "Initial catch-up fetch failed; waiting %.0fs before retry",
                            delay,
                        )
                        if _wait_interruptible(stop_event, delay):
                            break

                    while not stop_event.is_set():
                        if not _wait_until_idle_allowed(stop_event):
                            break
                        # Re-read settings in case host/mailbox changed during import.
                        try:
                            refreshed = load_imap_settings()
                        except Exception:  # noqa: BLE001
                            refreshed = None
                        if refreshed:
                            if connection_identity(refreshed) != conn_id:
                                no_idle_conn_id = None
                                break
                            if not bool(refreshed.get("use_idle", True)):
                                logger.info(
                                    "IDLE disabled in config; switching to poll mode"
                                )
                                break
                            idle_refresh_minutes = max(
                                1,
                                int(
                                    refreshed.get(
                                        "idle_refresh_minutes", idle_refresh_minutes
                                    )
                                ),
                            )
                            idle_refresh_seconds = float(idle_refresh_minutes * 60)
                            host = refreshed["host"]
                            port = int(refreshed.get("port", 993))
                            username = refreshed["username"]
                            password = refreshed["password"]
                            mailbox = refreshed.get("mailbox") or "INBOX"
                            use_ssl = bool(refreshed.get("use_ssl", True))
                            conn_id = connection_identity(refreshed)

                        client = connect_idle_client(
                            host=host,
                            port=port,
                            username=username,
                            password=password,
                            mailbox=mailbox,
                            use_ssl=use_ssl,
                        )
                        idle_conn_id = conn_id
                        leave_idle = False
                        _set_idle_connection_held(True)

                        def _idle_should_abort() -> bool:
                            nonlocal idle_refresh_minutes, idle_refresh_seconds
                            nonlocal no_idle_conn_id, leave_idle
                            if _idle_suspend_requested():
                                leave_idle = True
                                return True
                            refreshed_inner = load_imap_settings()
                            if not refreshed_inner:
                                return False
                            idle_refresh_minutes = max(
                                1,
                                int(
                                    refreshed_inner.get(
                                        "idle_refresh_minutes", idle_refresh_minutes
                                    )
                                ),
                            )
                            idle_refresh_seconds = float(idle_refresh_minutes * 60)
                            if connection_identity(refreshed_inner) != idle_conn_id:
                                logger.info(
                                    "IMAP connection settings changed; reconnecting IDLE watcher"
                                )
                                no_idle_conn_id = None
                                leave_idle = True
                                return True
                            if not bool(refreshed_inner.get("use_idle", True)):
                                logger.info(
                                    "IDLE disabled in config; switching to poll mode"
                                )
                                leave_idle = True
                                return True
                            return False

                        need_fetch = False
                        completed_idle = False
                        try:
                            # Mail may have arrived after the last import SEARCH but before
                            # this SELECT; that will not generate a later IDLE EXISTS. Check
                            # on the watcher connection, then drop it before importing.
                            since_date = None
                            if refreshed:
                                since_date = refreshed.get("since_date")
                            elif settings:
                                since_date = settings.get("since_date")
                            uid_validity = selected_uid_validity(client, mailbox)
                            if (
                                get_last_processed_uid is not None
                                and uid_validity is not None
                            ):
                                try:
                                    last_uid = get_last_processed_uid(
                                        mailbox, uid_validity
                                    )
                                except Exception:  # noqa: BLE001
                                    logger.debug(
                                        "get_last_processed_uid failed",
                                        exc_info=True,
                                    )
                                    last_uid = None
                                if mailbox_has_unprocessed_uids(
                                    client,
                                    last_processed_uid=last_uid,
                                    since=since_date,
                                ):
                                    logger.info(
                                        "IDLE: unprocessed UIDs after SELECT; fetching before IDLE"
                                    )
                                    need_fetch = True
                            if not need_fetch:
                                # Clean mailbox → allow IDLE; clear catch-up streak.
                                catchup_streak = 0
                                # on_wake is a no-op: fetch only after we drop this connection.
                                need_fetch = run_idle_session(
                                    client,
                                    stop_event=stop_event,
                                    idle_refresh_seconds=lambda: idle_refresh_seconds,
                                    on_wake=lambda: None,
                                    should_abort=_idle_should_abort,
                                )
                                # True means IDLE ran to EXISTS/refresh (not abort).
                                completed_idle = need_fetch
                        finally:
                            _close_client(client)
                            client = None
                            _set_idle_connection_held(False)

                        if stop_event.is_set():
                            break
                        if leave_idle and not need_fetch:
                            # Suspend, settings change, or IDLE disabled without a mailbox wake.
                            if _idle_suspend_requested():
                                # Wait for the import that paused us, then resume IDLE.
                                if not _wait_until_idle_allowed(stop_event):
                                    break
                                continue
                            break
                        if need_fetch:
                            if completed_idle:
                                # Only clear reconnect backoff after a successful IDLE window.
                                backoff_attempt = 0
                            # Pace import retries separately when pending UIDs remain
                            # (e.g. Gmail auth failure) or this is a repeat after failure.
                            if catchup_streak > 0:
                                delay = next_backoff_seconds(catchup_streak - 1)
                                logger.warning(
                                    "Catch-up fetch retry delayed %.0fs "
                                    "(pending mail remains or prior fetch failed)",
                                    delay,
                                )
                                if _wait_interruptible(stop_event, delay):
                                    break
                            invoke_on_fetch(on_fetch)
                            catchup_streak += 1
                            continue
                        # aborted without fetch (e.g. stop during idle_done path)
                        break
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
