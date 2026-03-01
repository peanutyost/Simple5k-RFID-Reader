"""
RFID Reader Web App for Race Timing.
Connects to UHF reader over TCP, timestamps tags immediately, shows dashboard, uploads to Simple5K API.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue, Empty
from typing import Optional, List, Dict, Any

from flask import Flask, render_template, request, jsonify, redirect, url_for

from reader_protocol import ReaderClient, TagRead
from simple5k_client import record_laps, available_races, update_race_time

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()

# ---------------------------------------------------------------------------
# Config (load/save)
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DEFAULT_CONFIG = {
    "api_base_url": "",
    "api_key": "",
    "reader_host": "192.168.1.100",
    "reader_port": 6000,
    "reader_id": 0,
    "webhook_url": "",
    "webhook_secret_header": "",
    "max_upload_queue_time_seconds": 3,
    "api_dedupe_timeout_seconds": 10,
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                c = json.load(f)
                return {**DEFAULT_CONFIG, **c}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(c: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)


# ---------------------------------------------------------------------------
# State (thread-safe)
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
config = load_config()
reader_client: Optional[ReaderClient] = None
connected = False
reading = False
selected_race: dict | None = None  # {id, name, ...}
# Dashboard: one row per EPC (only fresh within api_dedupe_timeout)
# Value: first_read_timestamp, strongest_rssi_timestamp, strongest_rssi, read_count, rssi (latest), antenna, ...
dashboard_tags: dict[str, dict] = {}
# API dedupe: pending_api[epc] = { best_ts (timestamp of strongest RSSI), best_rssi, race_id }; last_sent_at[epc] = epoch
pending_api: dict[str, dict] = {}
last_sent_at: dict[str, float] = {}
upload_queue: Queue = Queue()
webhook_queue: Queue = Queue()
last_upload_error: str | None = None
last_webhook_error: str | None = None
reader_diagnostics: dict = {}
reader_error: str | None = None


def _utc_now_iso() -> str:
    t = datetime.now(timezone.utc)
    s = t.strftime("%Y-%m-%dT%H:%M:%S.%f")
    return s[:26] + "Z"  # 6 decimals


def _iso_to_seconds(iso: str) -> Optional[float]:
    """Parse ISO 8601 with Z to Unix seconds, or None."""
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00")
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _delta_first_to_strongest(first_iso: str, strongest_iso: str) -> Optional[float]:
    """Seconds from first seen to strongest signal (positive = strongest after first)."""
    t1 = _iso_to_seconds(first_iso)
    t2 = _iso_to_seconds(strongest_iso)
    if t1 is None or t2 is None:
        return None
    return round(t2 - t1, 3)


# ---------------------------------------------------------------------------
# Tag callback: timestamp immediately, update dashboard, queue for API/webhook
# ---------------------------------------------------------------------------
def on_tag(tag: TagRead) -> None:
    ts = _utc_now_iso()
    tag.timestamp = ts
    cfg = load_config()
    timeout = float(cfg.get("api_dedupe_timeout_seconds", 10))
    with _state_lock:
        race_id = selected_race.get("id") if selected_race else None
    now_epoch = time.time()

    with _state_lock:
        # Dashboard: one line per EPC; first read time; strongest signal time + RSSI; increment read count
        epc = tag.epc
        if epc not in dashboard_tags:
            dashboard_tags[epc] = {
                "epc": epc,
                "first_read_timestamp": ts,
                "strongest_rssi_timestamp": ts,
                "strongest_rssi": tag.rssi,
                "read_count": 1,
                "rssi": tag.rssi,
                "antenna": tag.antenna,
                "frequency_mhz": tag.frequency_mhz,
                "pc": tag.pc,
                "phase": tag.phase,
                "last_read_at": now_epoch,
            }
        else:
            row = dashboard_tags[epc]
            row["read_count"] += 1
            row["rssi"] = tag.rssi
            row["antenna"] = tag.antenna
            row["frequency_mhz"] = tag.frequency_mhz
            row["pc"] = tag.pc
            row["phase"] = tag.phase
            row["last_read_at"] = now_epoch
            if tag.rssi > row.get("strongest_rssi", -999):
                row["strongest_rssi"] = tag.rssi
                row["strongest_rssi_timestamp"] = ts

        # API pending: keep timestamp of read with strongest RSSI (that is what we send)
        if epc not in pending_api:
            pending_api[epc] = {"best_ts": ts, "best_rssi": tag.rssi, "race_id": race_id}
        else:
            if tag.rssi > pending_api[epc].get("best_rssi", -999):
                pending_api[epc]["best_ts"] = ts
                pending_api[epc]["best_rssi"] = tag.rssi
            pending_api[epc]["race_id"] = race_id

        upload_queue.put({"epc": epc, "timestamp": ts, "rssi": tag.rssi, "race_id": race_id})
        if cfg.get("webhook_url"):
            webhook_queue.put({
                "epc": epc, "timestamp": ts, "rssi": tag.rssi, "antenna": tag.antenna,
                "race_id": race_id,
            })


# ---------------------------------------------------------------------------
# Upload thread: flush by max queue time or batch size; apply API dedupe
# ---------------------------------------------------------------------------
def upload_worker() -> None:
    global last_upload_error, last_sent_at, pending_api

    while True:
        time.sleep(0.2)
        cfg = load_config()
        max_queue_time = float(cfg.get("max_upload_queue_time_seconds", 3))
        dedupe_timeout = float(cfg.get("api_dedupe_timeout_seconds", 10))
        api_url = (cfg.get("api_base_url") or "").strip()
        api_key = (cfg.get("api_key") or "").strip()
        if not api_url or not api_key:
            continue

        now = time.time()
        # Drain upload queue into pending_api (keep timestamp of read with strongest RSSI per EPC)
        with _state_lock:
            try:
                while True:
                    r = upload_queue.get_nowait()
                    epc = r["epc"]
                    ts = r["timestamp"]
                    rssi = r.get("rssi", -999)
                    race_id = r.get("race_id")
                    if epc not in pending_api:
                        pending_api[epc] = {"best_ts": ts, "best_rssi": rssi, "race_id": race_id}
                    else:
                        if rssi > pending_api[epc].get("best_rssi", -999):
                            pending_api[epc]["best_ts"] = ts
                            pending_api[epc]["best_rssi"] = rssi
                        pending_api[epc]["race_id"] = race_id
            except Empty:
                pass

            # Build batch: EPCs that are due; send best_ts (timestamp of strongest signal)
            to_send = []
            for epc, data in list(pending_api.items()):
                last = last_sent_at.get(epc, 0)
                if (now - last) >= dedupe_timeout and data.get("race_id") is not None:
                    to_send.append({
                        "runner_rfid": epc,
                        "race_id": data["race_id"],
                        "timestamp": data["best_ts"],
                    })
                    last_sent_at[epc] = now
                    del pending_api[epc]

        if to_send:
            ok, err, _ = record_laps(api_url, api_key, to_send)
            with _state_lock:
                last_upload_error = None if ok else (err or "Unknown error")


# ---------------------------------------------------------------------------
# Webhook worker
# ---------------------------------------------------------------------------
def webhook_worker() -> None:
    global last_webhook_error
    while True:
        time.sleep(0.1)
        cfg = load_config()
        url = (cfg.get("webhook_url") or "").strip()
        if not url:
            try:
                webhook_queue.get_nowait()
            except Empty:
                pass
            continue
        try:
            payload = webhook_queue.get(timeout=0.5)
            headers = {"Content-Type": "application/json"}
            secret = (cfg.get("webhook_secret_header") or "").strip()
            if secret:
                headers["X-Webhook-Secret"] = secret
            r = __import__("requests").post(url, json=payload, headers=headers, timeout=5)
            with _state_lock:
                last_webhook_error = None if 200 <= r.status_code < 300 else f"HTTP {r.status_code}"
        except Empty:
            pass
        except Exception as e:
            with _state_lock:
                last_webhook_error = str(e)


# ---------------------------------------------------------------------------
# Prune dashboard to same window as API dedupe
# ---------------------------------------------------------------------------
def prune_dashboard() -> None:
    cfg = load_config()
    timeout = float(cfg.get("api_dedupe_timeout_seconds", 10))
    now = time.time()
    with _state_lock:
        to_remove = [epc for epc, row in dashboard_tags.items() if (now - row["last_read_at"]) > timeout]
        for epc in to_remove:
            del dashboard_tags[epc]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


def _parse_output_power(raw: Optional[str]) -> Optional[int]:
    """Parse output power 0-255; None means 'not set' (don't send set command)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        n = int(raw.strip())
        if 0 <= n <= 255:
            return n
    except ValueError:
        pass
    return None


def _selected_antennas_from_config(cfg: dict) -> List[int]:
    """Build list of selected antenna numbers (1-8) from config. Supports reader_antennas or legacy reader_work_antenna."""
    ant_str = (cfg.get("reader_antennas") or "").strip()
    if ant_str:
        return _parse_antenna_list(ant_str)
    work = cfg.get("reader_work_antenna")
    if work is not None and work != "":
        try:
            w = int(work) & 0xFF
            if 0 <= w <= 7:
                return [w + 1]  # 0-7 -> antenna 1-8
        except (TypeError, ValueError):
            pass
    return []


@app.route("/settings", methods=["GET", "POST"])
def settings():
    global config
    if request.method == "POST":
        current = load_config()
        new_key = request.form.get("api_key", "").strip()
        # Build reader_antennas from checkboxes (reader_antenna_1 .. reader_antenna_8)
        selected = []
        for n in range(1, 9):
            if request.form.get("reader_antenna_%d" % n) == "1":
                selected.append(n)
        reader_antennas = ",".join(str(n) for n in sorted(selected)) if selected else ""
        config = {
            "api_base_url": request.form.get("api_base_url", "").strip(),
            "api_key": new_key if new_key else current.get("api_key", ""),
            "reader_host": request.form.get("reader_host", "").strip(),
            "reader_port": int(request.form.get("reader_port") or 0) or 6000,
            "reader_id": int(request.form.get("reader_id") or 0),
            "reader_output_power": _parse_output_power(request.form.get("reader_output_power")),
            "reader_antennas": reader_antennas,
            "reader_fast_switch_stay_ms": int(request.form.get("reader_fast_switch_stay_ms") or 1),
            "reader_fast_switch_interval_ms": int(request.form.get("reader_fast_switch_interval_ms") or 0),
            "reader_fast_switch_repeat": int(request.form.get("reader_fast_switch_repeat") or 0),
            "webhook_url": request.form.get("webhook_url", "").strip(),
            "webhook_secret_header": request.form.get("webhook_secret_header", "").strip(),
            "max_upload_queue_time_seconds": int(request.form.get("max_upload_queue_time_seconds") or 0) or 3,
            "api_dedupe_timeout_seconds": int(request.form.get("api_dedupe_timeout_seconds") or 0) or 10,
        }
        save_config(config)
        return redirect(url_for("index"))
    cfg = load_config()
    return render_template("settings.html", config=cfg, selected_antennas=_selected_antennas_from_config(cfg))


@app.route("/api/status")
def api_status():
    prune_dashboard()
    cfg = load_config()
    tags_list = []
    for row in dashboard_tags.values():
        r = dict(row)
        delta = _delta_first_to_strongest(
            r.get("first_read_timestamp"),
            r.get("strongest_rssi_timestamp"),
        )
        r["delta_seconds"] = delta  # + = strongest after first seen
        tags_list.append(r)
    api_configured = bool((cfg.get("api_base_url") or "").strip() and (cfg.get("api_key") or "").strip())
    return jsonify({
        "connected": connected,
        "reading": reading,
        "selected_race": selected_race,
        "recent_tags": tags_list,
        "last_error": reader_error,
        "last_upload_error": last_upload_error,
        "last_webhook_error": last_webhook_error,
        "server_time_utc": _utc_now_iso(),
        "reader_diagnostics": reader_diagnostics,
        "api_configured": api_configured,
    })


@app.route("/api/available-races")
def api_available_races():
    cfg = load_config()
    url = (cfg.get("api_base_url") or "").strip()
    key = (cfg.get("api_key") or "").strip()
    if not url or not key:
        return jsonify({"races": []})
    ok, err, races = available_races(url, key)
    if not ok:
        return jsonify({"error": err or "Failed"}), 400
    return jsonify({"races": races})


@app.route("/api/connect", methods=["POST"])
def api_connect():
    global reader_client, connected, reader_error
    cfg = load_config()
    host = (cfg.get("reader_host") or "").strip()
    port = int(cfg.get("reader_port") or 0) or 6000
    rid = int(cfg.get("reader_id") or 0)
    if not host:
        return jsonify({"error": "Reader host not set"}), 400
    with _state_lock:
        if reader_client:
            reader_client.disconnect()
            reader_client = None
        connected = False
        reader_error = None
    client = ReaderClient(host, port, read_id=rid)
    client.set_tag_callback(on_tag)
    if not client.connect():
        with _state_lock:
            reader_error = "Connection failed"
        return jsonify({"error": "Connection failed"}), 400
    client.start_receive_thread()
    power = _parse_output_power(cfg.get("reader_output_power"))
    if power is not None:
        client.set_output_power(bytes([power]))
    with _state_lock:
        reader_client = client
        connected = True
    return jsonify({"status": "ok"})


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    global reader_client, connected, reading
    with _state_lock:
        if reader_client:
            reader_client.disconnect()
            reader_client = None
        connected = False
        reading = False
    return jsonify({"status": "ok"})


def _parse_antenna_list(s: str) -> List[int]:
    """Parse '1,2,3,4' into [1,2,3,4] (valid 1-8)."""
    if not s or not s.strip():
        return []
    out = []
    for part in s.replace(" ", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
            if 1 <= n <= 8:
                out.append(n)
        except ValueError:
            pass
    return out


@app.route("/api/start-reading", methods=["POST"])
def api_start_reading():
    global reading
    cfg = load_config()
    with _state_lock:
        if not reader_client or not connected:
            return jsonify({"error": "Not connected"}), 400
        if reading:
            return jsonify({"status": "ok"})
        antennas = _parse_antenna_list(cfg.get("reader_antennas") or "")
        if len(antennas) >= 2:
            stay = max(1, int(cfg.get("reader_fast_switch_stay_ms") or 1))
            interval = int(cfg.get("reader_fast_switch_interval_ms") or 0)
            repeat = int(cfg.get("reader_fast_switch_repeat") or 0)
            ok = reader_client.start_fast_switch_inventory(antennas, stay_ms=stay, interval_ms=interval, repeat=repeat)
        else:
            # One or zero antennas: use work antenna (0-based). Single selected antenna from list, else 0.
            work_ant = (antennas[0] - 1) if len(antennas) == 1 else 0
            reader_client.set_work_antenna(work_ant & 0xFF)
            ok = reader_client.start_inventory_real(0)
        if ok:
            reading = True
            return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to start inventory"}), 500


@app.route("/api/stop-reading", methods=["POST"])
def api_stop_reading():
    global reading
    with _state_lock:
        reading = False
    return jsonify({"status": "ok"})


@app.route("/api/start-race", methods=["POST"])
def api_start_race():
    if not selected_race:
        return jsonify({"error": "No race selected"}), 400
    cfg = load_config()
    url = (cfg.get("api_base_url") or "").strip()
    key = (cfg.get("api_key") or "").strip()
    if not url or not key:
        return jsonify({"error": "API not configured"}), 400
    ts = _utc_now_iso()
    ok, err = update_race_time(url, key, selected_race["id"], "start", ts)
    if not ok:
        return jsonify({"error": err or "Failed"}), 400
    return jsonify({"status": "ok"})


@app.route("/api/stop-race", methods=["POST"])
def api_stop_race():
    if not selected_race:
        return jsonify({"error": "No race selected"}), 400
    cfg = load_config()
    url = (cfg.get("api_base_url") or "").strip()
    key = (cfg.get("api_key") or "").strip()
    if not url or not key:
        return jsonify({"error": "API not configured"}), 400
    ts = _utc_now_iso()
    ok, err = update_race_time(url, key, selected_race["id"], "stop", ts)
    if not ok:
        return jsonify({"error": err or "Failed"}), 400
    return jsonify({"status": "ok"})


@app.route("/api/select-race", methods=["POST"])
def api_select_race():
    global selected_race
    data = request.get_json() or {}
    race_id = data.get("race_id")
    if race_id is not None:
        with _state_lock:
            selected_race = {"id": race_id, "name": data.get("name", str(race_id))}
    return jsonify({"status": "ok"})


@app.route("/api/reader-get-diagnostics", methods=["POST"])
def api_reader_diagnostics():
    global reader_diagnostics
    with _state_lock:
        if not reader_client or not connected:
            return jsonify({"error": "Not connected"}), 400
        client = reader_client
    fw = client.get_firmware_version()
    temp = client.get_reader_temperature()
    ant = client.get_work_antenna()
    power = client.get_output_power()
    freq = client.get_frequency_region()
    with _state_lock:
        reader_diagnostics = {
            "firmware": f"{fw[0]}.{fw[1]}" if fw else None,
            "temperature": f"{temp[0]}{temp[1]}°C" if temp else None,
            "work_antenna": ant,
            "output_power": power.hex() if power else None,
            "frequency_region": freq.hex() if freq else None,
        }
    return jsonify({"status": "ok", "diagnostics": reader_diagnostics})


# ---------------------------------------------------------------------------
# Start background workers
# ---------------------------------------------------------------------------
def main():
    t1 = threading.Thread(target=upload_worker, daemon=True)
    t2 = threading.Thread(target=webhook_worker, daemon=True)
    t1.start()
    t2.start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
