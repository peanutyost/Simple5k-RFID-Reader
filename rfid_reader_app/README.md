# RFID Reader Web App for Race Timing

Python web app that connects to a UHF RFID reader over TCP, timestamps each tag read immediately (host clock), and uploads lap records to the [Simple5K Tracker](https://github.com/peanutyost/Simple5K) API. Includes a dashboard (one line per tag) and a settings page.

## Requirements

- Python 3.9+
- UHF RFID reader on the network (TCP server; see Manuels for protocol)

## Install

```bash
cd rfid_reader_app
pip install -r requirements.txt
```

## Configure

1. Copy `config.example.json` to `config.json` and fill in:
   - `api_base_url` — Simple5K site root (e.g. `https://yoursite.com`)
   - `api_key` — From Simple5K (generate at `tracker/generate-api-key/`)
   - `reader_host` — Reader IP
   - `reader_port` — Reader TCP port (e.g. 6000)
   - `reader_id` — Reader ID as 24 hex chars (12 bytes); empty = 12 zero bytes
   - `max_tag_pending_seconds` — Read window: wait this long from first read to pick strongest signal, then stage (e.g. 30)
   - `max_staged_queue_time_seconds` — Flush all staged laps when the oldest has waited this long (e.g. 5)
   - `max_staged_queue_size` — Flush all staged laps when queue reaches this many (e.g. 50)
   - `api_dedupe_timeout_seconds` — Cooldown after staging: drop all reads for that tag during this window (e.g. 10)

2. Or use the **Settings** page in the web UI after first run.

## Run manually

```bash
python app.py
```

Open http://localhost:5000 — Dashboard; http://localhost:5000/settings — Settings.

## Running as a systemd service (Linux)

The app runs as a systemd service named `rfid-reader-app` on the race controller.

### Check status

```bash
systemctl status rfid-reader-app
```

### Start / stop / restart

```bash
sudo systemctl start rfid-reader-app
sudo systemctl stop rfid-reader-app
sudo systemctl restart rfid-reader-app
```

### View logs

```bash
journalctl -u rfid-reader-app -f          # live tail
journalctl -u rfid-reader-app -n 100      # last 100 lines
```

### Set up the service from scratch

1. Create the service file:

```bash
sudo nano /etc/systemd/system/rfid-reader-app.service
```

2. Paste the following (adjust paths if your install location differs):

```ini
[Unit]
Description=RFID Reader Web App for Race Timing
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/peanutyost/Simple5K-RFID-Reader/rfid_reader_app
ExecStart=/usr/bin/python3 /home/peanutyost/Simple5K-RFID-Reader/rfid_reader_app/app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

3. Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable rfid-reader-app
sudo systemctl start rfid-reader-app
```

The service will now start automatically on every boot.

### Find the service file location

```bash
systemctl show rfid-reader-app -p FragmentPath
```

## Race timing workflow

1. **Connect** to the reader from the dashboard
2. **Start reading** — reader begins scanning tags
3. **Select a race** from the dropdown
4. **Start race** — sends start time to the API; any reads before this are discarded
5. Tags crossing the finish line are tracked for the **tag read window** (default 30s); the read with the strongest RSSI (closest pass over the antenna) is recorded as the finish time
6. After the read window, the finish time is staged; it uploads when the **staged queue** hits its size or time limit
7. **Stop race** — flushes all in-flight reads immediately, sends stop time to the API
8. If the app crashes and restarts, it automatically reconnects to the reader and resumes (unsent staged reads are preserved in `race_state.json`)

## Timestamps

All timestamps use the **host system clock** (UTC). The reader does not provide timestamps; the app assigns one as soon as each tag is parsed from the TCP stream. Format: `YYYY-MM-DDTHH:MM:SS.ffffffZ` (6 decimal places), as required by the Simple5K API. Keep the host clock synced with NTP for accurate timing.

## Dashboard

- **System clock (UTC)** — Same source as tag timestamps.
- **Tag table** — One line per EPC: first read time, strongest-RSSI time and value, delta, read count, current RSSI, antenna, frequency, PC, phase.
- **Reader diagnostics** — Firmware, temperature, work antenna, output power, frequency region (Settings page; stop reading first).

## API

- **Record lap:** After `max_tag_pending_seconds` from first read, the strongest-RSSI timestamp is staged. Staged laps are sent when either the **oldest** staged item has waited `max_staged_queue_time_seconds` or the staged queue reaches `max_staged_queue_size`. Same tag reads are dropped for `api_dedupe_timeout_seconds` after staging.
- **Start/Stop race:** Uses the same host clock for `update-race-time` timestamps. Stop race flushes all pending reads before recording the stop time.
