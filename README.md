# Fetch2Gmail

Self-hosted email fetcher: **IMAP (ISP mailbox) → Gmail API import**. Replaces Gmail’s deprecated POP3 fetch.

- Polls an ISP mailbox via **IMAPS** (port 993).
- Imports messages with **Gmail API** (not SMTP); applies a Gmail label, preserves headers and date.
- Deletes from ISP only after Gmail confirms import. **Idempotent** (UID + message hash).

**Requirements:** Python 3.11+, IMAP credentials, Google Cloud project with Gmail API and OAuth2 (Web application).

---

## Use case 1: Headless Debian server

You use a **computer with a browser** (to get the token) and a **Debian server** (Odroid, Raspberry Pi, etc.) where the app runs. Follow the steps in order.

### On a computer with a browser (Windows or Linux)

**Step 1. Get credentials.json from Google Cloud**

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create or select a project.
2. **APIs & Services → Library** → search “Gmail API” → **Enable**.
3. **OAuth consent screen**: External → add app name → **Scopes** → add `https://www.googleapis.com/auth/gmail.modify` → **Test users** → add the Gmail address that will receive the imported mail.
4. **Credentials → Create credentials → OAuth client ID** → Application type **Web application**.
5. **Authorized redirect URIs** → add `http://127.0.0.1:8765/auth/gmail/callback` and `http://localhost:8765/auth/gmail/callback`.
6. Create → download the JSON. Save it as **credentials.json** in a folder (e.g. Desktop or `~/fetch2gmail-auth`).

**Step 2. Install Python 3.11+ and pipx**

- **Windows:** Install [Python 3.11+](https://www.python.org/downloads/) (check “Add Python to PATH”). Open Command Prompt or PowerShell: `pip install pipx` then `pipx ensurepath`. Close and reopen the terminal.
- **Linux:** `sudo apt install pipx` then `pipx ensurepath`. Reopen the terminal (or run `source ~/.bashrc`) so `~/.local/bin` is on PATH.

**Step 3. Install fetch2gmail**

```bash
pipx install fetch2gmail
```

**Step 4. Get the token**

In the **same folder** where **credentials.json** is, run:

```bash
fetch2gmail auth
```

A browser opens. Sign in with the **Gmail account that will receive the imported mail** and click Allow. **token.json** is saved in that folder. Press **Ctrl+C** to stop the auth server.

**Step 5. Copy these two files to the server**

Copy **credentials.json** and **token.json** to your Debian server. You will put them in the app data directory in the next section.

---

### On the Debian server

**Step 6. Create the data directory and add the files**

Pick one directory for all app files (e.g. `/opt/fetch2gmail` or `/home/odroid/fetch2gmail`). Create it and put **credentials.json** and **token.json** there:

```bash
sudo mkdir -p /opt/fetch2gmail
sudo chown "$USER" /opt/fetch2gmail
# Copy credentials.json and token.json into /opt/fetch2gmail (from step 5)
```

Use the same path in the steps below (replace `/opt/fetch2gmail` and the username if you use something else).

**Step 7. Install pipx and fetch2gmail on the server**

Run these from **any directory** (you do not need to be in `/opt/fetch2gmail`):

```bash
sudo apt install pipx
pipx ensurepath
# Log out and back in (or source ~/.bashrc) so ~/.local/bin is on PATH
pipx install fetch2gmail
```

The app will find your token and files in `/opt/fetch2gmail` because in step 9 you install a single systemd service that sets **WorkingDirectory** and **FETCH2GMAIL_CONFIG** to that directory. For step 8 you **cd** into `/opt/fetch2gmail` before running `set-ui-password` so the password file is stored there.

**Step 8. Set the UI username and password**

Run once so only you can access the web UI (username and hashed password are stored in **.ui_auth**):

```bash
cd /opt/fetch2gmail
fetch2gmail set-ui-password
```

Enter a username and password when prompted.

**Step 9. Install and enable the systemd service (one service: web UI + background fetch)**

One service runs the web UI and polls your ISP mailbox on a schedule (every 5 minutes by default). Generate the unit file and install it:

```bash
fetch2gmail install-service --user YOUR_USER --dir /opt/fetch2gmail -o /tmp/fetch2gmail.service
sudo mv /tmp/fetch2gmail.service /etc/systemd/system/fetch2gmail.service
sudo systemctl daemon-reload
sudo systemctl enable fetch2gmail
sudo systemctl start fetch2gmail
```

Replace **YOUR_USER** with the user that owns `/opt/fetch2gmail` (e.g. `odroid`). Replace **/opt/fetch2gmail** if you used a different data directory. The command finds `fetch2gmail` on your PATH; if it is elsewhere, add `--exec /path/to/fetch2gmail`.

Alternatively, print the unit file and pipe it into place:

```bash
fetch2gmail install-service --user YOUR_USER --dir /opt/fetch2gmail | sudo tee /etc/systemd/system/fetch2gmail.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable fetch2gmail
sudo systemctl start fetch2gmail
```

**Step 10. Configure ISP mail in the web UI**

Open **http://\<server-ip\>:8765** in your browser (e.g. http://192.168.1.10:8765). Log in with the UI username and password from step 8. You will see the **initial setup** form. Enter your IMAP host, username, password, mailbox, and Gmail label, then click **Create config**. The app stores your password securely and will poll your ISP mailbox on a schedule (every 5 minutes by default). You do not need to edit any config file by hand.

**Done.** To watch logs: `journalctl -u fetch2gmail -f`.

---

## Use case 2: Device with a browser (one machine)

Everything runs on **one machine** (laptop or desktop) that has a browser. You sign in with Google in the web UI — the redirect works because you open the UI on localhost. You do **not** need to run `fetch2gmail auth` or set a UI password. Follow the steps in order.

**Step 1. Get credentials.json from Google Cloud**

Same as Use case 1, step 1. Save **credentials.json** in a folder you will use as the app data directory (e.g. `~/fetch2gmail`).

**Step 2. Install Python 3.11+ and pipx**

Same as Use case 1, step 2 (Windows or Linux).

**Step 3. Install fetch2gmail**

```bash
pipx install fetch2gmail
```

**Step 4. Create the data directory and add credentials**

Create the folder and put **credentials.json** there (you do not have a token yet):

```bash
mkdir -p ~/fetch2gmail
# Copy credentials.json into ~/fetch2gmail
```

Use the same path in the steps below (replace `~/fetch2gmail` if you use something else).

**Step 5. Install and enable the systemd service**

Same as Use case 1, step 9: one service (web UI + background fetch). Replace **YOUR_USER** and the path with yours:

```bash
fetch2gmail install-service --user YOUR_USER --dir /home/YOUR_USER/fetch2gmail | sudo tee /etc/systemd/system/fetch2gmail.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable fetch2gmail
sudo systemctl start fetch2gmail
```

**Step 6. Sign in with Google in the web UI**

Open **http://127.0.0.1:8765** in your browser (**use localhost** so the Google OAuth redirect works). You will see **Sign in with Google** — click it, sign in with the Gmail account that will receive the imported mail, and click Allow. The app saves **token.json** in your data directory. You are now signed in.

**Step 7. Configure ISP mail in the web UI**

Use the **initial setup** form to enter your IMAP host, username, password, mailbox, and Gmail label, then click **Create config**. The app stores your password securely and the timer runs the fetch on the schedule you set (every 5 minutes by default).

**Done.**

---

## Reference

### OAuth redirect URI

Your Google OAuth client must use **Web application** (not Desktop) and have these **Authorized redirect URIs**:

- `http://127.0.0.1:8765/auth/gmail/callback`
- `http://localhost:8765/auth/gmail/callback`

Google does not allow redirect URIs that use an IP address. That is why the token must be obtained on a machine where the app can use localhost (Use case 1 steps 1–4 on a PC; Use case 2 on the same machine).

### Poll interval

The app runs the fetch every 5 minutes by default (background poller in the same process as the web UI). You can change the interval in the web UI (Config) or in **config.json** (`poll_interval_minutes`).

### Data directory

All app files live in **one directory**: **config.json**, **credentials.json**, **token.json**, and optionally **.env** (IMAP password; the UI can store it for you). The app creates **state.db** and **.ui_auth** there. Set **WorkingDirectory** and **FETCH2GMAIL_CONFIG** to this directory in systemd.

### Security

- Do not commit **credentials.json**, **token.json**, or **config.json** with secrets. Restrict file permissions to the user running the service.
- On the server, the UI is protected by the username and password you set with **`fetch2gmail set-ui-password`** (stored as a hash in **.ui_auth**). There is no Google sign-in on the server when **token.json** is already there.
- The Gmail scope requested is **gmail.modify** (read and modify labels/messages only).

### CLI

| Command | Purpose |
|--------|---------|
| `fetch2gmail auth` | Get **token.json** on a machine with a browser (opens http://127.0.0.1:8765). |
| `fetch2gmail set-ui-password` | Set UI username and password (hash in **.ui_auth**). |
| `fetch2gmail install-service` | Generate systemd unit file (one service: web UI + background fetch). Use `--user`, `--dir`, and optionally `--output` or pipe to `sudo tee`. |
| `fetch2gmail serve` | Run the web UI (default: localhost only; use `--host 0.0.0.0` to bind to all interfaces). |
| `fetch2gmail run` | Run one fetch cycle. |
| `fetch2gmail run --dry-run` | Connect to ISP and show what would be imported; no Gmail import, no delete. |

### Uninstall

**Machine used only to get the token (no system service):** After copying **credentials.json** and **token.json** to the server, remove the app: `pipx uninstall fetch2gmail`. Reinstall with `pipx install fetch2gmail` if you need to run `fetch2gmail auth` again later.

**Machine where the app runs (system service + app):** To remove everything so you can reinstall or run from source and see your changes:

1. **Stop and disable the systemd service, then remove the unit file:**
   ```bash
   sudo systemctl stop fetch2gmail
   sudo systemctl disable fetch2gmail
   sudo rm /etc/systemd/system/fetch2gmail.service
   sudo systemctl daemon-reload
   ```
   (To confirm the service is gone: `systemctl status fetch2gmail` should report "not found".)

2. **Uninstall the app:**
   - **If you installed with pipx:** `pipx uninstall fetch2gmail`
   - **If you installed from source in a venv:** deactivate the venv (`deactivate`) and delete the project directory (e.g. `rm -rf ~/fetch2gmail`), or just use a different terminal and run from your dev clone with `pip install -e .` so the installed copy is no longer used.

3. **Optionally remove the data directory** (config, credentials, token, state, UI password file):
   ```bash
   rm -rf /opt/fetch2gmail
   ```
   Use the path you used as the data directory (e.g. `/opt/fetch2gmail` or `~/fetch2gmail`). Only do this if you no longer need the config or token; back them up first if you might reuse them.

**To test local changes:** Uninstall the system copy (steps 1 and 2 above), then from your git clone run `python3 -m venv .venv`, `source .venv/bin/activate`, `pip install -e .`. Use that terminal to run `fetch2gmail serve` (or reinstall the system service later with `fetch2gmail install-service` and point it at your data directory).

---

## Development / testing from source

To run from a git clone (e.g. to test changes before pushing):

1. **Clone and go into the repo:**
   ```bash
   git clone https://github.com/yourusername/fetch2gmail.git
   cd fetch2gmail
   ```

2. **Create and activate a virtual environment:**
   - **Linux / macOS:** `python3 -m venv .venv` then `source .venv/bin/activate`
   - **Windows (PowerShell):** `python -m venv .venv` then `.venv\Scripts\Activate.ps1`
   - **Windows (Command Prompt):** `python -m venv .venv` then `.venv\Scripts\activate.bat`

3. **Install the package in editable mode** (so changes in the repo are used when you run the app):
   ```bash
   pip install -e .
   ```
   Optional, for tests: `pip install -e ".[dev]"`

4. **Run and test** (use the same terminal with the venv active):
   - `fetch2gmail serve` — web UI at http://127.0.0.1:8765
   - `fetch2gmail auth` — get token (put **credentials.json** in the current directory first)
   - `fetch2gmail run` or `fetch2gmail run --dry-run` — if you have **config.json** and secrets set up

5. **Before pushing: test the build** so the package still builds and you catch errors:
   ```bash
   pip install build
   python -m build
   ```
   This creates `dist/` with the sdist and wheel. See **docs/PUBLISHING.md** for Test PyPI and release steps.

---

## License

MIT. See **LICENSE**.
