# RFID Reader Web App for Race Timing

Python web app that connects to a UHF RFID reader over TCP, timestamps each tag read immediately (host clock), and uploads lap records to the [Simple5K Tracker](https://github.com/peanutyost/Simple5K) API. Includes a dashboard (one line per tag, same window as API dedupe) and a settings page.

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
   - `reader_id` — Reader address (default 0)
   - `max_upload_queue_time_seconds` — Flush API batch after this long (e.g. 3)
   - `api_dedupe_timeout_seconds` — Min lap interval; only earliest read per tag per window; dashboard uses same window (e.g. 10)

2. Or use the **Settings** page in the web UI after first run.

## Run

```bash
python app.py
```

Open http://localhost:5000 — Dashboard; http://localhost:5000/settings — Settings.

## Timestamps

All timestamps use the **host system clock** (UTC): race start/stop and every tag read. The reader does not provide timestamps; the app assigns one as soon as each tag is parsed. Format: `YYYY-MM-DDTHH:MM:SS.ffffffZ` (6 decimal places), as required by the Simple5K API.

## Dashboard

- **System clock (UTC)** — Same source as tag timestamps.
- **Reader diagnostics** — Firmware, temperature, work antenna, output power, frequency region (refresh when connected).
- **Tag table** — One line per EPC: first read time, read count, RSSI, antenna, frequency, PC, phase. Only tags with a read within the **same window** as the API dedupe timeout are shown (fresh reads only).

## API

- **Record lap:** Batched and deduped: at most one lap per tag per `api_dedupe_timeout_seconds`, using the **earliest** read timestamp in that window.
- **Start/Stop race:** Uses the same host clock for `update-race-time` timestamps.
