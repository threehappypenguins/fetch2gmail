"""
UI Basic Auth: store only a bcrypt hash of the password in a file. No plain-text password on disk.
File: .ui_auth in the config directory. Format: {"username": "...", "password_hash": "..."}.
"""

import json
from pathlib import Path

UI_AUTH_FILENAME = ".ui_auth"


def _get_ui_auth_path(config_dir: Path) -> Path:
    return config_dir / UI_AUTH_FILENAME


def create_ui_auth(config_dir: Path, username: str, password: str) -> None:
    """Store username and bcrypt hash of password in .ui_auth. Overwrites if exists."""
    import bcrypt
    config_dir = Path(config_dir).resolve()
    config_dir.mkdir(parents=True, exist_ok=True)
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    path = _get_ui_auth_path(config_dir)
    path.write_text(json.dumps({"username": username, "password_hash": password_hash}), encoding="utf-8")
    path.chmod(0o600)


def load_ui_auth(config_dir: Path | None) -> tuple[str, str] | None:
    """Return (username, password_hash) if .ui_auth exists and is valid, else None."""
    if not config_dir:
        return None
    path = _get_ui_auth_path(Path(config_dir).resolve())
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        u = data.get("username")
        h = data.get("password_hash")
        if u and h:
            return (u, h)
    except Exception:
        pass
    return None


def verify_ui_auth(config_dir: Path | None, username: str, password: str) -> bool:
    """Return True if username matches and password verifies against the stored hash."""
    loaded = load_ui_auth(config_dir)
    if not loaded:
        return False
    stored_user, stored_hash = loaded
    if username != stored_user:
        return False
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("ascii"))
    except Exception:
        return False
