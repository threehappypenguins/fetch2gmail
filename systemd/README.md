# systemd setup

One service runs the web UI and the background fetch (polls your ISP mailbox every few minutes). Use **`fetch2gmail install-service`** to generate the unit file; see the main README.

## Quick install

```bash
fetch2gmail install-service --user YOUR_USER --dir /opt/fetch2gmail | sudo tee /etc/systemd/system/fetch2gmail.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable fetch2gmail
sudo systemctl start fetch2gmail
```

Replace **YOUR_USER** and **/opt/fetch2gmail** with your user and data directory. Run **`fetch2gmail set-ui-password`** once (from the data directory) so the UI is protected, then open http://server-ip:8765.

To run a **second instance on a different port** (for another mailbox or config):

1. Generate a second unit with a different name (e.g. `fetch2gmail2.service`) and write it to `/tmp`:
   ```bash
   fetch2gmail install-service --user YOUR_USER --dir /opt/fetch2gmail2 -o /tmp/fetch2gmail2.service
   ```
2. Move it into place, then edit it to add the port flag:
   ```bash
   sudo mv /tmp/fetch2gmail2.service /etc/systemd/system/fetch2gmail2.service
   sudo nano /etc/systemd/system/fetch2gmail2.service
   ```
   In the editor, find the `ExecStart=` line and make sure it includes a port flag, for example:
   ```
   ExecStart=/home/YOUR_USER/.local/pipx/venvs/fetch2gmail/bin/fetch2gmail serve --host 0.0.0.0 --port 8766
   ```
   Each systemd service should point to its own data directory and use its own port.

## Manual copy

If you prefer not to use `install-service`, copy **fetch2gmail.service** from this directory to `/etc/systemd/system/`, then edit it: set **User**, **Group**, **WorkingDirectory**, **Environment=FETCH2GMAIL_CONFIG**, and **ExecStart** (path to `fetch2gmail serve --host 0.0.0.0`).

## Logs

```bash
journalctl -u fetch2gmail -f
```
