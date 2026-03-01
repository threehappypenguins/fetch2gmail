"""
Lightweight FastAPI web UI: localhost only by default. OAuth only (no username/password).
- credentials.json required first; landing page with "Sign in with Google" button.
- After sign-in, configure ISP email (setup wizard or dashboard).
- Optional: run `fetch2gmail set-ui-password` to store a hashed UI password in .ui_auth (no plain text); then the UI requires HTTP Basic Auth (e.g. when using --host 0.0.0.0).
"""

import base64
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Request, Response
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response as RawResponse
from pydantic import BaseModel

from .auth_ui import (
    auth_required,
    clear_session_cookie,
    set_session_cookie,
    verify_request,
)
from .config import get_config_path, load_config
from .ui_auth import load_ui_auth, verify_ui_auth
from .env_file import set_encrypted_env
from .gmail_client import get_gmail_service
from .log_buffer import get_recent_logs, install_log_buffer
from .run import run_once, setup_logging

logger = logging.getLogger(__name__)


def _poller_loop(stop_event: threading.Event) -> None:
    """Background thread: every poll_interval_minutes run a fetch when config exists."""
    first_run = True
    while not stop_event.is_set():
        try:
            if not _config_exists():
                stop_event.wait(timeout=60)
                continue
            path = _get_config_path()
            cfg = load_config(path, resolve_password=False)
            interval_minutes = max(1, int(cfg.get("poll_interval_minutes", 5)))
        except Exception as e:
            logger.warning("Poller could not load config: %s", e)
            stop_event.wait(timeout=60)
            continue
        # First run: short delay (30s) so we fetch soon after startup; then use full interval
        wait_seconds = 30 if first_run else interval_minutes * 60
        first_run = False
        waited = 0
        while waited < wait_seconds and not stop_event.is_set():
            stop_event.wait(timeout=10)
            waited += 10
        if stop_event.is_set():
            break
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


@app.middleware("http")
async def _optional_basic_auth(request: Request, call_next):
    """If .ui_auth exists in config dir (hashed password), require HTTP Basic Auth for all requests."""
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
                return await call_next(request)
        except Exception:
            pass
    from starlette.responses import Response
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": "Basic realm=\"Fetch2Gmail\""},
        content="Authentication required",
    )


# In-memory state for OAuth (state param -> valid). Single process only.
_oauth_states: dict[str, bool] = {}


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


def _require_auth(request: Request) -> bool:
    """True if request is authenticated: UI password (Basic Auth already passed), or Google session, or no auth required."""
    cfg_dir = _config_dir_safe()
    if load_ui_auth(cfg_dir):
        return True  # UI password mode: they passed Basic Auth in the middleware
    return verify_request(request, cfg_dir, _config_exists())


# Sanitized config for UI (no passwords, no token paths)
class ImapConfigSafe(BaseModel):
    host: str
    port: int
    username: str
    mailbox: str
    use_ssl: bool
    delete_after_import: bool


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


@app.get("/static/app.js", response_model=None)
def static_app_js():
    """Serve app script as plain JS to avoid HTML-embedded script encoding issues."""
    return RawResponse(content=_APP_JS.strip(), media_type="application/javascript")


@app.get("/api/setup/status")
def api_setup_status() -> dict[str, bool]:
    """Whether credentials.json and config exist (no auth). So UI can show 'Add credentials first'."""
    cfg_dir = _config_dir_safe()
    cred_exists = bool(cfg_dir and (cfg_dir / "credentials.json").exists())
    return {"credentials_exist": cred_exists, "config_exists": _config_exists()}


@app.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(request: Request) -> RedirectResponse:
    """Redirect to Google sign-in only when UI password not used."""
    cfg_dir = _config_dir_safe()
    if load_ui_auth(cfg_dir):
        return RedirectResponse(url="/", status_code=302)  # UI password mode: no Google needed
    exists = _config_exists()
    if not auth_required(cfg_dir, exists):
        return RedirectResponse(url="/", status_code=302)
    if verify_request(request, cfg_dir, exists):
        return RedirectResponse(url="/", status_code=302)
    return RedirectResponse(url="/auth/gmail", status_code=302)


@app.get("/api/auth/required")
def api_auth_required() -> dict[str, Any]:
    """Whether UI must show Google sign-in. False when .ui_auth is used (Basic Auth is the gate)."""
    cfg_dir = _config_dir_safe()
    if load_ui_auth(cfg_dir):
        return {"auth_required": False}
    return {"auth_required": auth_required(cfg_dir, _config_exists())}


@app.get("/api/auth/session")
def api_auth_session(request: Request) -> dict[str, bool]:
    """Whether the current request is logged in (UI password passed, or Google session, or no auth)."""
    cfg_dir = _config_dir_safe()
    if load_ui_auth(cfg_dir):
        return {"logged_in": True}
    return {"logged_in": verify_request(request, cfg_dir, _config_exists())}


@app.post("/api/logout")
def api_logout():
    resp = RedirectResponse(url="/login", status_code=302)
    clear_session_cookie(resp)
    return resp


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


@app.get("/api/gmail/email")
def api_gmail_email(request: Request) -> dict[str, str | None]:
    """Return the Gmail address currently connected (for UI warning when reconnecting)."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"email": _gmail_email()}


@app.get("/api/config", response_model=ConfigResponse)
def api_config(request: Request) -> ConfigResponse:
    """Return non-sensitive config for display/editing (no password resolution)."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        cfg = load_config(_get_config_path(), resolve_password=False)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="config.json not found")
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
    )


class ConfigUpdate(BaseModel):
    imap_host: str | None = None
    imap_port: int | None = None
    imap_username: str | None = None
    imap_mailbox: str | None = None
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
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "imap": {
            "host": body.imap_host,
            "port": body.imap_port,
            "username": body.imap_username,
            "password_env": "IMAP_PASSWORD",
            "mailbox": body.imap_mailbox,
            "use_ssl": True,
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
    """Update config file. If imap_password provided, store in .env."""
    if not _require_auth(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    path = _get_config_path()
    if not path.exists():
        raise HTTPException(status_code=404, detail="config.json not found")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if update.imap_host is not None:
        cfg.setdefault("imap", {})["host"] = update.imap_host
    if update.imap_port is not None:
        cfg.setdefault("imap", {})["port"] = update.imap_port
    if update.imap_username is not None:
        cfg.setdefault("imap", {})["username"] = update.imap_username
    if update.imap_mailbox is not None:
        cfg.setdefault("imap", {})["mailbox"] = update.imap_mailbox
    if update.imap_password is not None:
        cfg.setdefault("imap", {})["password_env"] = "IMAP_PASSWORD"
        set_encrypted_env(_config_dir(), "IMAP_PASSWORD", update.imap_password)
        from dotenv import load_dotenv
        load_dotenv(_config_dir() / ".env")
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


@app.get("/auth/gmail")
def auth_gmail_start(request: Request):
    """Redirect to Google OAuth. Works with or without config (uses config dir / cwd for credentials)."""
    cfg_dir = _config_dir()
    cred_path = cfg_dir / "credentials.json"
    if not cred_path.exists():
        return RedirectResponse(url="/?error=no_credentials", status_code=302)
    path = _get_config_path()
    if path.exists():
        cfg = load_config(path, resolve_password=False)
        gmail = cfg.get("gmail") or {}
        cred_path = Path(gmail.get("credentials_path", "credentials.json"))
        if not cred_path.is_absolute():
            cred_path = cfg_dir / cred_path
        if not cred_path.exists():
            return RedirectResponse(url="/?error=no_credentials", status_code=302)
    from .gmail_client import SCOPES
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/auth/gmail/callback"
    flow.redirect_uri = redirect_uri
    state = secrets.token_urlsafe(32)
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    _oauth_states[state] = flow.code_verifier
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/auth/gmail/callback")
def auth_gmail_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """Exchange code for tokens, save token.json, redirect to /. Works with or without config."""
    if error:
        return RedirectResponse(url=f"/?error=gmail_denied&detail={error}", status_code=302)
    if not code or not state:
        return RedirectResponse(url="/?error=invalid_callback", status_code=302)
    code_verifier = _oauth_states.pop(state, None)
    if not code_verifier:
        return RedirectResponse(url="/?error=invalid_callback", status_code=302)
    cfg_dir = _config_dir()
    cred_path = cfg_dir / "credentials.json"
    token_path = cfg_dir / "token.json"
    path = _get_config_path()
    if path.exists():
        cfg = load_config(path, resolve_password=False)
        gmail = cfg.get("gmail") or {}
        cred_path = Path(gmail.get("credentials_path", "credentials.json"))
        token_path = Path(gmail.get("token_path", "token.json"))
        if not cred_path.is_absolute():
            cred_path = cfg_dir / cred_path
        if not token_path.is_absolute():
            token_path = cfg_dir / token_path
    from .gmail_client import SCOPES
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(
        str(cred_path), SCOPES, code_verifier=code_verifier
    )
    callback_base = str(request.base_url).rstrip("/")
    if callback_base.endswith("/auth/gmail/callback"):
        callback_base = callback_base[: -len("/auth/gmail/callback")].rstrip("/")
    flow.redirect_uri = f"{callback_base}/auth/gmail/callback"
    flow.fetch_token(code=code)
    creds = flow.credentials
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    resp = RedirectResponse(url="/?gmail=connected", status_code=302)
    set_session_cookie(resp, _config_dir_safe())
    return resp


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
    .logout { float: right; font-size: 0.9rem; }
  </style>
</head>
<body>
  <h1>Fetch2Gmail <span class="logout" id="logoutSpan" style="display:none"><form method="post" action="/api/logout" style="display:inline"><button type="submit">Log out</button></form></span></h1>
  <p id="subtitle">IMAP to Gmail import. Configure below.</p>
  <p id="loadingMsg">Loading...</p>

  <div id="credentialsFirst" style="display:none">
    <section>
      <h2>Add credentials to get started</h2>
      <p>Place <strong>credentials.json</strong> in this app&apos;s folder (the same folder you ran <code>fetch2gmail serve</code> from).</p>
      <p>Get it from <strong>Google Cloud Console</strong>: create a project, enable Gmail API, configure the OAuth consent screen, create a <strong>Web application</strong> OAuth client, add the redirect URI <code>http://127.0.0.1:8765/auth/gmail/callback</code>, then download the JSON and save it as <strong>credentials.json</strong>.</p>
      <p>After you add the file, <a href="/">refresh this page</a> to continue.</p>
    </section>
  </div>

  <div id="landing" style="display:none">
    <section>
      <p><a href="/auth/gmail" id="btnSignIn" style="display:inline-block; padding:0.5rem 1rem; background:#1a73e8; color:white; text-decoration:none; border-radius:4px; font-weight:500;">Sign in with Google</a></p>
    </section>
  </div>

  <div id="setupWizard">
    <section>
      <h2>Configure your ISP email</h2>
      <p>Enter your IMAP (ISP) mailbox details. Your password is stored encrypted in <code>.env</code> (not plain text), not in the config file.</p>
      <label>IMAP host <input id="s_imap_host" type="text" placeholder="imap.example.com"></label>
      <label>IMAP port <input id="s_imap_port" type="number" value="993"></label>
      <label>IMAP username (email) <input id="s_imap_username" type="text" placeholder="you@isp.com"></label>
      <label>IMAP password <input id="s_imap_password" type="password" placeholder="Stored encrypted in .env"></label>
      <label>Mailbox <input id="s_imap_mailbox" type="text" value="INBOX" title="IMAP folder to fetch from. INBOX is the main inbox where new mail arrives."></label>
      <p class="hint" style="font-size:0.85rem; color:#666; margin-top:0;">Mailbox is the IMAP folder to fetch from. <strong>INBOX</strong> is the main inbox where new mail arrives at your ISP; leave as INBOX unless you use a different folder.</p>
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
  </section>

  <section>
    <h2>Gmail</h2>
    <p id="gmailStatus"></p>
    <p id="gmailEmail" class="status" style="font-size:0.9rem"></p>
    <button type="button" id="btnConnectGmail" style="display:none">Connect Gmail (OAuth)</button>
  </section>

  <section>
    <h2>Config</h2>
    <label>IMAP host <input id="imap_host" type="text"></label>
    <label>IMAP port <input id="imap_port" type="number"></label>
    <label>IMAP username <input id="imap_username" type="text"></label>
    <label>IMAP password <input id="imap_password" type="password" placeholder="Leave blank to keep current (stored encrypted)"></label>
    <label>Mailbox <input id="imap_mailbox" type="text" title="IMAP folder to fetch from. INBOX = main inbox."></label>
    <p class="hint" style="font-size:0.85rem; color:#666; margin-top:0;">Mailbox is the IMAP folder to fetch from. <strong>INBOX</strong> = main inbox where new mail arrives.</p>
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
  const api = function(path) { return fetch(path).then(function(r) { if (r.status === 401) { location.href = '/login'; throw new Error('Unauthorized'); } return r.ok ? r.json() : r.json().then(function(j) { return Promise.reject(j); }); }); };
  const put = function(path, body) { return fetch(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(function(r) { if (r.status === 401) { location.href = '/login'; throw new Error('Unauthorized'); } return r.json(); }); };

  fetch('/api/setup/status').then(function(r) { return r.json(); }).then(function(st) {
    if (!st.credentials_exist) {
      document.getElementById('loadingMsg').style.display = 'none';
      document.getElementById('credentialsFirst').style.display = 'block';
      return;
    }
    fetch('/api/auth/session').then(function(r) { return r.json(); }).then(function(session) {
      if (!session.logged_in) {
        document.getElementById('loadingMsg').style.display = 'none';
        document.getElementById('subtitle').textContent = 'Import mail from your ISP mailbox into Gmail. Sign in with Google to connect your account and get started.';
        document.getElementById('landing').style.display = 'block';
        return;
      }
      document.getElementById('logoutSpan').style.display = 'inline';
      loadConfig().then(function(c) { if (c) { loadStatus(); loadLogs(); } });
    }).catch(function() {
      document.getElementById('loadingMsg').style.display = 'none';
      document.getElementById('subtitle').textContent = 'Import mail from your ISP mailbox into Gmail. Sign in with Google to connect your account and get started.';
      document.getElementById('landing').style.display = 'block';
    });
  }).catch(function() { document.getElementById('loadingMsg').style.display = 'none'; });
  var params = new URLSearchParams(location.search);
  if (params.get('gmail') === 'connected') {
    document.getElementById('subtitle').textContent = 'Gmail connected. You can run a fetch now.';
    history.replaceState({}, '', '/');
  }
  if (params.get('error') === 'no_credentials') document.getElementById('subtitle').innerHTML = '<span class="error">Put credentials.json in the app folder and try Connect Gmail again.</span>';

  function hideLoading() { var el = document.getElementById('loadingMsg'); if (el) el.style.display = 'none'; }
  function loadConfig() {
    return api('/api/config').then(function(c) {
      hideLoading();
      document.getElementById('setupWizard').style.display = 'none';
      document.getElementById('dashboard').style.display = 'block';
      document.getElementById('imap_host').value = c.imap.host;
      document.getElementById('imap_port').value = c.imap.port;
      document.getElementById('imap_username').value = c.imap.username;
      document.getElementById('imap_mailbox').value = c.imap.mailbox;
      document.getElementById('delete_after_import').checked = c.imap.delete_after_import;
      document.getElementById('gmail_use_label').checked = c.gmail.use_label;
      document.getElementById('gmail_label_row').style.display = c.gmail.use_label ? 'block' : 'none';
      document.getElementById('gmail_label').value = c.gmail.label;
      document.getElementById('poll_interval').value = c.poll_interval_minutes;
      document.getElementById('state_db').value = c.state_db_path;
      document.getElementById('gmailStatus').innerHTML = c.gmail_connected ? '<span class="ok">Gmail connected</span>' : '<span class="error">Not connected</span>';
      document.getElementById('btnConnectGmail').style.display = 'inline-block';
      document.getElementById('btnConnectGmail').textContent = c.gmail_connected ? 'Reconnect Gmail (switch account)' : 'Connect Gmail (OAuth)';
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
  document.getElementById('btnConnectGmail').onclick = function() {
    var btn = document.getElementById('btnConnectGmail');
    if (btn.textContent.indexOf('Reconnect') === 0) {
      var emailEl = document.getElementById('gmailEmail');
      var msg = emailEl.textContent ? 'Reconnecting will replace the current Gmail account (' + emailEl.textContent.replace('Connected as ', '') + ') with the one you sign in with. Continue?' : 'Reconnecting will replace the current Gmail account. Continue?';
      if (!confirm(msg)) return;
    }
    window.location.href = '/auth/gmail';
  };
  document.getElementById('gmail_use_label').onchange = function() {
    document.getElementById('gmail_label_row').style.display = this.checked ? 'block' : 'none';
  };
  document.getElementById('btnSave').onclick = function() {
    var body = {
      imap_host: document.getElementById('imap_host').value,
      imap_port: parseInt(document.getElementById('imap_port').value, 10),
      imap_username: document.getElementById('imap_username').value,
      imap_mailbox: document.getElementById('imap_mailbox').value,
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
        imap_username: document.getElementById('s_imap_username').value,
        imap_password: document.getElementById('s_imap_password').value,
        imap_mailbox: document.getElementById('s_imap_mailbox').value,
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
