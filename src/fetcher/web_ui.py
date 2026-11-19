"""
Lightweight FastAPI web UI: localhost only by default.
- Auth: UI password only (.ui_auth). When absent, allow access so user can set it (token must exist).
- Get token.json via CLI: fetch2gmail auth (not in the UI).
- credentials.json required first; then run fetch2gmail auth for token; then create config in the UI or use the dashboard.
"""

import base64
import json
import logging
import os
import threading
import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response as RawResponse
from pydantic import BaseModel

import imaplib

from .config import (
    DEFAULT_IMAP_ACCOUNT_ID,
    get_config_path,
    get_gmail_accounts,
    get_gmail_destinations_for_imap,
    get_imap_accounts,
    load_config,
    password_env_key_for_account_id,
)
from .env_file import set_encrypted_env
from .gmail_client import get_gmail_service
from .idle_watcher import pause_idle_connection, watcher_loop
from .imap_client import get_uid_validity
from .log_buffer import get_recent_logs, install_log_buffer
from .run import run_once, run_copy_all, setup_logging
from .state import StateStore
from .ui_auth import create_ui_auth, load_ui_auth, verify_ui_auth

logger = logging.getLogger(__name__)

# Per-IMAP-account fetch locks (IDLE wake, poll fallback, manual fetch, copy-all).
_fetch_locks_guard = threading.Lock()
_fetch_locks: dict[str, threading.Lock] = {}

# Watcher manager: one thread per IMAP account id.
_watcher_manager_lock = threading.Lock()
_watcher_stop_events: dict[str, threading.Event] = {}
_watcher_threads: dict[str, threading.Thread] = {}
_watcher_supervisor_stop: threading.Event | None = None
_watcher_supervisor_thread: threading.Thread | None = None


def _fetch_lock_for(account_id: str) -> threading.Lock:
    aid = (account_id or DEFAULT_IMAP_ACCOUNT_ID).strip() or DEFAULT_IMAP_ACCOUNT_ID
    with _fetch_locks_guard:
        if aid not in _fetch_locks:
            _fetch_locks[aid] = threading.Lock()
        return _fetch_locks[aid]


def _load_imap_settings_for_account(account_id: str) -> dict[str, Any] | None:
    """Return IMAP + poll/IDLE settings for one account, or None if not ready."""
    if not _config_exists():
        return None
    path = _get_config_path()
    cfg = load_config(path, resolve_password=True)
    try:
        accounts = get_imap_accounts(cfg)
    except ValueError:
        return None
    imap = None
    for acct in accounts:
        if str(acct.get("id")) == account_id:
            imap = acct
            break
    if not imap:
        return None
    if not imap.get("host") or not imap.get("username") or not imap.get("password"):
        return None
    since_date = imap.get("since_date")
    if since_date == "":
        since_date = None
    return {
        "account_id": account_id,
        "host": imap["host"],
        "port": int(imap.get("port", 993)),
        "username": imap["username"],
        "password": imap["password"],
        "mailbox": imap.get("mailbox") or "INBOX",
        "use_ssl": bool(imap.get("use_ssl", True)),
        "use_idle": bool(imap.get("use_idle", True)),
        "idle_refresh_minutes": max(1, int(imap.get("idle_refresh_minutes", 25))),
        "poll_interval_minutes": max(1, int(cfg.get("poll_interval_minutes", 5))),
        "since_date": since_date,
        "state_db_path": str((cfg.get("state") or {}).get("db_path", "state.db")),
        "config_dir": str(path.resolve().parent),
    }


def _watcher_last_processed_uid_for(
    account_id: str, mailbox: str, uid_validity: int
) -> int | None:
    """Read last_processed_uid from state.db for pre-IDLE pending-mail checks."""
    if not _config_exists():
        return None
    path = _get_config_path()
    cfg = load_config(path, resolve_password=False)
    config_dir = path.resolve().parent
    raw_db = (cfg.get("state") or {}).get("db_path", "state.db")
    db_path = Path(raw_db)
    if not db_path.is_absolute():
        db_path = config_dir / db_path
    state = StateStore(str(db_path))
    try:
        state.connect()
        return state.get_last_processed_uid(
            mailbox, uid_validity, account_id=account_id
        )
    finally:
        state.close()


def _run_once_locked(
    config_path: str,
    dry_run: bool = False,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Run one fetch cycle under the per-account fetch lock (IDLE connection paused)."""
    aid = account_id or DEFAULT_IMAP_ACCOUNT_ID
    with _fetch_lock_for(aid):
        with pause_idle_connection(account_id=aid):
            return run_once(
                config_path=config_path, dry_run=dry_run, account_id=account_id
            )


def _watcher_on_fetch_for(account_id: str) -> bool:
    """Callback for IDLE/poll wakes for one account; return True on success."""
    try:
        path = _get_config_path()
        setup_logging()
        install_log_buffer()
        result = _run_once_locked(
            config_path=str(path), dry_run=False, account_id=account_id
        )
        if result.get("error"):
            logger.warning(
                "Background fetch error [%s]: %s", account_id, result["error"]
            )
            return False
        logger.info(
            "Background fetch [%s]: imported=%s skipped=%s deleted=%s",
            account_id,
            result.get("imported", 0),
            result.get("skipped_duplicate", 0),
            result.get("deleted", 0),
        )
        return True
    except ValueError as e:
        logger.warning("Background fetch skipped [%s]: %s", account_id, e)
        return False
    except Exception as e:
        logger.exception("Background fetch failed [%s]: %s", account_id, e)
        return False


def _watcher_thread_main(stop_event: threading.Event, account_id: str) -> None:
    """Background thread: IMAP IDLE with polling fallback for one account."""
    watcher_loop(
        stop_event,
        load_imap_settings=lambda: _load_imap_settings_for_account(account_id),
        on_fetch=lambda: _watcher_on_fetch_for(account_id),
        get_last_processed_uid=lambda mailbox, uid_validity: _watcher_last_processed_uid_for(
            account_id, mailbox, uid_validity
        ),
    )


def _desired_watcher_account_ids() -> list[str]:
    if not _config_exists():
        return []
    try:
        cfg = load_config(_get_config_path(), resolve_password=False)
        return [
            str(a.get("id") or DEFAULT_IMAP_ACCOUNT_ID) for a in get_imap_accounts(cfg)
        ]
    except Exception:
        return []


def _sync_watcher_threads() -> None:
    """Start/stop watcher threads so they match configured IMAP account ids."""
    desired = set(_desired_watcher_account_ids())
    with _watcher_manager_lock:
        # Stop removed accounts
        for aid in list(_watcher_threads.keys()):
            if aid in desired:
                continue
            stop_ev = _watcher_stop_events.pop(aid, None)
            thread = _watcher_threads.pop(aid, None)
            if stop_ev:
                stop_ev.set()
            if thread and thread.is_alive():
                thread.join(timeout=5)
            logger.info("Stopped IMAP watcher for account %s", aid)
        # Start new accounts
        for aid in desired:
            existing = _watcher_threads.get(aid)
            if existing and existing.is_alive():
                continue
            stop_ev = threading.Event()
            thread = threading.Thread(
                target=_watcher_thread_main,
                args=(stop_ev, aid),
                daemon=True,
                name=f"imap-watcher-{aid}",
            )
            _watcher_stop_events[aid] = stop_ev
            _watcher_threads[aid] = thread
            thread.start()
            logger.info("Started IMAP watcher for account %s", aid)


def _watcher_supervisor_main(stop_event: threading.Event) -> None:
    """Periodically reconcile watcher threads with config (add/remove accounts)."""
    while not stop_event.is_set():
        try:
            _sync_watcher_threads()
        except Exception:
            logger.exception("Watcher supervisor sync failed")
        stop_event.wait(timeout=15.0)


def _stop_all_watchers() -> None:
    with _watcher_manager_lock:
        for stop_ev in _watcher_stop_events.values():
            stop_ev.set()
        threads = list(_watcher_threads.values())
        _watcher_stop_events.clear()
        _watcher_threads.clear()
    for thread in threads:
        thread.join(timeout=10)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _watcher_supervisor_stop, _watcher_supervisor_thread
    _watcher_supervisor_stop = threading.Event()
    _sync_watcher_threads()
    _watcher_supervisor_thread = threading.Thread(
        target=_watcher_supervisor_main,
        args=(_watcher_supervisor_stop,),
        daemon=True,
        name="imap-watcher-supervisor",
    )
    _watcher_supervisor_thread.start()
    logger.info(
        "Background IMAP watchers started (one IDLE/poll thread per IMAP account)"
    )
    yield
    if _watcher_supervisor_stop:
        _watcher_supervisor_stop.set()
    if _watcher_supervisor_thread:
        _watcher_supervisor_thread.join(timeout=5)
    _stop_all_watchers()


app = FastAPI(title="Fetch2Gmail", description="IMAP to Gmail import service", lifespan=_lifespan)


def _config_dir_for_middleware() -> Path | None:
    """Config directory for UI auth file; None if not determinable (e.g. no config path yet)."""
    try:
        return get_config_path().resolve().parent
    except Exception:
        return None


_NO_CACHE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
# Minimal blank HTML for 401 so the browser shows nothing behind the Basic Auth dialog (no cached page).
_401_BLANK_BODY = b"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Authentication required</title></head><body></body></html>"


@app.middleware("http")
async def _optional_basic_auth(request: Request, call_next):
    """If .ui_auth exists in config dir (hashed password), require HTTP Basic Auth for all requests."""
    from starlette.responses import Response

    config_dir = _config_dir_for_middleware()
    ui_creds = load_ui_auth(config_dir) if config_dir else None
    if not ui_creds:
        return await call_next(request)
    auth = request.headers.get("Authorization")
    if auth and auth.startswith("Basic "):
        try:
            raw = base64.b64decode(auth[6:].strip()).decode("utf-8")
            user, _, password = raw.partition(":")
            if verify_ui_auth(config_dir, user, password):
                response = await call_next(request)
                # Prevent caching so after closing the browser we don't show stale content behind the auth dialog
                for k, v in _NO_CACHE_HEADERS.items():
                    response.headers[k] = v
                return response
        except Exception:
            pass
    return Response(
        status_code=401,
        headers={
            "WWW-Authenticate": "Basic realm=\"Fetch2Gmail\"",
            **_NO_CACHE_HEADERS,
            "Content-Type": "text/html; charset=utf-8",
        },
        content=_401_BLANK_BODY,
    )


def _get_config_path() -> Path:
    return get_config_path()


def _config_dir() -> Path:
    return _get_config_path().resolve().parent


def _config_dir_safe() -> Path | None:
    """Config directory for auth; None if no config path yet."""
    try:
        return _config_dir()
    except Exception:
        return None


def _config_exists() -> bool:
    return _get_config_path().exists()


def _token_exists() -> bool:
    """True when config exists and every Gmail account has its token file."""
    cfg_dir = _config_dir_safe()
    if not cfg_dir or not _config_exists():
        return False
    try:
        cfg = load_config(_get_config_path(), resolve_password=False)
        accounts = get_gmail_accounts(cfg)
        if not accounts:
            return False
        for acct in accounts:
            token_path = Path(acct.get("token_path", "token.json"))
            if not token_path.is_absolute():
                token_path = cfg_dir / token_path
            if not token_path.exists():
                return False
        return True
    except Exception:
        return False


def _require_auth(request: Request) -> bool:
    """True when allowed: either .ui_auth exists (Basic Auth already passed in middleware) or no .ui_auth (allow to set password or use app)."""
    cfg_dir = _config_dir_safe()
    if load_ui_auth(cfg_dir):
        return True  # Passed Basic Auth in middleware
    return True  # No .ui_auth: allow (set password or use dashboard)


def _token_available() -> bool:
    """True when token.json exists so Gmail operations can run. Uses default path when no config yet."""
    cfg_dir = _config_dir_safe()
    if not cfg_dir:
        return False
    if _config_exists():
        return _token_exists()
    return (cfg_dir / "token.json").exists()


def _can_set_ui_password() -> bool:
    """True when no .ui_auth yet. Wizard is only shown when token exists (see show_set_password_wizard)."""
    cfg_dir = _config_dir_safe()
    return bool(cfg_dir and not load_ui_auth(cfg_dir))


# Sanitized config for UI (no passwords)
class ImapConfigSafe(BaseModel):
    host: str
    port: int
    username: str
    mailbox: str
    use_ssl: bool
    delete_after_import: bool
    since_date: str | None = None
    use_idle: bool = True
    idle_refresh_minutes: int = 25
    never_mark_spam: bool = True


class ImapAccountSafe(BaseModel):
    id: str
    host: str
    port: int = 993
    username: str
    mailbox: str = "INBOX"
    use_ssl: bool = True
    delete_after_import: bool = True
    since_date: str | None = None
    use_idle: bool = True
    idle_refresh_minutes: int = 25
    never_mark_spam: bool = True
    password_env: str = "IMAP_PASSWORD"
    password_set: bool = False
    gmail_token_paths: list[str] = []


class GmailConfigSafe(BaseModel):
    use_label: bool
    label: str
    credentials_path: str
    token_path: str


class GmailAccountSafe(BaseModel):
    use_label: bool
    label: str
    credentials_path: str
    token_path: str


class UIConfigSafe(BaseModel):
    host: str
    port: int


class ConfigResponse(BaseModel):
    imap: ImapConfigSafe  # first/legacy account for back-compat
    imap_accounts: list[ImapAccountSafe] = []
    gmail: GmailConfigSafe
    gmail_accounts: list[GmailAccountSafe] = []
    ui: UIConfigSafe
    poll_interval_minutes: int
    state_db_path: str
    gmail_connected: bool
    imap_password_set: bool
    config_exists: bool = True  # False when config.json not created yet (show setup wizard)
    show_set_password_wizard: bool = False  # True when no .ui_auth → show wizard (must set password before using app)
    ui_password_mode: bool = False  # True when .ui_auth exists → show "Change password" on dashboard

@app.get("/static/app.js", response_model=None)
def static_app_js():
    """Serve app script as plain JS to avoid HTML-embedded script encoding issues."""
    return RawResponse(content=_APP_JS.strip(), media_type="application/javascript")


@app.get("/api/setup/status")
def api_setup_status() -> dict[str, bool]:
    """Whether credentials, token, and config exist (no auth). UI uses this to show the right step."""
    cfg_dir = _config_dir_safe()
    cred_exists = bool(cfg_dir and (cfg_dir / "credentials.json").exists())
    return {
        "credentials_exist": cred_exists,
        "config_exists": _config_exists(),
        "token_available": _token_available(),
    }


class SetUiPasswordBody(BaseModel):
    """Body for creating .ui_auth from the UI (no .ui_auth yet)."""
    username: str
    password: str
    password_confirm: str


class ChangeUiPasswordBody(BaseModel):
    """Body for changing .ui_auth (when already set)."""
    current_password: str
    new_username: str = ""  # empty = keep existing username
    new_password: str
    new_password_confirm: str


@app.post("/api/setup/ui-password")
def api_setup_ui_password(body: SetUiPasswordBody) -> dict[str, str]:
    """Create .ui_auth when token exists and no .ui_auth yet (wizard shown)."""
    if not _can_set_ui_password():
        raise HTTPException(status_code=400, detail="UI password already set")
    if not _token_available():
        raise HTTPException(
            status_code=400,
            detail="Get token.json first (run fetch2gmail auth from the app folder), then set the UI password.",
        )
    username = (body.username or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")
    if not body.password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")
    if body.password != body.password_confirm:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    cfg_dir = _config_dir()
    create_ui_auth(cfg_dir, username, body.password)
    return {"status": "ok", "message": "UI password set. Reload the page; you will be prompted for this username and password."}


@app.put("/api/setup/ui-password")
def api_change_ui_password(request: Request, body: ChangeUiPasswordBody) -> dict[str, str]:
    """Change .ui_auth (when already set). Requires current password."""
    cfg_dir = _config_dir_safe()
    if not cfg_dir or not load_ui_auth(cfg_dir):
        raise HTTPException(status_code=400, detail="UI password is not set")
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    stored = load_ui_auth(cfg_dir)
    if not stored or not verify_ui_auth(cfg_dir, stored[0], body.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    username = (body.new_username or "").strip() or stored[0]
    if not body.new_password:
        raise HTTPException(status_code=400, detail="New password cannot be empty")
    if body.new_password != body.new_password_confirm:
        raise HTTPException(status_code=400, detail="New passwords do not match")
    create_ui_auth(Path(cfg_dir), username, body.new_password)
    return {"status": "ok", "message": "Password updated. Use the new credentials on next login."}


@app.get("/login", response_class=HTMLResponse, response_model=None)
def login_page() -> RedirectResponse:
    """Redirect to index (no OAuth in UI)."""
    return RedirectResponse(url="/", status_code=302)


@app.get("/", response_class=HTMLResponse, response_model=None)
def index(request: Request) -> str:
    """Always show the app page; frontend shows landing, credentials message, or dashboard."""
    return _HTML_PAGE


def _gmail_connected() -> bool:
    """True when all configured Gmail accounts have token files (and can be used for fetch)."""
    return _token_exists()


def _gmail_email() -> str | None:
    """Return the Gmail address for the first account, or None if not connected or error."""
    if not _gmail_connected():
        return None
    try:
        cfg = load_config(_get_config_path(), resolve_password=False)
        accounts = get_gmail_accounts(cfg)
        if not accounts:
            return None
        gmail = accounts[0]
        cred_path = Path(gmail.get("credentials_path", "credentials.json"))
        token_path = Path(gmail.get("token_path", "token.json"))
        if not cred_path.is_absolute():
            cred_path = _config_dir() / cred_path
        if not token_path.is_absolute():
            token_path = _config_dir() / token_path
        service = get_gmail_service(cred_path, token_path)
        profile = service.users().getProfile(userId="me").execute()
        return profile.get("emailAddress")
    except Exception:
        return None


def _imap_account_password_set(imap: dict[str, Any]) -> bool:
    if imap.get("password") not in (None, ""):
        return True
    acct_id = str(imap.get("id") or DEFAULT_IMAP_ACCOUNT_ID)
    key = imap.get("password_env") or (
        "IMAP_PASSWORD"
        if acct_id == DEFAULT_IMAP_ACCOUNT_ID
        else password_env_key_for_account_id(acct_id)
    )
    return bool(os.environ.get(key) or os.environ.get(f"{key}_ENC"))


def _imap_password_set() -> bool:
    path = _get_config_path()
    if not path.exists():
        return False
    try:
        cfg = load_config(path, resolve_password=False)
        accounts = get_imap_accounts(cfg)
        if not accounts:
            return False
        return all(_imap_account_password_set(a) for a in accounts)
    except Exception:
        return False


def _imap_account_to_safe(acct: dict[str, Any]) -> ImapAccountSafe:
    acct_id = str(acct.get("id") or DEFAULT_IMAP_ACCOUNT_ID)
    password_env = acct.get("password_env") or (
        "IMAP_PASSWORD"
        if acct_id == DEFAULT_IMAP_ACCOUNT_ID
        else password_env_key_for_account_id(acct_id)
    )
    paths = acct.get("gmail_token_paths") or []
    if not isinstance(paths, list):
        paths = []
    return ImapAccountSafe(
        id=acct_id,
        host=acct.get("host", ""),
        port=int(acct.get("port", 993)),
        username=acct.get("username", ""),
        mailbox=acct.get("mailbox", "INBOX"),
        use_ssl=bool(acct.get("use_ssl", True)),
        delete_after_import=bool(acct.get("delete_after_import", True)),
        since_date=acct.get("since_date"),
        use_idle=bool(acct.get("use_idle", True)),
        idle_refresh_minutes=max(1, int(acct.get("idle_refresh_minutes", 25))),
        never_mark_spam=bool(acct.get("never_mark_spam", True)),
        password_env=str(password_env),
        password_set=_imap_account_password_set(acct),
        gmail_token_paths=[str(p) for p in paths if str(p).strip()],
    )


def _verify_imap_credentials(
    host: str,
    port: int,
    username: str,
    password: str,
    mailbox: str = "INBOX",
    use_ssl: bool = True,
) -> None:
    """Verify IMAP login; raise ValueError with a user-friendly message on failure."""
    if not (host and username and password):
        raise ValueError("IMAP host, username, and password are required to verify.")
    try:
        get_uid_validity(
            host=host,
            port=port,
            username=username,
            password=password,
            mailbox=mailbox,
            use_ssl=use_ssl,
        )
    except imaplib.IMAP4.error as e:
        msg = str(e).strip()
        if "AUTHENTICATIONFAILED" in msg.upper() or "LOGIN" in msg.upper() or "invalid" in msg.lower():
            raise ValueError("IMAP login failed: incorrect password or invalid credentials.") from e
        raise ValueError(f"IMAP connection failed: {msg}") from e
    except OSError as e:
        raise ValueError(f"IMAP connection failed: {e}") from e


@app.get("/api/gmail/email")
def api_gmail_email(request: Request) -> dict[str, str | None]:
    """Return the Gmail address currently connected (for UI warning when reconnecting)."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"email": _gmail_email()}


def _default_config_response() -> ConfigResponse:
    """Return empty config when config.json does not exist yet (avoids 404, show setup wizard)."""
    empty_imap = ImapConfigSafe(
        host="",
        port=993,
        username="",
        mailbox="INBOX",
        use_ssl=True,
        delete_after_import=True,
        since_date=None,
    )
    return ConfigResponse(
        imap=empty_imap,
        imap_accounts=[
            ImapAccountSafe(
                id=DEFAULT_IMAP_ACCOUNT_ID,
                host="",
                port=993,
                username="",
                mailbox="INBOX",
                use_ssl=True,
                delete_after_import=True,
                password_env="IMAP_PASSWORD",
                password_set=False,
                gmail_token_paths=[],
            )
        ],
        gmail=GmailConfigSafe(
            use_label=False,
            label="ISP Mail",
            credentials_path="credentials.json",
            token_path="token.json",
        ),
        gmail_accounts=[
            GmailAccountSafe(
                use_label=False,
                label="ISP Mail",
                credentials_path="credentials.json",
                token_path="token.json",
            )
        ],
        ui=UIConfigSafe(host="127.0.0.1", port=8765),
        poll_interval_minutes=5,
        state_db_path="state.db",
        gmail_connected=_gmail_connected(),
        imap_password_set=False,
        config_exists=False,
        show_set_password_wizard=_can_set_ui_password() and _token_available(),
        ui_password_mode=False,
    )


@app.get("/api/config", response_model=ConfigResponse)
def api_config(request: Request) -> ConfigResponse:
    """Return non-sensitive config for display/editing (no password resolution)."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    path = _get_config_path()
    if not path.exists():
        return _default_config_response()
    try:
        cfg = load_config(path, resolve_password=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        imap_accounts_raw = get_imap_accounts(cfg)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    imap_accounts_safe = [_imap_account_to_safe(a) for a in imap_accounts_raw]
    first = imap_accounts_safe[0] if imap_accounts_safe else None
    gmail_accounts = get_gmail_accounts(cfg)
    gmail = gmail_accounts[0] if gmail_accounts else {}
    ui = cfg.get("ui") or {}
    state = cfg.get("state") or {}
    cred_path = gmail.get("credentials_path", "credentials.json")
    token_path = gmail.get("token_path", "token.json")
    gmail_accounts_safe: list[GmailAccountSafe] = []
    for acct in gmail_accounts:
        gmail_accounts_safe.append(
            GmailAccountSafe(
                use_label=acct.get("use_label") if "use_label" in acct else bool((acct.get("label") or "").strip()),
                label=acct.get("label", "ISP Mail"),
                credentials_path=acct.get("credentials_path", "credentials.json"),
                token_path=acct.get("token_path", "token.json"),
            )
        )
    return ConfigResponse(
        imap=ImapConfigSafe(
            host=first.host if first else "",
            port=first.port if first else 993,
            username=first.username if first else "",
            mailbox=first.mailbox if first else "INBOX",
            use_ssl=first.use_ssl if first else True,
            delete_after_import=first.delete_after_import if first else True,
            since_date=first.since_date if first else None,
            use_idle=first.use_idle if first else True,
            idle_refresh_minutes=first.idle_refresh_minutes if first else 25,
            never_mark_spam=first.never_mark_spam if first else True,
        ),
        imap_accounts=imap_accounts_safe,
        gmail=GmailConfigSafe(
            use_label=gmail.get("use_label") if "use_label" in gmail else bool((gmail.get("label") or "").strip()),
            label=gmail.get("label", "ISP Mail"),
            credentials_path=cred_path,
            token_path=token_path,
        ),
        gmail_accounts=gmail_accounts_safe,
        ui=UIConfigSafe(
            host=ui.get("host", "127.0.0.1"),
            port=int(ui.get("port", 8765)),
        ),
        poll_interval_minutes=int(cfg.get("poll_interval_minutes", 5)),
        state_db_path=str(state.get("db_path", "state.db")),
        gmail_connected=_gmail_connected(),
        imap_password_set=_imap_password_set(),
        config_exists=True,
        show_set_password_wizard=_can_set_ui_password() and _token_available(),
        ui_password_mode=bool(load_ui_auth(_config_dir_safe())),
    )


class ImapAccountUpdate(BaseModel):
    id: str
    host: str
    port: int = 993
    username: str
    mailbox: str = "INBOX"
    use_ssl: bool = True
    delete_after_import: bool = True
    since_date: str | None = None
    use_idle: bool = True
    idle_refresh_minutes: int = 25
    never_mark_spam: bool = True
    password: str | None = None  # stored in .env when provided
    gmail_token_paths: list[str] = []


class ConfigUpdate(BaseModel):
    # Preferred: full IMAP accounts list
    imap_accounts: list[ImapAccountUpdate] | None = None
    # Legacy single-IMAP fields (applied to first account when imap_accounts omitted)
    imap_host: str | None = None
    imap_port: int | None = None
    imap_username: str | None = None
    imap_mailbox: str | None = None
    imap_use_ssl: bool | None = None
    imap_since_date: str | None = None
    imap_password: str | None = None  # stored in .env, not in config
    delete_after_import: bool | None = None
    never_mark_spam: bool | None = None
    imap_use_idle: bool | None = None
    imap_idle_refresh_minutes: int | None = None
    gmail_use_label: bool | None = None
    gmail_label: str | None = None
    gmail_accounts: list[GmailAccountSafe] | None = None
    poll_interval_minutes: int | None = None
    state_db_path: str | None = None


class SetupBody(BaseModel):
    """Initial setup: create config.json and .env."""
    imap_host: str
    imap_port: int = 993
    imap_username: str
    imap_password: str
    imap_mailbox: str = "INBOX"
    imap_use_ssl: bool = True
    imap_since_date: str | None = None
    delete_after_import: bool = True
    gmail_use_label: bool = False
    gmail_label: str = "ISP Mail"
    credentials_path: str = "credentials.json"
    token_path: str = "token.json"
    state_db_path: str = "state.db"


@app.post("/api/setup")
def api_setup(request: Request, body: SetupBody) -> dict[str, str]:
    """Create initial config.json and .env (when no config exists)."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    path = _get_config_path()
    if path.exists():
        raise HTTPException(status_code=400, detail="Config already exists")
    if body.imap_since_date:
        try:
            datetime.date.fromisoformat(body.imap_since_date)
        except Exception:
            raise HTTPException(status_code=400, detail="since_date must be YYYY-MM-DD")
    try:
        _verify_imap_credentials(
            host=body.imap_host,
            port=body.imap_port,
            username=body.imap_username,
            password=body.imap_password,
            mailbox=body.imap_mailbox,
            use_ssl=body.imap_use_ssl,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "imap_accounts": [
            {
                "id": DEFAULT_IMAP_ACCOUNT_ID,
                "host": body.imap_host,
                "port": body.imap_port,
                "username": body.imap_username,
                "password_env": "IMAP_PASSWORD",
                "mailbox": body.imap_mailbox,
                "since_date": body.imap_since_date,
                "use_ssl": body.imap_use_ssl,
                "delete_after_import": body.delete_after_import,
                "never_mark_spam": True,
                "use_idle": True,
                "idle_refresh_minutes": 25,
                "gmail_token_paths": [],
            }
        ],
        "gmail_accounts": [
            {
                "use_label": body.gmail_use_label,
                "label": body.gmail_label,
                "credentials_path": body.credentials_path,
                "token_path": body.token_path,
            }
        ],
        "state": {"db_path": body.state_db_path},
        "ui": {"host": "127.0.0.1", "port": 8765},
        "poll_interval_minutes": 5,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    set_encrypted_env(_config_dir(), "IMAP_PASSWORD", body.imap_password)
    from dotenv import load_dotenv

    load_dotenv(_config_dir() / ".env")
    _sync_watcher_threads()
    return {"status": "ok", "message": "Config created. Connect Gmail next."}


def _validate_imap_accounts_routing(
    imap_accounts: list[dict[str, Any]], gmail_accounts: list[dict[str, Any]]
) -> None:
    """Raise HTTPException if any IMAP gmail_token_paths are invalid."""
    cfg = {"gmail_accounts": gmail_accounts, "imap_accounts": imap_accounts}
    for acct in imap_accounts:
        try:
            get_gmail_destinations_for_imap(cfg, acct)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e


@app.put("/api/config")
def api_config_update(request: Request, update: ConfigUpdate) -> dict[str, str]:
    """Update config file. IMAP passwords (when provided) are verified then stored in .env."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    path = _get_config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="config.json not found")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

    from dotenv import load_dotenv

    # Gmail accounts first so routing validation can use the new list.
    if update.gmail_accounts is not None:
        cfg["gmail_accounts"] = [
            {
                "use_label": a.use_label,
                "label": a.label,
                "credentials_path": a.credentials_path,
                "token_path": a.token_path,
            }
            for a in update.gmail_accounts
        ]
        cfg.pop("gmail", None)
    else:
        if update.gmail_use_label is not None or update.gmail_label is not None:
            existing = get_gmail_accounts(cfg)
            if not existing:
                existing = [{"credentials_path": "credentials.json", "token_path": "token.json"}]
            first = dict(existing[0])
            if update.gmail_use_label is not None:
                first["use_label"] = update.gmail_use_label
            if update.gmail_label is not None:
                first["label"] = update.gmail_label
            rest = existing[1:]
            cfg["gmail_accounts"] = [first, *rest]
            cfg.pop("gmail", None)

    gmail_accounts = get_gmail_accounts(cfg)

    if update.imap_accounts is not None:
        if not update.imap_accounts:
            raise HTTPException(status_code=400, detail="At least one IMAP account is required")
        seen_ids: set[str] = set()
        new_accounts: list[dict[str, Any]] = []
        for acct in update.imap_accounts:
            acct_id = (acct.id or "").strip()
            if not acct_id:
                raise HTTPException(status_code=400, detail="IMAP account id cannot be empty")
            if acct_id in seen_ids:
                raise HTTPException(status_code=400, detail=f"Duplicate IMAP account id: {acct_id}")
            seen_ids.add(acct_id)
            if acct.since_date:
                try:
                    datetime.date.fromisoformat(acct.since_date)
                except Exception:
                    raise HTTPException(
                        status_code=400, detail=f"since_date for {acct_id} must be YYYY-MM-DD"
                    )
            password_env = (
                "IMAP_PASSWORD"
                if acct_id == DEFAULT_IMAP_ACCOUNT_ID
                else password_env_key_for_account_id(acct_id)
            )
            entry: dict[str, Any] = {
                "id": acct_id,
                "host": acct.host,
                "port": int(acct.port),
                "username": acct.username,
                "password_env": password_env,
                "mailbox": acct.mailbox or "INBOX",
                "use_ssl": bool(acct.use_ssl),
                "delete_after_import": bool(acct.delete_after_import),
                "never_mark_spam": bool(acct.never_mark_spam),
                "use_idle": bool(acct.use_idle),
                "idle_refresh_minutes": max(1, int(acct.idle_refresh_minutes)),
                "since_date": acct.since_date or None,
                "gmail_token_paths": [p for p in (acct.gmail_token_paths or []) if str(p).strip()],
            }
            if acct.password:
                try:
                    _verify_imap_credentials(
                        host=entry["host"],
                        port=entry["port"],
                        username=entry["username"],
                        password=acct.password,
                        mailbox=entry["mailbox"],
                        use_ssl=entry["use_ssl"],
                    )
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"[{acct_id}] {e}") from e
                set_encrypted_env(_config_dir(), password_env, acct.password)
            new_accounts.append(entry)
        _validate_imap_accounts_routing(new_accounts, gmail_accounts)
        cfg["imap_accounts"] = new_accounts
        cfg.pop("imap", None)
        load_dotenv(_config_dir() / ".env")
    else:
        # Legacy single-IMAP field updates → mutate first account / legacy imap.
        try:
            accounts = get_imap_accounts(cfg)
        except ValueError:
            accounts = []
        if not accounts:
            accounts = [
                {
                    "id": DEFAULT_IMAP_ACCOUNT_ID,
                    "password_env": "IMAP_PASSWORD",
                    "mailbox": "INBOX",
                    "use_ssl": True,
                    "delete_after_import": True,
                    "never_mark_spam": True,
                    "use_idle": True,
                    "idle_refresh_minutes": 25,
                    "gmail_token_paths": [],
                }
            ]
        first = dict(accounts[0])
        first.setdefault("id", DEFAULT_IMAP_ACCOUNT_ID)
        if update.imap_password is not None:
            host = update.imap_host if update.imap_host is not None else first.get("host", "")
            port = int(update.imap_port if update.imap_port is not None else first.get("port", 993))
            username = (
                update.imap_username
                if update.imap_username is not None
                else first.get("username", "")
            )
            mailbox = (
                update.imap_mailbox
                if update.imap_mailbox is not None
                else first.get("mailbox", "INBOX")
            )
            use_ssl = (
                update.imap_use_ssl
                if update.imap_use_ssl is not None
                else first.get("use_ssl", True)
            )
            try:
                _verify_imap_credentials(
                    host=host,
                    port=port,
                    username=username,
                    password=update.imap_password,
                    mailbox=mailbox,
                    use_ssl=use_ssl,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            password_env = first.get("password_env") or "IMAP_PASSWORD"
            first["password_env"] = password_env
            set_encrypted_env(_config_dir(), password_env, update.imap_password)
            load_dotenv(_config_dir() / ".env")
        if update.imap_host is not None:
            first["host"] = update.imap_host
        if update.imap_port is not None:
            first["port"] = update.imap_port
        if update.imap_username is not None:
            first["username"] = update.imap_username
        if update.imap_mailbox is not None:
            first["mailbox"] = update.imap_mailbox
        if "imap_since_date" in update.model_fields_set:
            if update.imap_since_date:
                try:
                    datetime.date.fromisoformat(update.imap_since_date)
                except Exception:
                    raise HTTPException(status_code=400, detail="since_date must be YYYY-MM-DD")
                first["since_date"] = update.imap_since_date
            else:
                first["since_date"] = None
        if update.imap_use_ssl is not None:
            first["use_ssl"] = update.imap_use_ssl
        if update.delete_after_import is not None:
            first["delete_after_import"] = update.delete_after_import
        if update.never_mark_spam is not None:
            first["never_mark_spam"] = bool(update.never_mark_spam)
        if update.imap_use_idle is not None:
            first["use_idle"] = bool(update.imap_use_idle)
        if update.imap_idle_refresh_minutes is not None:
            first["idle_refresh_minutes"] = max(1, int(update.imap_idle_refresh_minutes))
        accounts[0] = first
        cfg["imap_accounts"] = accounts
        cfg.pop("imap", None)

    # Strip removed Gmail token paths from IMAP routing lists.
    valid_tokens = {
        Path(str(a.get("token_path", "token.json"))).name for a in get_gmail_accounts(cfg)
    }
    for acct in cfg.get("imap_accounts") or []:
        paths = acct.get("gmail_token_paths") or []
        if isinstance(paths, list):
            acct["gmail_token_paths"] = [
                p for p in paths if Path(str(p)).name in valid_tokens or str(p) in valid_tokens
            ]

    if update.poll_interval_minutes is not None:
        cfg["poll_interval_minutes"] = update.poll_interval_minutes
    if update.state_db_path is not None:
        cfg.setdefault("state", {})["db_path"] = update.state_db_path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    _sync_watcher_threads()
    return {"status": "ok"}

class DiscoveredToken(BaseModel):
    token_path: str
    email: str | None = None
    in_use: bool = False


@app.get("/api/gmail/discover-tokens", response_model=list[DiscoveredToken])
def api_gmail_discover_tokens(request: Request) -> list[DiscoveredToken]:
    """List token*.json files in config dir and (best-effort) associated Gmail email."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    cfg_dir = _config_dir_safe()
    if not cfg_dir:
        return []
    # tokens in directory
    token_files = sorted([p.name for p in Path(cfg_dir).glob("token*.json") if p.is_file()])
    in_use: set[str] = set()
    try:
        if _config_exists():
            cfg = load_config(_get_config_path(), resolve_password=False)
            for a in get_gmail_accounts(cfg):
                # Normalize for comparison against filenames in this directory. Config may store
                # relative paths ("token2.json") or absolute paths ("/.../token2.json").
                tp = Path(str(a.get("token_path", "token.json")))
                in_use.add(tp.name)
    except Exception:
        pass
    out: list[DiscoveredToken] = []
    # Resolve email addresses best-effort (credentials required)
    cred_path = Path(cfg_dir) / "credentials.json"
    for fname in token_files:
        tok_path = Path(cfg_dir) / fname
        email = None
        try:
            if cred_path.exists() and tok_path.exists():
                service = get_gmail_service(cred_path, tok_path)
                profile = service.users().getProfile(userId="me").execute()
                email = profile.get("emailAddress")
        except Exception:
            email = None
        out.append(DiscoveredToken(token_path=fname, email=email, in_use=(fname in in_use)))
    return out


class DeleteGmailAccountBody(BaseModel):
    index: int


@app.delete("/api/gmail/accounts")
def api_delete_gmail_account(request: Request, body: DeleteGmailAccountBody) -> dict[str, str]:
    """Delete a secondary Gmail account entry and delete its token file (safety: index must be > 0)."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if body.index <= 0:
        raise HTTPException(status_code=400, detail="Cannot delete the primary Gmail account from the UI.")
    path = _get_config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="config.json not found")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    accounts = get_gmail_accounts(cfg)
    if body.index >= len(accounts):
        raise HTTPException(status_code=400, detail="Invalid account index")
    token_path = str(accounts[body.index].get("token_path", "")).strip()
    token_name = Path(token_path).name
    # Remove from config
    new_accounts = [accounts[i] for i in range(len(accounts)) if i != body.index]
    cfg["gmail_accounts"] = new_accounts
    cfg.pop("gmail", None)
    # Strip deleted token from IMAP routing lists
    try:
        imap_accounts = get_imap_accounts(cfg)
    except ValueError:
        imap_accounts = []
    for acct in imap_accounts:
        paths = acct.get("gmail_token_paths") or []
        if isinstance(paths, list):
            acct["gmail_token_paths"] = [
                p
                for p in paths
                if Path(str(p)).name != token_name and str(p) != token_path
            ]
    if imap_accounts:
        cfg["imap_accounts"] = imap_accounts
        cfg.pop("imap", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        # Delete token file (only if inside config dir and named token*.json)
        cfg_dir = _config_dir()
        tp = Path(token_path)
        if not tp.is_absolute():
            tp = cfg_dir / tp
        tp = tp.resolve()
        if tp.parent != cfg_dir.resolve():
            return {"status": "ok", "message": "Account removed. Token file was not deleted (outside config dir)."}
        if not tp.name.startswith("token") or not tp.name.endswith(".json"):
            return {"status": "ok", "message": "Account removed. Token file was not deleted (unexpected filename)."}
        if tp.exists():
            tp.unlink()
        return {"status": "ok", "message": f"Account removed. Deleted {tp.name}."}
    except Exception:
        return {"status": "ok", "message": "Account removed. Token file deletion failed (check permissions)."}


class CopyAllBody(BaseModel):
    """Body for copy-all: copy every message from ISP to Gmail (with optional delete after)."""
    delete_after: bool = False
    account_id: str | None = None  # omit = all accounts


@app.post("/api/fetch")
def api_fetch(
    request: Request,
    dry_run: bool = False,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Trigger one fetch cycle (one account or all). Optionally dry_run."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    setup_logging()
    install_log_buffer()
    try:
        path = str(_get_config_path())
        if account_id:
            return _run_once_locked(
                config_path=path, dry_run=dry_run, account_id=account_id
            )
        # All accounts (each under its own fetch lock / IDLE pause)
        cfg = load_config(path, resolve_password=False)
        accounts = get_imap_accounts(cfg)
        if not accounts:
            raise HTTPException(status_code=400, detail="No IMAP accounts configured")
        totals = {"imported": 0, "skipped_duplicate": 0, "deleted": 0}
        per: list[dict[str, Any]] = []
        errors: list[str] = []
        last_fetch_time: str | None = None
        for acct in accounts:
            aid = str(acct.get("id") or DEFAULT_IMAP_ACCOUNT_ID)
            result = _run_once_locked(
                config_path=path, dry_run=dry_run, account_id=aid
            )
            per.append(result)
            totals["imported"] += int(result.get("imported") or 0)
            totals["skipped_duplicate"] += int(result.get("skipped_duplicate") or 0)
            totals["deleted"] += int(result.get("deleted") or 0)
            if result.get("last_fetch_time"):
                last_fetch_time = str(result["last_fetch_time"])
            if result.get("error"):
                errors.append(f"{aid}: {result['error']}")
        return {
            "error": "; ".join(errors) if errors else None,
            "imported": totals["imported"],
            "skipped_duplicate": totals["skipped_duplicate"],
            "deleted": totals["deleted"],
            "last_fetch_time": last_fetch_time,
            "accounts": per,
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/fetch/copy-all")
def api_fetch_copy_all(request: Request, body: CopyAllBody) -> dict[str, Any]:
    """Copy all emails from ISP mailbox(es) to Gmail. Optionally delete from ISP after."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    setup_logging()
    install_log_buffer()
    try:
        path = str(_get_config_path())
        cfg = load_config(path, resolve_password=False)
        if body.account_id:
            targets = [body.account_id]
        else:
            targets = [
                str(a.get("id") or DEFAULT_IMAP_ACCOUNT_ID) for a in get_imap_accounts(cfg)
            ]
        if not targets:
            raise HTTPException(status_code=400, detail="No IMAP accounts configured")
        totals = {"imported": 0, "skipped_duplicate": 0, "deleted": 0, "error": None}
        per: list[dict[str, Any]] = []
        errors: list[str] = []
        for aid in targets:
            with _fetch_lock_for(aid):
                with pause_idle_connection(account_id=aid):
                    result = run_copy_all(
                        config_path=path,
                        delete_after_import=body.delete_after,
                        dry_run=False,
                        account_id=aid,
                    )
            per.append(result)
            totals["imported"] += int(result.get("imported") or 0)
            totals["skipped_duplicate"] += int(result.get("skipped_duplicate") or 0)
            totals["deleted"] += int(result.get("deleted") or 0)
            if result.get("error"):
                errors.append(f"{aid}: {result['error']}")
        out = {
            "error": "; ".join(errors) if errors else None,
            "imported": totals["imported"],
            "skipped_duplicate": totals["skipped_duplicate"],
            "deleted": totals["deleted"],
            "accounts": per,
        }
        return out
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/logs")
def api_logs(request: Request, n: int = 100) -> dict[str, list[str]]:
    """Return recent log lines (no credentials)."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"lines": get_recent_logs(min(n, 500))}


@app.get("/api/status")
def api_status(request: Request) -> dict[str, Any]:
    """Last fetch time(s) and basic status per IMAP account."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        cfg = load_config(_get_config_path(), resolve_password=False)
        state_cfg = cfg.get("state", {})
        raw_db_path = state_cfg.get("db_path", "state.db")
        db_path = Path(raw_db_path)
        if not db_path.is_absolute():
            db_path = Path(_get_config_path()).resolve().parent / db_path
        state = StateStore(str(db_path))
        state.connect()
        rows = state.get_all_last_fetch_times()
        # Ensure every configured account appears
        by_id = {str(r["account_id"]): r for r in rows}
        accounts_out: list[dict[str, Any]] = []
        try:
            configured = get_imap_accounts(cfg)
        except ValueError:
            configured = []
        for acct in configured:
            aid = str(acct.get("id") or DEFAULT_IMAP_ACCOUNT_ID)
            mailbox = acct.get("mailbox", "INBOX")
            if aid in by_id:
                accounts_out.append(by_id[aid])
            else:
                row = state.get_last_fetch_time_any(mailbox, account_id=aid)
                accounts_out.append(
                    {
                        "account_id": aid,
                        "mailbox": mailbox,
                        "last_fetch_time": row[0] if row else None,
                        "uid_validity": row[1] if row else None,
                    }
                )
        state.close()
        latest = None
        for a in accounts_out:
            t = a.get("last_fetch_time")
            if t and (latest is None or str(t) > str(latest)):
                latest = t
        return {
            "last_fetch_time": latest,
            "accounts": accounts_out,
        }
    except Exception:
        return {"last_fetch_time": None, "accounts": [], "error": "could not read state"}

_HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Fetch2Gmail</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 800px; margin: 1rem auto; padding: 0 1rem; }
    h1 { font-size: 1.5rem; }
    section { margin: 1.5rem 0; }
    label { display: block; margin-top: 0.5rem; }
    input, button { padding: 0.4rem; margin-right: 0.5rem; }
    button { cursor: pointer; }
    pre { background: #f4f4f4; padding: 0.75rem; overflow: auto; max-height: 300px; font-size: 0.85rem; }
    .status { color: #066; }
    .error { color: #c00; }
    .ok { color: #060; }
    #setupWizard { display: none; }
    #dashboard { display: none; }
    #setPasswordWizard { display: none; }
    #tokenFirst { display: none; }
  </style>
</head>
<body>
  <h1>Fetch2Gmail</h1>
  <p id="subtitle">IMAP to Gmail import. Configure below.</p>
  <p id="loadingMsg">Loading...</p>

  <div id="credentialsFirst" style="display:none">
    <section>
      <h2>Add credentials to get started</h2>
      <p>Place <strong>credentials.json</strong> in this app&apos;s folder (the same folder you run <code>fetch2gmail serve</code> from). Get it from <strong>Google Cloud Console</strong>: create a project, enable Gmail API, configure the OAuth consent screen, create a <strong>Web application</strong> OAuth client, add the redirect URI <code>http://127.0.0.1:8765/auth/gmail/callback</code>, then download the JSON as <strong>credentials.json</strong>.</p>
      <p>Then run <strong><code>fetch2gmail auth</code></strong> (CLI) to get <strong>token.json</strong>. After you add both files, <a href="/">refresh this page</a> to continue.</p>
    </section>
  </div>

  <div id="tokenFirst" style="display:none">
    <section>
      <h2>Get token.json</h2>
      <p>Run <strong><code>fetch2gmail auth</code></strong> from the app folder (the same folder you run <code>fetch2gmail serve</code> from). A browser will open; sign in with the Gmail account that will receive the imported mail and click Allow. <strong>token.json</strong> will be saved in that folder.</p>
      <p>After you have token.json, <a href="/">refresh this page</a> to set the UI password and continue.</p>
    </section>
  </div>

  <div id="setPasswordWizard" style="display:none">
    <section>
      <h2>Set UI password</h2>
      <p>Set a username and password to protect this UI. You will use these to log in (HTTP Basic Auth).</p>
      <label>Username <input id="set_pw_username" type="text" placeholder="admin" autocomplete="username"></label>
      <label>Password <input id="set_pw_password" type="password" placeholder="Choose a password" autocomplete="new-password"></label>
      <label>Confirm password <input id="set_pw_confirm" type="password" placeholder="Confirm" autocomplete="new-password"></label>
      <button type="button" id="btnSetPassword">Set password</button>
      <p id="setPasswordMsg" class="status"></p>
    </section>
  </div>

  <div id="setupWizard">
    <section>
      <h2>Configure your ISP email</h2>
      <p>Enter your IMAP (ISP) mailbox details. Your password is stored encrypted in <code>.env</code> (not plain text), not in the config file.</p>
      <label>IMAP host <input id="s_imap_host" type="text" placeholder="imap.example.com"></label>
      <label>IMAP port <input id="s_imap_port" type="number" value="993"></label>
      <label style="display:inline-block; margin-top:0.25rem;"><input type="checkbox" id="s_imap_use_ssl" checked> Use SSL/TLS for IMAP</label>
      <p class="hint" style="font-size:0.85rem; color:#666; margin-top:0;">Uncheck for plain IMAP (e.g. port 143).</p>
      <label>IMAP username (email) <input id="s_imap_username" type="text" placeholder="you@isp.com"></label>
      <label>IMAP password <input id="s_imap_password" type="password" placeholder="Stored encrypted in .env"></label>
      <label>Mailbox <input id="s_imap_mailbox" type="text" value="INBOX" title="IMAP folder to fetch from. INBOX is the main inbox where new mail arrives."></label>
      <p class="hint" style="font-size:0.85rem; color:#666; margin-top:0;">Mailbox is the IMAP folder to fetch from. <strong>INBOX</strong> is the main inbox where new mail arrives at your ISP; leave as INBOX unless you use a different folder.</p>
      <label>Only fetch mail on or after <input id="s_imap_since_date" type="date"></label>
      <label style="margin-top:1rem;"><input type="checkbox" id="s_delete_after_import" checked> Delete emails from ISP after importing to Gmail</label>
      <label><input type="checkbox" id="s_gmail_use_label"> Add a Gmail label to imported mail</label>
      <div id="s_gmail_label_row" style="display:none;"><label>Label name <input id="s_gmail_label" type="text" value="ISP Mail" placeholder="e.g. ISP Mail"></label></div>
      <button type="button" id="btnSetup">Create config</button>
      <p id="setupMsg" class="status"></p>
    </section>
  </div>

  <div id="dashboard">
  <section>
    <h2>Status</h2>
    <p id="status">Loading...</p>
    <button type="button" id="btnFetch">Run fetch now</button>
    <button type="button" id="btnDryRun">Dry run (no import/delete)</button>
    <div style="margin-top:1rem;">
      <h3 style="font-size:1rem; margin-bottom:0.5rem;">Copy all emails from ISP to Gmail</h3>
      <p class="hint" style="font-size:0.85rem; color:#666; margin:0.25rem 0;">Fetches every message in the mailbox (not just new). Read/unread is preserved. Skips messages already in Gmail.</p>
      <label style="display:inline-block; margin-top:0.5rem;"><input type="checkbox" id="copy_all_delete_after"> Delete from ISP after copying</label>
      <button type="button" id="btnCopyAll" style="margin-left:0.5rem;">Copy all now</button>
      <p id="copyAllMsg" class="status" style="margin-top:0.5rem;"></p>
    </div>
  </section>

  <section>
    <h2>Gmail</h2>
    <p id="gmailStatus"></p>
    <p id="gmailEmail" class="status" style="font-size:0.9rem"></p>
    <div id="gmailAccounts"></div>
    <button type="button" id="btnAddGmailAccount">Add Gmail account</button>
    <div id="addGmailAccountPanel" style="display:none; margin-top:0.75rem; padding:0.75rem; background:#f7f7f7; border:1px solid #e6e6e6;">
      <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap;">
        <label style="margin:0; flex: 1 1 320px;">
          Token to add
          <select id="addGmailTokenSelect" style="width:100%; padding:0.4rem; margin-top:0.25rem;"></select>
        </label>
        <div style="display:flex; gap:0.5rem; align-items:flex-end; padding-top:1.35rem;">
          <button type="button" id="btnConfirmAddGmailAccount">Add</button>
          <button type="button" id="btnCancelAddGmailAccount">Cancel</button>
        </div>
      </div>
      <p id="addGmailAccountMsg" class="status" style="margin-top:0.5rem;"></p>
    </div>
    <p class="hint" style="font-size:0.85rem; color:#666; margin-top:0.5rem;">
      Tokens are created with <code>fetch2gmail auth</code> (CLI). To add a second account, run it again with a different token filename (e.g. <code>--token token2.json</code>).
    </p>
  </section>

  <section id="changePasswordSection" style="display:none">
    <h2>Change UI password</h2>
    <p>Update the username and password used to log in to this UI.</p>
    <button type="button" id="btnShowChangePassword">Change password</button>
    <div id="changePasswordForm" style="display:none; margin-top:1rem;">
      <label>Current password <input id="change_pw_current" type="password" autocomplete="current-password"></label>
      <label>New username <input id="change_pw_username" type="text" placeholder="Leave blank to keep current" autocomplete="username"></label>
      <label>New password <input id="change_pw_new" type="password" autocomplete="new-password"></label>
      <label>Confirm new password <input id="change_pw_confirm" type="password" autocomplete="new-password"></label>
      <button type="button" id="btnChangePassword">Update password</button>
      <p id="changePasswordMsg" class="status"></p>
    </div>
  </section>

  <section>
    <h2>IMAP accounts</h2>
    <p class="hint" style="font-size:0.85rem; color:#666;">One process can watch multiple IMAP mailboxes. Leave Gmail destinations unchecked to import into all configured Gmail accounts.</p>
    <div id="imapAccounts"></div>
    <button type="button" id="btnAddImapAccount" style="margin-top:0.75rem;">Add IMAP account</button>
    <hr style="margin:1.25rem 0; border:none; border-top:1px solid #eee;">
    <label><input type="checkbox" id="gmail_use_label"> Add a Gmail label to imported mail (primary account)</label>
    <div id="gmail_label_row" style="display:none;"><label>Label name <input id="gmail_label" type="text" placeholder="e.g. ISP Mail"></label></div>
    <label>Poll interval (minutes) <input id="poll_interval" type="number" min="1"></label>
    <p class="hint" style="font-size:0.85rem; color:#666; margin-top:0;">Used when IDLE is unavailable or disabled (shared across IMAP accounts).</p>
    <label>State DB path <input id="state_db" type="text"></label>
    <button type="button" id="btnSave">Save config</button>
  </section>

  <section>
    <h2>Recent logs</h2>
    <pre id="logs">(fetch or run to see logs)</pre>
    <button type="button" id="btnRefreshLogs">Refresh</button>
  </section>
  </div>

  <script src="/static/app.js"></script>
</body>
</html>
"""

_APP_JS = r"""
(function() {
  const api = function(path) { return fetch(path).then(function(r) { if (r.status === 401) { location.href = '/'; throw new Error('Unauthorized'); } return r.ok ? r.json() : r.json().then(function(j) { return Promise.reject(j); }); }); };
  const put = function(path, body) { return fetch(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(function(r) { if (r.status === 401) { location.href = '/'; throw new Error('Unauthorized'); } return r.ok ? r.json() : r.json().then(function(j) { return Promise.reject(j); }); }); };
  const del = function(path, body) { return fetch(path, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(function(r) { if (r.status === 401) { location.href = '/'; throw new Error('Unauthorized'); } return r.ok ? r.json() : r.json().then(function(j) { return Promise.reject(j); }); }); };

  fetch('/api/setup/status').then(function(r) { return r.json(); }).then(function(st) {
    if (!st.credentials_exist) {
      document.getElementById('loadingMsg').style.display = 'none';
      document.getElementById('credentialsFirst').style.display = 'block';
      return;
    }
    if (!st.token_available) {
      document.getElementById('loadingMsg').style.display = 'none';
      document.getElementById('tokenFirst').style.display = 'block';
      return;
    }
    loadConfig().then(function(c) { if (c) { loadStatus(); loadLogs(); wireSetPasswordWizard(c.show_set_password_wizard); wireChangePassword(c.ui_password_mode); } });
  }).catch(function() { document.getElementById('loadingMsg').style.display = 'none'; });
  var params = new URLSearchParams(location.search);
  if (params.get('error') === 'no_credentials') document.getElementById('subtitle').innerHTML = '<span class="error">Put credentials.json in the app folder and run fetch2gmail auth for token.json.</span>';

  function hideLoading() { var el = document.getElementById('loadingMsg'); if (el) el.style.display = 'none'; }
  var _imapAccountsState = [];
  var _gmailAccountsForRouting = [];
  function ensureGmailAccounts(c) {
    if (c.gmail_accounts && c.gmail_accounts.length) return c.gmail_accounts;
    // Back-compat: synthesize from single c.gmail
    if (c.gmail) return [c.gmail];
    return [];
  }
  function ensureImapAccounts(c) {
    if (c.imap_accounts && c.imap_accounts.length) return JSON.parse(JSON.stringify(c.imap_accounts));
    if (c.imap) {
      return [{
        id: 'default',
        host: c.imap.host || '',
        port: c.imap.port || 993,
        username: c.imap.username || '',
        mailbox: c.imap.mailbox || 'INBOX',
        use_ssl: c.imap.use_ssl !== false,
        delete_after_import: c.imap.delete_after_import !== false,
        since_date: c.imap.since_date || null,
        use_idle: c.imap.use_idle !== false,
        idle_refresh_minutes: c.imap.idle_refresh_minutes || 25,
        never_mark_spam: c.imap.never_mark_spam !== false,
        password_set: !!c.imap_password_set,
        gmail_token_paths: []
      }];
    }
    return [];
  }
  function esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function collectImapAccountsFromDom() {
    var wrap = document.getElementById('imapAccounts');
    if (!wrap) return _imapAccountsState.slice();
    var cards = wrap.querySelectorAll('[data-imap-idx]');
    var out = [];
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      var idx = parseInt(card.getAttribute('data-imap-idx'), 10);
      var prev = _imapAccountsState[idx] || {};
      var idEl = card.querySelector('[data-f="id"]');
      var hostEl = card.querySelector('[data-f="host"]');
      var portEl = card.querySelector('[data-f="port"]');
      var userEl = card.querySelector('[data-f="username"]');
      var mailboxEl = card.querySelector('[data-f="mailbox"]');
      var sinceEl = card.querySelector('[data-f="since_date"]');
      var pwEl = card.querySelector('[data-f="password"]');
      var idleRefreshEl = card.querySelector('[data-f="idle_refresh_minutes"]');
      var paths = [];
      var checks = card.querySelectorAll('[data-gmail-token]');
      for (var c = 0; c < checks.length; c++) {
        if (checks[c].checked) paths.push(checks[c].getAttribute('data-gmail-token'));
      }
      out.push({
        id: (idEl && idEl.value || '').trim(),
        host: hostEl ? hostEl.value : '',
        port: portEl ? parseInt(portEl.value, 10) || 993 : 993,
        username: userEl ? userEl.value : '',
        mailbox: mailboxEl ? mailboxEl.value : 'INBOX',
        use_ssl: !!(card.querySelector('[data-f="use_ssl"]') || {}).checked,
        delete_after_import: !!(card.querySelector('[data-f="delete_after_import"]') || {}).checked,
        never_mark_spam: !!(card.querySelector('[data-f="never_mark_spam"]') || {}).checked,
        use_idle: !!(card.querySelector('[data-f="use_idle"]') || {}).checked,
        idle_refresh_minutes: idleRefreshEl ? (parseInt(idleRefreshEl.value, 10) || 25) : 25,
        since_date: sinceEl && sinceEl.value ? sinceEl.value : null,
        password: pwEl && pwEl.value ? pwEl.value : null,
        password_set: !!prev.password_set,
        gmail_token_paths: paths
      });
    }
    return out;
  }
  function renderImapAccounts(c) {
    var wrap = document.getElementById('imapAccounts');
    if (!wrap) return;
    _imapAccountsState = ensureImapAccounts(c);
    _gmailAccountsForRouting = ensureGmailAccounts(c);
    if (!_imapAccountsState.length) {
      wrap.innerHTML = '<p class="error">No IMAP accounts. Click Add IMAP account.</p>';
      return;
    }
    var html = '';
    for (var i = 0; i < _imapAccountsState.length; i++) {
      var a = _imapAccountsState[i];
      var selected = {};
      var paths = a.gmail_token_paths || [];
      var allSelected = !paths.length;
      for (var p = 0; p < paths.length; p++) selected[paths[p]] = true;
      html += '<div data-imap-idx="' + i + '" style="border:1px solid #e5e5e5; padding:0.75rem; margin:0.75rem 0; border-radius:4px;">';
      html += '<div style="display:flex; justify-content:space-between; align-items:center; gap:0.5rem;">';
      html += '<strong>IMAP #' + (i + 1) + '</strong>';
      html += ( _imapAccountsState.length > 1 ? '<button type="button" data-idel="' + i + '">Remove</button>' : '' );
      html += '</div>';
      html += '<label>Id <input data-f="id" type="text" value="' + esc(a.id || '') + '" placeholder="e.g. isp1"></label>';
      html += '<label>Host <input data-f="host" type="text" value="' + esc(a.host || '') + '"></label>';
      html += '<label>Port <input data-f="port" type="number" value="' + esc(a.port || 993) + '"></label>';
      html += '<label style="display:inline-block; margin-top:0.25rem;"><input type="checkbox" data-f="use_ssl"' + (a.use_ssl !== false ? ' checked' : '') + '> Use SSL/TLS</label>';
      html += '<label>Username <input data-f="username" type="text" value="' + esc(a.username || '') + '"></label>';
      html += '<label>Password <input data-f="password" type="password" placeholder="' + (a.password_set ? 'Leave blank to keep current' : 'Required') + '"></label>';
      html += '<label>Mailbox <input data-f="mailbox" type="text" value="' + esc(a.mailbox || 'INBOX') + '"></label>';
      html += '<label>Only fetch on or after <input data-f="since_date" type="date" value="' + esc(a.since_date || '') + '"></label>';
      html += '<label style="margin-top:0.5rem;"><input type="checkbox" data-f="delete_after_import"' + (a.delete_after_import !== false ? ' checked' : '') + '> Delete from ISP after import</label>';
      html += '<label><input type="checkbox" data-f="never_mark_spam"' + (a.never_mark_spam !== false ? ' checked' : '') + '> Never mark imported mail as spam</label>';
      html += '<label><input type="checkbox" data-f="use_idle"' + (a.use_idle !== false ? ' checked' : '') + '> Use IMAP IDLE when supported</label>';
      html += '<label>IDLE refresh (minutes) <input data-f="idle_refresh_minutes" type="number" min="1" value="' + esc(a.idle_refresh_minutes || 25) + '"></label>';
      html += '<div style="margin-top:0.75rem;"><strong>Gmail destinations</strong>';
      html += '<p class="hint" style="font-size:0.85rem; color:#666; margin:0.25rem 0;">Unchecked = all Gmail accounts. Check specific tokens to route this IMAP account.</p>';
      if (!_gmailAccountsForRouting.length) {
        html += '<p class="error">No Gmail accounts configured.</p>';
      } else {
        for (var g = 0; g < _gmailAccountsForRouting.length; g++) {
          var ga = _gmailAccountsForRouting[g];
          var tp = ga.token_path || 'token.json';
          var checked = allSelected || selected[tp] || selected[tp.split(/[/\\\\]/).pop()];
          html += '<label style="display:block;"><input type="checkbox" data-gmail-token="' + esc(tp) + '"' + (checked && !allSelected ? ' checked' : '') + '> ' + esc(ga.label || tp) + ' (<code>' + esc(tp) + '</code>)</label>';
        }
      }
      html += '</div></div>';
    }
    wrap.innerHTML = html;
    var delBtns = wrap.querySelectorAll('button[data-idel]');
    for (var d = 0; d < delBtns.length; d++) {
      delBtns[d].onclick = function(ev) {
        var idx = parseInt(ev.target.getAttribute('data-idel'), 10);
        _imapAccountsState = collectImapAccountsFromDom();
        if (_imapAccountsState.length <= 1) return;
        _imapAccountsState.splice(idx, 1);
        renderImapAccounts({ imap_accounts: _imapAccountsState, gmail_accounts: _gmailAccountsForRouting });
      };
    }
  }
  function renderGmailAccounts(c) {
    var wrap = document.getElementById('gmailAccounts');
    if (!wrap) return;
    var accounts = ensureGmailAccounts(c);
    if (!accounts.length) {
      wrap.innerHTML = '<p class=\"error\">No Gmail accounts configured.</p>';
      return;
    }
    // Enrich with Gmail email addresses by looking at token*.json files.
    api('/api/gmail/discover-tokens').then(function(tokens) {
      var emailByToken = {};
      if (Array.isArray(tokens)) {
        for (var i = 0; i < tokens.length; i++) {
          var t = tokens[i];
          if (t && t.token_path) emailByToken[t.token_path] = t.email || '';
        }
      }
      var html = '<div style=\"margin-top:0.5rem;\">';
      html += '<table style=\"width:100%; border-collapse:collapse;\">';
      html += '<tr><th style=\"text-align:left; font-weight:600; padding:4px 0;\">Account</th><th style=\"text-align:left; font-weight:600; padding:4px 0;\">Token</th><th style=\"text-align:left; font-weight:600; padding:4px 0;\">Gmail account</th><th></th></tr>';
      for (var j = 0; j < accounts.length; j++) {
        var a = accounts[j];
        var token = (a.token_path || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        var email = emailByToken[a.token_path || ''] || '';
        var emailDisplay = email ? email.replace(/</g,'&lt;').replace(/>/g,'&gt;') : '(unknown)';
        html += '<tr style=\"border-top:1px solid #eee;\">';
        html += '<td style=\"padding:6px 0;\">#' + (j + 1) + (j === 0 ? ' (primary)' : '') + '</td>';
        html += '<td style=\"padding:6px 0;\"><code>' + token + '</code></td>';
        html += '<td style=\"padding:6px 0; white-space:nowrap;\">' + emailDisplay + '</td>';
        html += '<td style=\"padding:6px 0; text-align:right;\">' + (j === 0 ? '' : '<button type=\"button\" data-gdel=\"' + j + '\">Delete</button>') + '</td>';
        html += '</tr>';
      }
      html += '</table>';
      html += '<p class=\"hint\" style=\"font-size:0.85rem; color:#666; margin:0.5rem 0 0;\">Deleting a secondary account also deletes its token file (e.g. <code>token2.json</code>).</p>';
      html += '</div>';
      wrap.innerHTML = html;
      // Wire delete buttons
      var btns = wrap.querySelectorAll('button[data-gdel]');
      for (var k = 0; k < btns.length; k++) {
        btns[k].onclick = function(ev) {
          var idx = parseInt(ev.target.getAttribute('data-gdel'), 10);
          if (!confirm('Delete Gmail account #' + (idx + 1) + '?\\n\\nThis will remove it from config and delete its token file.')) return;
          del('/api/gmail/accounts', { index: idx }).then(function(r) {
            document.getElementById('status').textContent = r.message || 'Account deleted.';
            document.getElementById('status').className = 'status';
            return loadConfig().then(function(cc) { if (cc) { renderGmailAccounts(cc); } });
          }).catch(function(e) {
            document.getElementById('status').textContent = 'Error: ' + (typeof e.detail === 'string' ? e.detail : (e.detail && e.detail.detail) || e.message || 'Delete failed');
            document.getElementById('status').className = 'error';
          });
        };
      }
    }).catch(function() {
      // Fallback: render without email info if discover-tokens fails.
      var html = '<div style=\"margin-top:0.5rem;\">';
      html += '<table style=\"width:100%; border-collapse:collapse;\">';
      html += '<tr><th style=\"text-align:left; font-weight:600; padding:4px 0;\">Account</th><th style=\"text-align:left; font-weight:600; padding:4px 0;\">Token</th><th></th></tr>';
      for (var j = 0; j < accounts.length; j++) {
        var a = accounts[j];
        var token = (a.token_path || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        html += '<tr style=\"border-top:1px solid #eee;\">';
        html += '<td style=\"padding:6px 0;\">#' + (j + 1) + (j === 0 ? ' (primary)' : '') + '</td>';
        html += '<td style=\"padding:6px 0;\"><code>' + token + '</code></td>';
        html += '<td style=\"padding:6px 0; text-align:right;\">' + (j === 0 ? '' : '<button type=\"button\" data-gdel=\"' + j + '\">Delete</button>') + '</td>';
        html += '</tr>';
      }
      html += '</table>';
      html += '<p class=\"hint\" style=\"font-size:0.85rem; color:#666; margin:0.5rem 0 0;\">Deleting a secondary account also deletes its token file (e.g. <code>token2.json</code>).</p>';
      html += '</div>';
      wrap.innerHTML = html;
      // Wire delete buttons in fallback as well.
      var btns = wrap.querySelectorAll('button[data-gdel]');
      for (var k = 0; k < btns.length; k++) {
        btns[k].onclick = function(ev) {
          var idx = parseInt(ev.target.getAttribute('data-gdel'), 10);
          if (!confirm('Delete Gmail account #' + (idx + 1) + '?\\n\\nThis will remove it from config and delete its token file.')) return;
          del('/api/gmail/accounts', { index: idx }).then(function(r) {
            document.getElementById('status').textContent = r.message || 'Account deleted.';
            document.getElementById('status').className = 'status';
            return loadConfig().then(function(cc) { if (cc) { renderGmailAccounts(cc); } });
          }).catch(function(e) {
            document.getElementById('status').textContent = 'Error: ' + (typeof e.detail === 'string' ? e.detail : (e.detail && e.detail.detail) || e.message || 'Delete failed');
            document.getElementById('status').className = 'error';
          });
        };
      }
    });
  }
  function loadConfig() {
    return api('/api/config').then(function(c) {
      hideLoading();
      if (c.show_set_password_wizard) {
        document.getElementById('dashboard').style.display = 'none';
        document.getElementById('setupWizard').style.display = 'none';
        document.getElementById('setPasswordWizard').style.display = 'block';
        wireSetPasswordWizard(true);
        wireChangePassword(false);
        return c;
      }
      if (!c.config_exists) {
        document.getElementById('dashboard').style.display = 'none';
        document.getElementById('setPasswordWizard').style.display = 'none';
        document.getElementById('setupWizard').style.display = 'block';
        return null;
      }
      document.getElementById('setPasswordWizard').style.display = 'none';
      document.getElementById('setupWizard').style.display = 'none';
      document.getElementById('dashboard').style.display = 'block';
      document.getElementById('gmail_use_label').checked = c.gmail.use_label;
      document.getElementById('gmail_label_row').style.display = c.gmail.use_label ? 'block' : 'none';
      document.getElementById('gmail_label').value = c.gmail.label;
      document.getElementById('poll_interval').value = c.poll_interval_minutes;
      document.getElementById('state_db').value = c.state_db_path;
      document.getElementById('gmailStatus').innerHTML = c.gmail_connected ? '<span class="ok">Gmail connected</span>' : '<span class="error">Not connected (add token.json from fetch2gmail auth)</span>';
      document.getElementById('gmailEmail').textContent = '';
      if (c.gmail_connected) {
        var configuredPaths = {};
        var accounts = ensureGmailAccounts(c);
        for (var ai = 0; ai < accounts.length; ai++) {
          var tp = (accounts[ai].token_path || '').trim();
          if (tp) configuredPaths[tp] = true;
          var base = tp.split(/[/\\\\]/).pop();
          if (base) configuredPaths[base] = true;
        }
        api('/api/gmail/discover-tokens').then(function(tokens) {
          if (!Array.isArray(tokens) || !tokens.length) return;
          var emails = [];
          for (var i = 0; i < tokens.length; i++) {
            var t = tokens[i];
            if (!t.email) continue;
            var path = (t.token_path || '').trim();
            var base = path.split(/[/\\\\]/).pop();
            if (configuredPaths[path] || configuredPaths[base]) emails.push(t.email);
          }
          if (!emails.length) return;
          var uniq = [];
          var seen = {};
          for (var j = 0; j < emails.length; j++) {
            var e = emails[j];
            if (!seen[e]) { seen[e] = true; uniq.push(e); }
          }
          document.getElementById('gmailEmail').textContent = 'Connected as ' + uniq.join(', ');
        }).catch(function() {});
      }
      renderImapAccounts(c);
      renderGmailAccounts(c);
      return c;
    }).catch(function(e) {
      hideLoading();
      if (e && e.detail === 'config.json not found') {
        document.getElementById('dashboard').style.display = 'none';
        document.getElementById('setupWizard').style.display = 'block';
        return null;
      }
      throw e;
    });
  }
  function loadStatus() {
    return api('/api/status').then(function(s) {
      var parts = ['Last fetch: ' + (s.last_fetch_time || 'never')];
      if (s.accounts && s.accounts.length > 1) {
        for (var i = 0; i < s.accounts.length; i++) {
          var a = s.accounts[i];
          parts.push('[' + (a.account_id || '?') + '] ' + (a.last_fetch_time || 'never'));
        }
      }
      document.getElementById('status').textContent = parts.join(' | ');
      document.getElementById('status').className = 'status';
    }).catch(function(e) {
      document.getElementById('status').textContent = 'Status: ' + ((e.detail && e.detail.detail) ? e.detail.detail : e.message);
      document.getElementById('status').className = 'error';
    });
  }
  function loadLogs() {
    return api('/api/logs?n=100').then(function(l) {
      document.getElementById('logs').textContent = l.lines.length ? l.lines.join('\n') : '(no logs)';
    }).catch(function() {});
  }
  function wireSetPasswordWizard(show) {
    if (!show) return;
    var msg = document.getElementById('setPasswordMsg');
    document.getElementById('btnSetPassword').onclick = function() {
      var username = document.getElementById('set_pw_username').value.trim();
      var password = document.getElementById('set_pw_password').value;
      var confirm = document.getElementById('set_pw_confirm').value;
      if (!username) { msg.textContent = 'Username cannot be empty.'; msg.className = 'error'; return; }
      if (!password) { msg.textContent = 'Password cannot be empty.'; msg.className = 'error'; return; }
      if (password !== confirm) { msg.textContent = 'Passwords do not match.'; msg.className = 'error'; return; }
      fetch('/api/setup/ui-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username, password: password, password_confirm: confirm })
      }).then(function(r) { return r.json().then(function(j) { return { ok: r.ok, body: j }; }); }).then(function(x) {
        if (x.ok) {
          msg.textContent = x.body.message || 'Password set. Reload the page.';
          msg.className = 'ok';
          setTimeout(function() { location.reload(); }, 2500);
        } else {
          msg.textContent = x.body.detail || 'Error setting password.';
          msg.className = 'error';
        }
      }).catch(function(e) {
        msg.textContent = 'Error: ' + (e.message || 'request failed');
        msg.className = 'error';
      });
    };
  }
  function wireChangePassword(show) {
    if (!show) return;
    var section = document.getElementById('changePasswordSection');
    var form = document.getElementById('changePasswordForm');
    if (!section || !form) return;
    section.style.display = 'block';
    document.getElementById('btnShowChangePassword').onclick = function() { form.style.display = form.style.display === 'none' ? 'block' : 'none'; };
    document.getElementById('btnChangePassword').onclick = function() {
      var msg = document.getElementById('changePasswordMsg');
      var current = document.getElementById('change_pw_current').value;
      var newUser = document.getElementById('change_pw_username').value.trim();
      var newPw = document.getElementById('change_pw_new').value;
      var confirm = document.getElementById('change_pw_confirm').value;
      if (!current) { msg.textContent = 'Current password is required.'; msg.className = 'error'; return; }
      if (!newPw) { msg.textContent = 'New password cannot be empty.'; msg.className = 'error'; return; }
      if (newPw !== confirm) { msg.textContent = 'New passwords do not match.'; msg.className = 'error'; return; }
      fetch('/api/setup/ui-password', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ current_password: current, new_username: newUser, new_password: newPw, new_password_confirm: confirm }) }).then(function(r) { return r.json().then(function(j) { return { ok: r.ok, body: j }; }); }).then(function(x) {
        if (x.ok) {
          msg.textContent = x.body.message || 'Password updated.';
          msg.className = 'ok';
          document.getElementById('change_pw_current').value = '';
          document.getElementById('change_pw_new').value = '';
          document.getElementById('change_pw_confirm').value = '';
        } else {
          msg.textContent = x.body.detail || 'Error';
          msg.className = 'error';
        }
      }).catch(function(e) {
        msg.textContent = e.message || 'Error';
        msg.className = 'error';
      });
    };
  }
  document.getElementById('btnFetch').onclick = function() {
    fetch('/api/fetch', { method: 'POST' }).then(function(r) {
      if (r.status === 401) { location.href = '/login'; return; }
      return r.json();
    }).then(function(j) {
      document.getElementById('status').textContent = 'Imported: ' + j.imported + ', Deleted: ' + j.deleted + '.';
      loadStatus(); loadLogs();
    }).catch(function(e) {
      document.getElementById('status').textContent = 'Error: ' + ((e.detail && e.detail.detail) ? e.detail.detail : e.message);
      document.getElementById('status').className = 'error';
      loadLogs();
    });
  };
  document.getElementById('btnDryRun').onclick = function() {
    fetch('/api/fetch?dry_run=true', { method: 'POST' }).then(function(r) {
      if (r.status === 401) { location.href = '/login'; return; }
      return r.json();
    }).then(function(j) {
      document.getElementById('status').textContent = 'Dry run: would import ' + j.imported + '.';
      loadLogs();
    }).catch(function(e) {
      document.getElementById('status').textContent = 'Error: ' + ((e.detail && e.detail.detail) ? e.detail.detail : e.message);
      document.getElementById('status').className = 'error';
      loadLogs();
    });
  };
  document.getElementById('btnCopyAll').onclick = function() {
    var msgEl = document.getElementById('copyAllMsg');
    var deleteAfter = document.getElementById('copy_all_delete_after').checked;
    msgEl.textContent = 'Copying all...';
    msgEl.className = 'status';
    document.getElementById('btnCopyAll').disabled = true;
    fetch('/api/fetch/copy-all', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ delete_after: deleteAfter })
    }).then(function(r) {
      if (r.status === 401) { location.href = '/'; return; }
      return r.json();
    }).then(function(j) {
      if (j.error) {
        msgEl.textContent = 'Error: ' + j.error;
        msgEl.className = 'error';
      } else {
        msgEl.textContent = 'Imported: ' + j.imported + ', Skipped (already in Gmail): ' + j.skipped_duplicate + ', Deleted from ISP: ' + j.deleted + '.';
        msgEl.className = 'ok';
      }
      loadStatus();
      loadLogs();
    }).catch(function(e) {
      msgEl.textContent = 'Error: ' + ((e.detail && e.detail.detail) ? e.detail.detail : e.message || 'request failed');
      msgEl.className = 'error';
      loadLogs();
    }).then(function() {
      document.getElementById('btnCopyAll').disabled = false;
    });
  };
  document.getElementById('gmail_use_label').onchange = function() {
    document.getElementById('gmail_label_row').style.display = this.checked ? 'block' : 'none';
  };
  document.getElementById('btnSave').onclick = function() {
    var imapAccounts = collectImapAccountsFromDom();
    if (!imapAccounts.length) {
      document.getElementById('status').textContent = 'Error: at least one IMAP account is required';
      document.getElementById('status').className = 'error';
      return;
    }
    for (var i = 0; i < imapAccounts.length; i++) {
      if (!imapAccounts[i].id) {
        document.getElementById('status').textContent = 'Error: each IMAP account needs an id';
        document.getElementById('status').className = 'error';
        return;
      }
    }
    var body = {
      imap_accounts: imapAccounts.map(function(a) {
        var entry = {
          id: a.id,
          host: a.host,
          port: a.port,
          username: a.username,
          mailbox: a.mailbox,
          use_ssl: a.use_ssl,
          delete_after_import: a.delete_after_import,
          never_mark_spam: a.never_mark_spam,
          use_idle: a.use_idle,
          idle_refresh_minutes: a.idle_refresh_minutes,
          since_date: a.since_date,
          gmail_token_paths: a.gmail_token_paths || []
        };
        if (a.password) entry.password = a.password;
        return entry;
      }),
      gmail_use_label: document.getElementById('gmail_use_label').checked,
      gmail_label: document.getElementById('gmail_label').value,
      poll_interval_minutes: parseInt(document.getElementById('poll_interval').value, 10),
      state_db_path: document.getElementById('state_db').value
    };
    api('/api/config').then(function(c) {
      var accounts = ensureGmailAccounts(c);
      var useLabel = document.getElementById('gmail_use_label').checked;
      var globalLabel = document.getElementById('gmail_label').value || 'ISP Mail';
      for (var k = 0; k < accounts.length; k++) {
        accounts[k].use_label = useLabel;
        if (k === 0) accounts[k].label = globalLabel;
      }
      body.gmail_accounts = accounts;
      return put('/api/config', body);
    }).then(function() {
      document.getElementById('status').textContent = 'Config saved.';
      document.getElementById('status').className = 'status';
      return loadConfig();
    }).catch(function(e) {
      document.getElementById('status').textContent = 'Error: ' + (typeof e.detail === 'string' ? e.detail : (e.detail && e.detail.detail) || e.message || 'Save failed');
      document.getElementById('status').className = 'error';
    });
  };
  var addImapBtn = document.getElementById('btnAddImapAccount');
  if (addImapBtn) {
    addImapBtn.onclick = function() {
      _imapAccountsState = collectImapAccountsFromDom();
      var n = _imapAccountsState.length + 1;
      _imapAccountsState.push({
        id: 'isp' + n,
        host: '',
        port: 993,
        username: '',
        mailbox: 'INBOX',
        use_ssl: true,
        delete_after_import: true,
        never_mark_spam: true,
        use_idle: true,
        idle_refresh_minutes: 25,
        since_date: null,
        password_set: false,
        gmail_token_paths: []
      });
      renderImapAccounts({ imap_accounts: _imapAccountsState, gmail_accounts: _gmailAccountsForRouting });
    };
  }
  document.getElementById('btnRefreshLogs').onclick = loadLogs;

  document.getElementById('s_gmail_use_label').onchange = function() {
    document.getElementById('s_gmail_label_row').style.display = this.checked ? 'block' : 'none';
  };
  document.getElementById('btnSetup').onclick = function() {
    var msg = document.getElementById('setupMsg');
    fetch('/api/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
        imap_host: document.getElementById('s_imap_host').value,
        imap_port: parseInt(document.getElementById('s_imap_port').value, 10),
        imap_use_ssl: document.getElementById('s_imap_use_ssl').checked,
        imap_username: document.getElementById('s_imap_username').value,
        imap_password: document.getElementById('s_imap_password').value,
        imap_mailbox: document.getElementById('s_imap_mailbox').value,
        imap_since_date: document.getElementById('s_imap_since_date').value || null,
        delete_after_import: document.getElementById('s_delete_after_import').checked,
        gmail_use_label: document.getElementById('s_gmail_use_label').checked,
        gmail_label: document.getElementById('s_gmail_label').value
      })
    }).then(function(r) {
      if (!r.ok) return r.json().then(function(j) { throw new Error(j.detail || r.statusText); });
      msg.textContent = 'Config created. Connect Gmail next.';
      msg.className = 'ok';
      setTimeout(function() { location.reload(); }, 1500);
    }).catch(function(e) {
      msg.textContent = 'Error: ' + (e.detail || e.message);
      msg.className = 'error';
    });
  };

  var addBtn = document.getElementById('btnAddGmailAccount');
  if (addBtn) {
    addBtn.onclick = function() {
      var panel = document.getElementById('addGmailAccountPanel');
      var sel = document.getElementById('addGmailTokenSelect');
      var msg = document.getElementById('addGmailAccountMsg');
      var btnOk = document.getElementById('btnConfirmAddGmailAccount');
      var btnCancel = document.getElementById('btnCancelAddGmailAccount');
      if (!panel || !sel || !msg || !btnOk || !btnCancel) return;
      msg.textContent = 'Loading tokens...';
      msg.className = 'status';
      panel.style.display = 'block';
      sel.innerHTML = '';
      btnOk.disabled = true;

      function closePanel() {
        panel.style.display = 'none';
        msg.textContent = '';
        sel.innerHTML = '';
      }
      btnCancel.onclick = closePanel;

          api('/api/gmail/discover-tokens').then(function(tokens) {
        var available = tokens.filter(function(t) { return !t.in_use && t.token_path; });
        if (!available.length) {
          msg.textContent = 'No unused token*.json files found in the app folder. Run: fetch2gmail auth --token token2.json';
          msg.className = 'error';
          btnOk.disabled = true;
          return;
        }
        for (var i = 0; i < available.length; i++) {
          var t = available[i];
          var opt = document.createElement('option');
          opt.value = t.token_path;
          opt.textContent = t.token_path + (t.email ? (' (' + t.email + ')') : '');
          sel.appendChild(opt);
        }
        msg.textContent = 'Select a token and click Add.';
        msg.className = 'status';
        btnOk.disabled = false;

        btnOk.onclick = function() {
          var chosen = sel.value;
          if (!chosen) return;
          btnOk.disabled = true;
          msg.textContent = 'Adding account...';
          msg.className = 'status';
          var chosenEmail = '';
          for (var i = 0; i < available.length; i++) {
            if (available[i].token_path === chosen) { chosenEmail = available[i].email || ''; break; }
          }
          api('/api/config').then(function(c) {
            var accounts = ensureGmailAccounts(c);
            accounts.push({
              use_label: document.getElementById('gmail_use_label').checked,
              // Default label for new accounts: Gmail email if known; otherwise fall back to global label or ISP Mail.
              label: (chosenEmail || document.getElementById('gmail_label').value || 'ISP Mail'),
              credentials_path: (accounts[0] && accounts[0].credentials_path) ? accounts[0].credentials_path : 'credentials.json',
              token_path: chosen
            });
            return put('/api/config', { gmail_accounts: accounts });
          }).then(function() {
            msg.textContent = 'Account added.';
            msg.className = 'ok';
            setTimeout(function() { closePanel(); loadConfig(); }, 600);
          }).catch(function(e) {
            msg.textContent = 'Error: ' + (typeof e.detail === 'string' ? e.detail : (e.detail && e.detail.detail) || e.message || 'Failed');
            msg.className = 'error';
            btnOk.disabled = false;
          });
        };
      }).catch(function() {
        msg.textContent = 'Could not discover tokens.';
        msg.className = 'error';
        btnOk.disabled = true;
      });
    };
  }
})();
"""


def serve(host: str = "127.0.0.1", port: int = 8765, config_path: str | None = None) -> None:
    """Run the web UI (bind to localhost only)."""
    if config_path:
        import os
        os.environ["FETCH2GMAIL_CONFIG"] = config_path
    setup_logging()
    install_log_buffer()
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
