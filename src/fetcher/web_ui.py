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
import time
import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response as RawResponse
from pydantic import BaseModel

import imaplib

from .config import get_config_path, load_config
from .env_file import set_encrypted_env
from .gmail_client import get_gmail_service
from .imap_client import get_uid_validity
from .log_buffer import get_recent_logs, install_log_buffer
from .run import run_once, run_copy_all, setup_logging
from .ui_auth import create_ui_auth, load_ui_auth, verify_ui_auth

logger = logging.getLogger(__name__)

# Prevents multiple "Copy all" runs from overlapping (manual button only).
_copy_all_lock = threading.Lock()


def _poller_loop(stop_event: threading.Event) -> None:
    """Background thread: every poll_interval_minutes run a fetch when config exists.
    Re-reads config every 10s so changing poll_interval_minutes in the UI takes effect without restart.
    """
    last_run_at: float | None = None
    while not stop_event.is_set():
        try:
            if not _config_exists():
                stop_event.wait(timeout=60)
                continue
            path = _get_config_path()
            cfg = load_config(path, resolve_password=False)
            interval_minutes = max(1, int(cfg.get("poll_interval_minutes", 5)))
            interval_seconds = interval_minutes * 60
        except Exception as e:
            logger.warning("Poller could not load config: %s", e)
            stop_event.wait(timeout=60)
            continue
        now = time.monotonic()
        if last_run_at is None:
            # First run: short delay (30s) so we fetch soon after startup
            next_run_at = now + 30
        else:
            next_run_at = last_run_at + interval_seconds
        # Wait in 10s chunks and re-read config each wake so interval changes apply without restart
        while not stop_event.is_set() and time.monotonic() < next_run_at:
            stop_event.wait(timeout=10)
            # Re-read interval so UI changes take effect within ~10s
            try:
                if _config_exists() and last_run_at is not None:
                    cfg = load_config(_get_config_path(), resolve_password=False)
                    interval_minutes = max(1, int(cfg.get("poll_interval_minutes", 5)))
                    interval_seconds = interval_minutes * 60
                    next_run_at = last_run_at + interval_seconds
            except Exception:
                pass
        if stop_event.is_set():
            break
        last_run_at = time.monotonic()
        try:
            setup_logging()
            install_log_buffer()
            result = run_once(config_path=str(path), dry_run=False)
            if result.get("error"):
                logger.warning("Background fetch error: %s", result["error"])
            else:
                logger.info(
                    "Background fetch: imported=%s skipped=%s deleted=%s",
                    result.get("imported", 0),
                    result.get("skipped_duplicate", 0),
                    result.get("deleted", 0),
                )
        except ValueError as e:
            logger.warning("Background fetch skipped: %s", e)
        except Exception as e:
            logger.exception("Background fetch failed: %s", e)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    stop_event = threading.Event()
    poller = threading.Thread(target=_poller_loop, args=(stop_event,), daemon=True)
    poller.start()
    logger.info("Background poller started (interval from config poll_interval_minutes)")
    yield
    stop_event.set()
    poller.join(timeout=15)


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
    """True when config exists and the configured token file exists."""
    cfg_dir = _config_dir_safe()
    if not cfg_dir or not _config_exists():
        return False
    try:
        cfg = load_config(_get_config_path(), resolve_password=False)
        token_path = Path((cfg.get("gmail") or {}).get("token_path", "token.json"))
        if not token_path.is_absolute():
            token_path = cfg_dir / token_path
        return token_path.exists()
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


# Sanitized config for UI (no passwords, no token paths)
class ImapConfigSafe(BaseModel):
    host: str
    port: int
    username: str
    mailbox: str
    use_ssl: bool
    delete_after_import: bool
    since_date: str | None = None


class GmailConfigSafe(BaseModel):
    use_label: bool
    label: str
    credentials_path: str
    token_path: str


class UIConfigSafe(BaseModel):
    host: str
    port: int


class ConfigResponse(BaseModel):
    imap: ImapConfigSafe
    gmail: GmailConfigSafe
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
    path = _get_config_path()
    if not path.exists():
        return False
    try:
        cfg = load_config(path, resolve_password=False)
        token_path = (cfg.get("gmail") or {}).get("token_path", "token.json")
        tp = Path(token_path)
        if not tp.is_absolute():
            tp = _config_dir() / token_path
        return tp.exists()
    except Exception:
        return False


def _gmail_email() -> str | None:
    """Return the Gmail address for the current token, or None if not connected or error."""
    if not _gmail_connected():
        return None
    try:
        cfg = load_config(_get_config_path(), resolve_password=False)
        gmail = cfg.get("gmail") or {}
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


def _imap_password_set() -> bool:
    path = _get_config_path()
    if not path.exists():
        return False
    try:
        cfg = load_config(path, resolve_password=False)
        imap = cfg.get("imap") or {}
        if imap.get("password") not in (None, ""):
            return True
        key = imap.get("password_env", "IMAP_PASSWORD")
        return bool(os.environ.get(key) or os.environ.get(f"{key}_ENC"))
    except Exception:
        return False


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
    return ConfigResponse(
        imap=ImapConfigSafe(
            host="",
            port=993,
            username="",
            mailbox="INBOX",
            use_ssl=True,
            delete_after_import=True,
            since_date=None,
        ),
        gmail=GmailConfigSafe(
            use_label=False,
            label="ISP Mail",
            credentials_path="credentials.json",
            token_path="token.json",
        ),
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
    imap = cfg.get("imap") or {}
    gmail = cfg.get("gmail") or {}
    ui = cfg.get("ui") or {}
    state = cfg.get("state") or {}
    cred_path = gmail.get("credentials_path", "credentials.json")
    token_path = gmail.get("token_path", "token.json")
    return ConfigResponse(
        imap=ImapConfigSafe(
            host=imap.get("host", ""),
            port=int(imap.get("port", 993)),
            username=imap.get("username", ""),
            mailbox=imap.get("mailbox", "INBOX"),
            use_ssl=imap.get("use_ssl", True),
            delete_after_import=imap.get("delete_after_import", True),
            since_date=imap.get("since_date"),
        ),
        gmail=GmailConfigSafe(
            use_label=gmail.get("use_label") if "use_label" in gmail else bool((gmail.get("label") or "").strip()),
            label=gmail.get("label", "ISP Mail"),
            credentials_path=cred_path,
            token_path=token_path,
        ),
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


class ConfigUpdate(BaseModel):
    imap_host: str | None = None
    imap_port: int | None = None
    imap_username: str | None = None
    imap_mailbox: str | None = None
    imap_use_ssl: bool | None = None
    imap_since_date: str | None = None
    imap_password: str | None = None  # stored in .env, not in config
    delete_after_import: bool | None = None
    gmail_use_label: bool | None = None
    gmail_label: str | None = None
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
        except Exception as e:
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
        "imap": {
            "host": body.imap_host,
            "port": body.imap_port,
            "username": body.imap_username,
            "password_env": "IMAP_PASSWORD",
            "mailbox": body.imap_mailbox,
            "since_date": body.imap_since_date,
            "use_ssl": body.imap_use_ssl,
            "delete_after_import": body.delete_after_import,
        },
        "gmail": {
            "use_label": body.gmail_use_label,
            "label": body.gmail_label,
            "credentials_path": body.credentials_path,
            "token_path": body.token_path,
        },
        "state": {"db_path": body.state_db_path},
        "ui": {"host": "127.0.0.1", "port": 8765},
        "poll_interval_minutes": 5,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    set_encrypted_env(_config_dir(), "IMAP_PASSWORD", body.imap_password)
    # Reload env so next request sees it
    from dotenv import load_dotenv
    load_dotenv(_config_dir() / ".env")
    return {"status": "ok", "message": "Config created. Connect Gmail next."}


@app.put("/api/config")
def api_config_update(request: Request, update: ConfigUpdate) -> dict[str, str]:
    """Update config file. If imap_password provided, verify IMAP login then store in .env."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    path = _get_config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="config.json not found")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    imap = cfg.setdefault("imap", {})
    if update.imap_password is not None:
        host = update.imap_host if update.imap_host is not None else imap.get("host", "")
        port = int(update.imap_port if update.imap_port is not None else imap.get("port", 993))
        username = update.imap_username if update.imap_username is not None else imap.get("username", "")
        mailbox = update.imap_mailbox if update.imap_mailbox is not None else imap.get("mailbox", "INBOX")
        use_ssl = update.imap_use_ssl if update.imap_use_ssl is not None else imap.get("use_ssl", True)
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
        imap["password_env"] = "IMAP_PASSWORD"
        set_encrypted_env(_config_dir(), "IMAP_PASSWORD", update.imap_password)
        from dotenv import load_dotenv
        load_dotenv(_config_dir() / ".env")
    if update.imap_host is not None:
        imap["host"] = update.imap_host
    if update.imap_port is not None:
        imap["port"] = update.imap_port
    if update.imap_username is not None:
        imap["username"] = update.imap_username
    if update.imap_mailbox is not None:
        imap["mailbox"] = update.imap_mailbox
    if update.imap_since_date is not None:
        try:
            if update.imap_since_date != "":
                datetime.date.fromisoformat(update.imap_since_date)
        except Exception:
            raise HTTPException(status_code=400, detail="since_date must be YYYY-MM-DD")
        cfg.setdefault("imap", {})["since_date"] = update.imap_since_date
    if update.imap_use_ssl is not None:
        imap["use_ssl"] = update.imap_use_ssl
    if update.delete_after_import is not None:
        cfg.setdefault("imap", {})["delete_after_import"] = update.delete_after_import
    if update.gmail_use_label is not None:
        cfg.setdefault("gmail", {})["use_label"] = update.gmail_use_label
    if update.gmail_label is not None:
        cfg.setdefault("gmail", {})["label"] = update.gmail_label
    if update.poll_interval_minutes is not None:
        cfg["poll_interval_minutes"] = update.poll_interval_minutes
    if update.state_db_path is not None:
        cfg.setdefault("state", {})["db_path"] = update.state_db_path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return {"status": "ok"}


class CopyAllBody(BaseModel):
    """Body for copy-all: copy every message from ISP to Gmail (with optional delete after)."""
    delete_after: bool = False


@app.post("/api/fetch")
def api_fetch(request: Request, dry_run: bool = False) -> dict[str, Any]:
    """Trigger one fetch cycle. Optionally dry_run."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    setup_logging()
    install_log_buffer()
    try:
        result = run_once(config_path=str(_get_config_path()), dry_run=dry_run)
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/fetch/copy-all")
def api_fetch_copy_all(request: Request, body: CopyAllBody) -> dict[str, Any]:
    """Copy all emails from ISP mailbox to Gmail (not just new). Skips already in Gmail. Optionally delete from ISP after."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    setup_logging()
    install_log_buffer()
    try:
        with _copy_all_lock:
            result = run_copy_all(
                config_path=str(_get_config_path()),
                delete_after_import=body.delete_after,
                dry_run=False,
            )
        return result
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
    """Last fetch time and basic status."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        cfg = load_config(_get_config_path(), resolve_password=False)
        from .state import StateStore
        state_cfg = cfg.get("state", {})
        db_path = state_cfg.get("db_path", "state.db")
        mailbox = (cfg.get("imap") or {}).get("mailbox", "INBOX")
        state = StateStore(db_path)
        state.connect()
        row = state.get_last_fetch_time_any(mailbox)
        state.close()
        if row:
            return {"last_fetch_time": row[0], "uid_validity": row[1]}
        return {"last_fetch_time": None, "uid_validity": None}
    except Exception:
        return {"last_fetch_time": None, "error": "could not read state"}


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
      <label>Only fetch mail newer than <input id="s_imap_since_date" type="date"></label>
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
    <p class="hint" style="font-size:0.85rem; color:#666;">Token is from <code>fetch2gmail auth</code> (CLI). To switch accounts, run <code>fetch2gmail auth</code> again and replace token.json.</p>
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
    <h2>Config</h2>
    <label>IMAP host <input id="imap_host" type="text"></label>
    <label>IMAP port <input id="imap_port" type="number"></label>
    <label style="display:inline-block; margin-top:0.25rem;"><input type="checkbox" id="imap_use_ssl"> Use SSL/TLS for IMAP</label>
    <p class="hint" style="font-size:0.85rem; color:#666; margin-top:0;">Uncheck for plain IMAP (e.g. port 143).</p>
    <label>IMAP username <input id="imap_username" type="text"></label>
    <label>IMAP password <input id="imap_password" type="password" placeholder="Leave blank to keep current (stored encrypted)"></label>
    <label>Mailbox <input id="imap_mailbox" type="text" title="IMAP folder to fetch from. INBOX = main inbox."></label>
    <p class="hint" style="font-size:0.85rem; color:#666; margin-top:0;">Mailbox is the IMAP folder to fetch from. <strong>INBOX</strong> = main inbox where new mail arrives.</p>
    <label>Only fetch mail newer than <input id="imap_since_date" type="date"></label>
    <label style="margin-top:1rem;"><input type="checkbox" id="delete_after_import"> Delete emails from ISP after importing to Gmail</label>
    <label><input type="checkbox" id="gmail_use_label"> Add a Gmail label to imported mail</label>
    <div id="gmail_label_row" style="display:none;"><label>Label name <input id="gmail_label" type="text" placeholder="e.g. ISP Mail"></label></div>
    <label>Poll interval (minutes) <input id="poll_interval" type="number"></label>
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
      document.getElementById('imap_host').value = c.imap.host;
      document.getElementById('imap_port').value = c.imap.port;
      document.getElementById('imap_use_ssl').checked = c.imap.use_ssl;
      document.getElementById('imap_username').value = c.imap.username;
      document.getElementById('imap_mailbox').value = c.imap.mailbox;
      document.getElementById('imap_since_date').value = c.imap.since_date || '';
      document.getElementById('delete_after_import').checked = c.imap.delete_after_import;
      document.getElementById('gmail_use_label').checked = c.gmail.use_label;
      document.getElementById('gmail_label_row').style.display = c.gmail.use_label ? 'block' : 'none';
      document.getElementById('gmail_label').value = c.gmail.label;
      document.getElementById('poll_interval').value = c.poll_interval_minutes;
      document.getElementById('state_db').value = c.state_db_path;
      document.getElementById('gmailStatus').innerHTML = c.gmail_connected ? '<span class="ok">Gmail connected</span>' : '<span class="error">Not connected (add token.json from fetch2gmail auth)</span>';
      document.getElementById('gmailEmail').textContent = '';
      if (c.gmail_connected) {
        api('/api/gmail/email').then(function(d) { if (d.email) document.getElementById('gmailEmail').textContent = 'Connected as ' + d.email; }).catch(function() {});
      }
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
      document.getElementById('status').textContent = 'Last fetch: ' + (s.last_fetch_time || 'never');
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
    var body = {
      imap_host: document.getElementById('imap_host').value,
      imap_port: parseInt(document.getElementById('imap_port').value, 10),
      imap_use_ssl: document.getElementById('imap_use_ssl').checked,
      imap_username: document.getElementById('imap_username').value,
      imap_mailbox: document.getElementById('imap_mailbox').value,
      imap_since_date: document.getElementById('imap_since_date').value || null,
      delete_after_import: document.getElementById('delete_after_import').checked,
      gmail_use_label: document.getElementById('gmail_use_label').checked,
      gmail_label: document.getElementById('gmail_label').value,
      poll_interval_minutes: parseInt(document.getElementById('poll_interval').value, 10),
      state_db_path: document.getElementById('state_db').value
    };
    var pw = document.getElementById('imap_password').value;
    if (pw) body.imap_password = pw;
    put('/api/config', body).then(function() {
      document.getElementById('status').textContent = 'Config saved.';
      document.getElementById('status').className = 'status';
      if (pw) document.getElementById('imap_password').value = '';
    }).catch(function(e) {
      document.getElementById('status').textContent = 'Error: ' + (typeof e.detail === 'string' ? e.detail : (e.detail && e.detail.detail) || e.message || 'Save failed');
      document.getElementById('status').className = 'error';
    });
  };
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
