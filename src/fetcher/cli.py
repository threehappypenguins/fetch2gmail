"""
Interactive CLI for Fetch2Gmail: config wizard and one-shot / dry-run.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import get_config_path, load_config
from .run import run_once, setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fetch2gmail",
        description="Fetch mail from IMAP and import into Gmail (users.messages.import).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        help="Path to config.json (default: FETCH2GMAIL_CONFIG or ./config.json)",
    )
    sub = parser.add_subparsers(dest="command", help="Commands")

    # Run once (used by timer / UI trigger)
    p_run = sub.add_parser("run", help="Run one fetch cycle")
    p_run.add_argument("--dry-run", action="store_true", help="Fetch from ISP only; simulate import, do not delete")
    p_run.set_defaults(func=_cmd_run)

    # Config wizard (non-interactive: create example; interactive: prompt for values)
    p_config = sub.add_parser("config", help="Create or validate config")
    p_config.add_argument("--init", action="store_true", help="Create config.json from template")
    p_config.add_argument("--validate", action="store_true", help="Validate existing config.json")
    p_config.set_defaults(func=_cmd_config)

    p_wizard = sub.add_parser("wizard", help="Interactive config wizard (prompt for IMAP, Gmail paths, etc.)")
    p_wizard.set_defaults(func=lambda a: config_wizard_interactive())

    p_serve = sub.add_parser("serve", help="Run web UI")
    p_serve.add_argument("--host", default="127.0.0.1", help="Bind host (use 0.0.0.0 to allow LAN access, e.g. from phone)")
    p_serve.add_argument("--port", type=int, default=8765, help="Bind port")
    p_serve.set_defaults(func=_cmd_serve)

    p_auth = sub.add_parser(
        "auth",
        help="Get Gmail token (for headless setup). Run on a machine with a browser, then copy token.json to Odroid.",
    )
    p_auth.add_argument(
        "--credentials",
        default="credentials.json",
        help="Path to credentials.json from GCP (default: credentials.json in current directory)",
    )
    p_auth.add_argument(
        "--token",
        default="token.json",
        help="Where to save token.json (default: token.json in current directory)",
    )
    p_auth.add_argument("--port", type=int, default=8765, help="Port for local OAuth callback (default: 8765)")
    p_auth.set_defaults(func=_cmd_auth)

    p_set_ui_password = sub.add_parser(
        "set-ui-password",
        help="Set a username and password for the web UI (stored as a hash in .ui_auth; use when exposing UI with --host 0.0.0.0)",
    )
    p_set_ui_password.set_defaults(func=_cmd_set_ui_password)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)
    args.func(args)


def _cmd_run(args: argparse.Namespace) -> None:
    setup_logging()
    config_path = args.config or str(get_config_path())
    try:
        result = run_once(config_path=config_path, dry_run=args.dry_run)
    except FileNotFoundError as e:
        print(f"Config not found: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)
    if result.get("error"):
        print(f"Run error: {result['error']}", file=sys.stderr)
        sys.exit(1)
    print(
        f"Imported: {result['imported']}, Skipped (duplicate): {result['skipped_duplicate']}, "
        f"Deleted from ISP: {result['deleted']}"
    )
    if result.get("last_fetch_time"):
        print(f"Last fetch: {result['last_fetch_time']}")


def _cmd_config(args: argparse.Namespace) -> None:
    if args.init:
        dest = Path.cwd() / "config.json"
        if dest.exists():
            print(f"{dest} already exists; not overwriting.", file=sys.stderr)
            sys.exit(1)
        # Prefer repo/config.example.json, then cwd
        example = Path(__file__).resolve().parents[2] / "config.example.json"
        if not example.exists():
            example = Path.cwd() / "config.example.json"
        if example.exists():
            import shutil
            shutil.copy(example, dest)
            print(f"Created {dest}. Edit and set IMAP password via environment (e.g. IMAP_PASSWORD).")
        else:
            # Embedded fallback
            _write_default_config(dest)
            print(f"Created {dest}. Edit and set IMAP password via environment (e.g. IMAP_PASSWORD).")
        return
    if args.validate:
        config_path = args.config or str(get_config_path())
        try:
            load_config(config_path)
            print("Config OK.")
        except Exception as e:
            print(f"Invalid config: {e}", file=sys.stderr)
            sys.exit(1)
        return
    # No --init or --validate: show path and hint
    print(f"Config path: {get_config_path()}")
    print("Use --init to create config.json, --validate to check existing config.")


def _cmd_serve(args: argparse.Namespace) -> None:
    from .web_ui import serve
    serve(host=args.host, port=args.port, config_path=args.config)


def _cmd_set_ui_password(args: argparse.Namespace) -> None:
    """Prompt for username and password; store bcrypt hash in .ui_auth in config directory."""
    import getpass

    from .ui_auth import create_ui_auth

    config_path = Path(args.config or get_config_path()).resolve()
    config_dir = config_path.parent
    print(f"Storing UI auth in {config_dir / '.ui_auth'} (password is hashed, not stored in plain text).")
    username = input("Username: ").strip()
    if not username:
        print("Username cannot be empty.", file=sys.stderr)
        sys.exit(1)
    password = getpass.getpass("Password: ")
    if not password:
        print("Password cannot be empty.", file=sys.stderr)
        sys.exit(1)
    create_ui_auth(config_dir, username, password)
    print("Done. The web UI will now require this username and password when you run it (e.g. with --host 0.0.0.0).")


def _cmd_auth(args: argparse.Namespace) -> None:
    """Run minimal OAuth server to get token.json; then user copies it to headless device."""
    import threading
    import webbrowser

    from .auth_server import app

    cred_path = Path(args.credentials).resolve()
    token_path = Path(args.token).resolve()
    if not cred_path.exists():
        print(f"Credentials file not found: {cred_path}", file=sys.stderr)
        print("Download credentials.json from Google Cloud (OAuth client, Web application).", file=sys.stderr)
        sys.exit(1)

    os.environ["FETCH2GMAIL_AUTH_CREDENTIALS"] = str(cred_path)
    os.environ["FETCH2GMAIL_AUTH_TOKEN"] = str(token_path)

    url = f"http://127.0.0.1:{args.port}/"

    def open_browser() -> None:
        import time
        time.sleep(1.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()
    print(f"Opening {url} in your browser. Sign in with Google; token will be saved to {token_path}")
    print("Then copy credentials.json and token.json to your Odroid. Press Ctrl+C when done.")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def _write_default_config(dest: Path) -> None:
    """Write a default config.json to dest."""
    cfg = {
        "imap": {
            "host": "imap.example.com",
            "port": 993,
            "username": "your-isp-email@example.com",
            "password_env": "IMAP_PASSWORD",
            "mailbox": "INBOX",
            "use_ssl": True,
            "delete_after_import": True,
        },
        "gmail": {"use_label": False, "label": "ISP Mail", "credentials_path": "credentials.json", "token_path": "token.json"},
        "state": {"db_path": "state.db"},
        "ui": {"host": "127.0.0.1", "port": 8765},
        "poll_interval_minutes": 5,
    }
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def config_wizard_interactive() -> None:
    """Interactive wizard: prompt for IMAP host, user, etc., and write config.json."""
    print("Fetch2Gmail config wizard")
    host = input("IMAP host (e.g. imap.example.com): ").strip() or "imap.example.com"
    port = input("IMAP port [993]: ").strip() or "993"
    user = input("IMAP username (email): ").strip()
    mailbox = input("Mailbox [INBOX]: ").strip() or "INBOX"
    label = input("Gmail label for imported messages [ISP Mail]: ").strip() or "ISP Mail"
    db_path = input("State DB path [state.db]: ").strip() or "state.db"
    print("IMAP password: set via environment variable IMAP_PASSWORD (recommended) or password_env in config.")
    password_env = input("Environment variable for password [IMAP_PASSWORD]: ").strip() or "IMAP_PASSWORD"
    cred_path = input("Gmail OAuth credentials.json path [credentials.json]: ").strip() or "credentials.json"
    token_path = input("Gmail OAuth token path [token.json]: ").strip() or "token.json"
    cfg = {
        "imap": {
            "host": host,
            "port": int(port),
            "username": user,
            "password_env": password_env,
            "mailbox": mailbox,
            "use_ssl": True,
            "delete_after_import": True,
        },
        "gmail": {"use_label": bool(label), "label": label or "ISP Mail", "credentials_path": cred_path, "token_path": token_path},
        "state": {"db_path": db_path},
        "ui": {"host": "127.0.0.1", "port": 8765},
        "poll_interval_minutes": 5,
    }
    path = Path.cwd() / "config.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"Wrote {path}. Set {password_env} and run 'fetch2gmail run' or start the UI.")


if __name__ == "__main__":
    main()
