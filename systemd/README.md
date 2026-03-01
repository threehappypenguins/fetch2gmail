# systemd setup

Standard setup: **UI service** + **timer**.

- **fetch2gmail.service**: oneshot service that runs one fetch cycle. Used by the timer (and can be triggered from the web UI).
- **fetch2gmail.timer**: runs the fetch on a schedule (e.g. every 5 minutes). This is how the app polls your ISP mail.
- **fetch2gmail-ui.service**: web UI (bind to 0.0.0.0:8765) and runs the fetch in the background. Run **`fetch2gmail set-ui-password`** once so the UI is protected; then enable this service.

## Install (Debian / Odroid)

1. Copy all three units (adjust user/paths as needed):
   ```bash
   sudo cp fetch2gmail.service fetch2gmail.timer fetch2gmail-ui.service /etc/systemd/system/
   ```
2. Edit the service for your user and paths:
   ```bash
   sudo systemctl edit --full fetch2gmail.service
   ```
   Set `User=` and `Group=` to the user that owns the data directory. Set `WorkingDirectory=` to your **data directory** (the folder that contains `config.json`, `credentials.json`, `token.json`, and optionally `.env`). Set `Environment=FETCH2GMAIL_CONFIG=` to the full path to `config.json` (e.g. `/opt/fetch2gmail/config.json`). Set `ExecStart=` to the path to `fetch2gmail run` (global install e.g. `/usr/local/bin/fetch2gmail run`, or venv e.g. `/opt/fetch2gmail/.venv/bin/fetch2gmail run`). See main README "Where to put config and secrets on the server" and "systemd".

3. Edit `fetch2gmail-ui.service` (same User, WorkingDirectory, FETCH2GMAIL_CONFIG as the fetch service; ExecStart = path to `fetch2gmail serve --host 0.0.0.0`). Then enable and start both the UI service and the timer:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable fetch2gmail-ui.service
   sudo systemctl start fetch2gmail-ui.service
   sudo systemctl enable fetch2gmail.timer
   sudo systemctl start fetch2gmail.timer
   ```
4. Check:
   ```bash
   systemctl list-timers fetch2gmail*
   journalctl -u fetch2gmail.service -f
   journalctl -u fetch2gmail-ui.service -f
   ```
   Open http://server-ip:8765 and enter the UI username/password (set with `fetch2gmail set-ui-password`).

## Run as specific user (e.g. pi or odroid)

Using an instance unit: `fetch2gmail@odroid.service` and `fetch2gmail@odroid.timer` with `User=odroid` and paths under `/home/odroid/fetch2gmail`. Copy to `/etc/systemd/system/` as `fetch2gmail@.service` and `fetch2gmail@.timer`, then:

```bash
sudo systemctl enable fetch2gmail@odroid.timer
sudo systemctl start fetch2gmail@odroid.timer
```
