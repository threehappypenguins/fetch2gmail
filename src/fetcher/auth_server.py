"""
Minimal OAuth-only server for headless setup: get token.json on a machine with a browser, then copy to Odroid.
Run with: fetch2gmail auth [--credentials PATH] [--token PATH] [--port 8765]
Use 127.0.0.1 only so GCP redirect URI (http://127.0.0.1:8765/auth/gmail/callback) is valid.
"""

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .gmail_client import SCOPES

# Single-purpose app: no config, no session, just OAuth -> save token
app = FastAPI(
    title="Fetch2Gmail – get token",
    description="One-time Gmail OAuth to save token.json for use on a headless device.",
)

_oauth_states: dict[str, str] = {}


def _credentials_path() -> Path:
    p = os.environ.get("FETCH2GMAIL_AUTH_CREDENTIALS", "credentials.json")
    return Path(p).resolve()


def _token_path() -> Path:
    p = os.environ.get("FETCH2GMAIL_AUTH_TOKEN", "token.json")
    return Path(p).resolve()


@app.get("/", response_class=HTMLResponse)
def _index() -> str:
    return """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Fetch2Gmail – get token</title></head><body>
    <p>Redirecting to Google to sign in…</p>
    <script>location.href = '/auth/gmail';</script>
    <p><a href="/auth/gmail">Click here</a> if not redirected.</p>
    </body></html>"""


@app.get("/auth/gmail", response_model=None)
def _auth_start(request: Request) -> RedirectResponse:
    cred_path = _credentials_path()
    if not cred_path.exists():
        return RedirectResponse(
            url=f"/error?msg=credentials+not+found%3A+{cred_path}",
            status_code=302,
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
    base = str(request.base_url).rstrip("/")
    flow.redirect_uri = f"{base}/auth/gmail/callback"
    state = secrets.token_urlsafe(32)
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    _oauth_states[state] = flow.code_verifier
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/auth/gmail/callback", response_model=None)
def _auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse | HTMLResponse:
    if error:
        return RedirectResponse(url=f"/error?msg=gmail_denied%3A+{error}", status_code=302)
    if not code or not state:
        return RedirectResponse(url="/error?msg=invalid_callback", status_code=302)
    code_verifier = _oauth_states.pop(state, None)
    if not code_verifier:
        return RedirectResponse(url="/error?msg=invalid_callback", status_code=302)

    cred_path = _credentials_path()
    token_path = _token_path()
    if not cred_path.exists():
        return RedirectResponse(url="/error?msg=credentials+not+found", status_code=302)

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
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    cred_abs = cred_path.resolve()
    tok_abs = token_path.resolve()
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Token saved</title></head><body>
    <h2>Token saved</h2>
    <p><strong>token.json</strong> has been saved. Copy these two files to your Odroid (or other headless device):</p>
    <ul>
      <li><strong>credentials.json</strong> (from GCP)</li>
      <li><strong>token.json</strong> (just saved at <code>{tok_abs}</code>)</li>
    </ul>
    <p>Put them in the same folder where Fetch2Gmail runs on the device (e.g. <code>/data</code> in Docker).</p>
    <p>You can close this window and stop the server (Ctrl+C).</p>
    </body></html>"""
    return HTMLResponse(html)


@app.get("/error", response_class=HTMLResponse)
def _error(msg: str = "") -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Error</title></head><body>
    <h2>Error</h2>
    <p>{msg or "Unknown error"}</p>
    <p>Make sure <strong>credentials.json</strong> is in the current folder (or set FETCH2GMAIL_AUTH_CREDENTIALS).</p>
    </body></html>"""
