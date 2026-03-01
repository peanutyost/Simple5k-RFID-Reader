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
from typing import Optional, List, Dict, Any, Union

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
    "reader_id": "",  # 24 hex chars (12 bytes); empty = 12 zero bytes
    "webhook_url": "",
    "webhook_secret_header": "",
    "max_upload_queue_time_seconds": 3,
    "api_dedupe_timeout_seconds": 10,
    "tag_idle_before_stage_seconds": 2,
    "max_staged_queue_time_seconds": 5,
    "max_staged_queue_size": 50,
    "reader_inventory_restart_minutes": 10,  # 0 = disabled; restart inventory periodically to keep running for hours
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
# API: pending_api[epc] = { best_ts, best_rssi, race_id, last_read_at (epoch) }; when tag has no new reads for tag_idle_before_stage_seconds, move to staged_queue
pending_api: dict[str, dict] = {}
# staged_queue: list of { runner_rfid, race_id, timestamp (best_ts), staged_at (epoch) }; flush when max time or max size
staged_queue: list[dict] = []
last_sent_at: dict[str, float] = {}
upload_queue: Queue = Queue()
webhook_queue: Queue = Queue()
last_upload_error: str | None = None
last_webhook_error: str | None = None
reader_diagnostics: dict = {}
reader_error: str | None = None
last_inventory_restart_at: float = 0.0

# Reader protocol error codes (from Manuels ReaderUtils.FormatErrorCode)
READER_ERROR_MESSAGES = {
    0x10: "Command succeeded",
    0x11: "Command failed",
    0x20: "CPU reset error",
    0x21: "Turn on CW error",
    0x22: "Antenna is missing",
    0x23: "Write flash error",
    0x24: "Read flash error",
    0x25: "Set output power error",
    0x31: "Error during inventory",
    0x32: "Error during read",
    0x33: "Error during write",
    0x34: "Error during lock",
    0x35: "Error during kill",
    0x36: "No tag to be operated",
    0x37: "Tag inventoried but access failed",
    0x38: "Buffer is empty",
    0x40: "Access failed or wrong password",
    0x41: "Invalid parameter",
    0x42: "WordCnt too long",
    0x43: "MemBank out of range",
    0x44: "Lock region out of range",
    0x45: "LockType out of range",
    0x46: "Invalid reader address",
    0x47: "AntennaID out of range",
    0x48: "Output power out of range",
    0x49: "Frequency region out of range",
    0x4A: "Baud rate out of range",
    0x4B: "Buzzer behavior out of range",
    0x4C: "EPC match too long",
    0x4D: "EPC match length wrong",
    0x4E: "Invalid EPC match mode",
    0x4F: "Invalid frequency range",
    0x50: "Failed to receive RN16 from tag",
    0x51: "Invalid DRM mode",
    0x52: "PLL can not lock",
    0x53: "No response from RF chip",
    0x54: "Can't achieve desired output power",
    0x55: "Can't authenticate firmware copyright",
    0x56: "Spectrum regulation wrong",
    0x57: "Output power too low",
    0xFF: "Unknown error",
    # Code 5 (0x05) not in demo; often reader-specific
    0x05: "Reader error 5 (invalid address or parameter – try Reader ID 0 or 00)",
}


def _reader_error_message(code: int) -> str:
    """Human-readable message for reader protocol error code."""
    return READER_ERROR_MESSAGES.get(code & 0xFF, "Reader error %s" % (code & 0xFF))


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

        # API pending: keep timestamp of read with strongest RSSI and last read time (for staging when idle)
        if epc not in pending_api:
            pending_api[epc] = {"best_ts": ts, "best_rssi": tag.rssi, "race_id": race_id, "last_read_at": now_epoch}
        else:
            if tag.rssi > pending_api[epc].get("best_rssi", -999):
                pending_api[epc]["best_ts"] = ts
                pending_api[epc]["best_rssi"] = tag.rssi
            pending_api[epc]["race_id"] = race_id
            pending_api[epc]["last_read_at"] = now_epoch

        upload_queue.put({"epc": epc, "timestamp": ts, "rssi": tag.rssi, "race_id": race_id})
        if cfg.get("webhook_url"):
            webhook_queue.put({
                "epc": epc, "timestamp": ts, "rssi": tag.rssi, "antenna": tag.antenna,
                "race_id": race_id,
            })


# ---------------------------------------------------------------------------
# Inventory keepalive: periodically restart inventory so reader stays running for hours
# ---------------------------------------------------------------------------
def inventory_restart_worker() -> None:
    """If reader_inventory_restart_minutes > 0 and we're reading, re-send start inventory every N minutes."""
    global last_inventory_restart_at
    while True:
        time.sleep(60)  # check every minute
        cfg = load_config()
        interval_min = int(cfg.get("reader_inventory_restart_minutes") or 0)
        if interval_min <= 0:
            continue
        now = time.time()
        with _state_lock:
            if not reader_client or not connected or not reading:
                continue
            if (now - last_inventory_restart_at) < interval_min * 60:
                continue
            client = reader_client
            antennas = _parse_antenna_list(cfg.get("reader_antennas") or "")
            if len(antennas) >= 2:
                stay = max(1, int(cfg.get("reader_fast_switch_stay_ms") or 1))
                interval_ms = int(cfg.get("reader_fast_switch_interval_ms") or 0)
                repeat = int(cfg.get("reader_fast_switch_repeat") or 0)
                client.start_fast_switch_inventory(antennas, stay_ms=stay, interval_ms=interval_ms, repeat=repeat)
            else:
                work_ant = (antennas[0] - 1) if len(antennas) == 1 else 0
                client.set_work_antenna(work_ant & 0xFF)
                client.start_inventory_real(0)
            last_inventory_restart_at = now


# ---------------------------------------------------------------------------
# Upload thread: stage tags when idle (no new reads), flush staged when max time or max size
# ---------------------------------------------------------------------------
def upload_worker() -> None:
    global last_upload_error, last_sent_at, pending_api, staged_queue

    while True:
        time.sleep(0.2)
        cfg = load_config()
        tag_idle_seconds = float(cfg.get("tag_idle_before_stage_seconds", 2))
        max_staged_time = float(cfg.get("max_staged_queue_time_seconds", 5))
        max_staged_size = int(cfg.get("max_staged_queue_size", 50))
        dedupe_timeout = float(cfg.get("api_dedupe_timeout_seconds", 10))
        api_url = (cfg.get("api_base_url") or "").strip()
        api_key = (cfg.get("api_key") or "").strip()
        if not api_url or not api_key:
            continue

        now = time.time()
        with _state_lock:
            # Drain upload queue into pending_api (strongest RSSI wins; update last_read_at from latest read)
            try:
                while True:
                    r = upload_queue.get_nowait()
                    epc = r["epc"]
                    ts = r["timestamp"]
                    rssi = r.get("rssi", -999)
                    race_id = r.get("race_id")
                    read_at = _iso_to_seconds(ts) or now
                    if epc not in pending_api:
                        pending_api[epc] = {"best_ts": ts, "best_rssi": rssi, "race_id": race_id, "last_read_at": read_at}
                    else:
                        if rssi > pending_api[epc].get("best_rssi", -999):
                            pending_api[epc]["best_ts"] = ts
                            pending_api[epc]["best_rssi"] = rssi
                        pending_api[epc]["race_id"] = race_id
                        pending_api[epc]["last_read_at"] = read_at
            except Empty:
                pass

            # Stage tags that have had no new reads for tag_idle_before_stage_seconds (use best_ts); skip if sent recently (dedupe)
            for epc, data in list(pending_api.items()):
                if data.get("race_id") is None:
                    continue
                if (now - last_sent_at.get(epc, 0)) < dedupe_timeout:
                    continue
                last_read = data.get("last_read_at") or 0
                if (now - last_read) >= tag_idle_seconds:
                    staged_queue.append({
                        "runner_rfid": epc,
                        "race_id": data["race_id"],
                        "timestamp": data["best_ts"],
                        "staged_at": now,
                    })
                    del pending_api[epc]

            # Flush staged queue when: oldest item has been staged for max_staged_queue_time_seconds, or queue size >= max_staged_queue_size
            oldest_staged = min((s["staged_at"] for s in staged_queue), default=now)
            should_flush = (now - oldest_staged) >= max_staged_time or len(staged_queue) >= max_staged_size
            to_send = []
            if should_flush and staged_queue:
                to_send = [
                    {"runner_rfid": s["runner_rfid"], "race_id": s["race_id"], "timestamp": s["timestamp"]}
                    for s in staged_queue
                ]
                for s in staged_queue:
                    last_sent_at[s["runner_rfid"]] = now
                staged_queue = []

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


def _parse_reader_id(raw: Any) -> bytes:
    """Parse config reader_id. Returns 12 bytes only if 24 hex chars given; else 1 byte (standard reader address 0-255)."""
    if raw is None:
        return bytes([0])
    if isinstance(raw, int):
        return bytes([raw & 0xFF])
    s = (raw if isinstance(raw, str) else str(raw)).strip().replace(" ", "").replace(":", "")
    if not s:
        return bytes([0])
    if len(s) == 24 and all(c in "0123456789AaBbCcDdEeFf" for c in s):
        return bytes.fromhex(s)
    if len(s) <= 2 and all(c in "0123456789AaBbCcDdEeFf" for c in s):
        return bytes([int(s, 16) & 0xFF])
    return bytes([0])


def _reader_id_to_display(cfg_value: Any) -> str:
    """For Settings form: 1 byte = 2 hex chars; 12 bytes = 24 hex chars."""
    if cfg_value is None or cfg_value == "":
        return ""
    if isinstance(cfg_value, int):
        return "%02X" % (cfg_value & 0xFF)
    s = (cfg_value if isinstance(cfg_value, str) else str(cfg_value)).strip().replace(" ", "").replace(":", "")
    return s[:24].upper() if s else ""


def _parse_output_power(raw: Optional[Union[str, int]]) -> Optional[int]:
    """Parse output power 0-255; None means 'not set' (don't send set command). Accepts str or int (e.g. from JSON)."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw if 0 <= raw <= 255 else None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        n = int(str(raw).strip())
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
            "reader_id": (request.form.get("reader_id") or "").strip().replace(" ", "").replace(":", "")[:24] or "",
            "reader_output_power": _parse_output_power(request.form.get("reader_output_power")),
            "reader_antennas": reader_antennas,
            "reader_fast_switch_stay_ms": int(request.form.get("reader_fast_switch_stay_ms") or 1),
            "reader_fast_switch_interval_ms": int(request.form.get("reader_fast_switch_interval_ms") or 0),
            "reader_fast_switch_repeat": int(request.form.get("reader_fast_switch_repeat") or 0),
            "reader_inventory_restart_minutes": max(0, min(120, int(r) if (r := request.form.get("reader_inventory_restart_minutes", "").strip()) else 10)),
            "webhook_url": request.form.get("webhook_url", "").strip(),
            "webhook_secret_header": request.form.get("webhook_secret_header", "").strip(),
            "tag_idle_before_stage_seconds": max(0, int(request.form.get("tag_idle_before_stage_seconds") or 0) or 2),
            "max_staged_queue_time_seconds": max(1, int(request.form.get("max_staged_queue_time_seconds") or 0) or 5),
            "max_staged_queue_size": max(1, min(500, int(request.form.get("max_staged_queue_size") or 0) or 50)),
            "max_upload_queue_time_seconds": int(request.form.get("max_upload_queue_time_seconds") or 0) or 3,
            "api_dedupe_timeout_seconds": int(request.form.get("api_dedupe_timeout_seconds") or 0) or 10,
        }
        save_config(config)
        return redirect(url_for("index"))
    cfg = load_config()
    return render_template(
        "settings.html",
        config=cfg,
        selected_antennas=_selected_antennas_from_config(cfg),
        reader_id_display=_reader_id_to_display(cfg.get("reader_id")),
    )


@app.route("/api/status")
def api_status():
    global reader_client, connected, reading, reader_error
    # Sync app state if reader connection was lost (e.g. receive thread exited)
    with _state_lock:
        if reader_client and not reader_client.is_connected():
            reader_client = None
            connected = False
            reading = False
            reader_error = reader_error or "Connection lost"
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
    rid = _parse_reader_id(cfg.get("reader_id"))
    if not host:
        return jsonify({"error": "Reader host not set"}), 400
    with _state_lock:
        if reader_client:
            reader_client.disconnect()
            reader_client = None
        connected = False
        # Keep previous reader_error until we succeed or set a new one
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
    # Verify with a get command so we surface reader error (e.g. 5 = wrong address)
    client.get_firmware_version()
    err_code = client.get_last_error_code()
    if err_code is not None and err_code != 0:
        client.disconnect()
        with _state_lock:
            reader_error = _reader_error_message(err_code)
        return jsonify({"error": _reader_error_message(err_code)}), 400
    with _state_lock:
        reader_client = client
        connected = True
        reader_error = None
    return jsonify({"status": "ok"})


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    global reader_client, connected, reading
    with _state_lock:
        if reader_client:
            reader_client.set_inventory_round_complete_callback(None)
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


def _resend_start_inventory() -> None:
    """Called when reader sends 0x8A round-complete; re-send start command immediately to keep inventory running."""
    with _state_lock:
        if not reader_client or not connected or not reading:
            return
        client = reader_client
    cfg = load_config()
    antennas = _parse_antenna_list(cfg.get("reader_antennas") or "")
    if len(antennas) >= 2:
        stay = max(1, int(cfg.get("reader_fast_switch_stay_ms") or 1))
        interval_ms = int(cfg.get("reader_fast_switch_interval_ms") or 0)
        repeat = int(cfg.get("reader_fast_switch_repeat") or 0)
        client.start_fast_switch_inventory(antennas, stay_ms=stay, interval_ms=interval_ms, repeat=repeat)
    else:
        work_ant = (antennas[0] - 1) if len(antennas) == 1 else 0
        client.set_work_antenna(work_ant & 0xFF)
        client.start_inventory_real(0)


@app.route("/api/start-reading", methods=["POST"])
def api_start_reading():
    global reading, last_inventory_restart_at
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
            last_inventory_restart_at = time.time()
            reader_client.set_inventory_round_complete_callback(_resend_start_inventory)
            return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to start inventory"}), 500


@app.route("/api/stop-reading", methods=["POST"])
def api_stop_reading():
    global reading
    with _state_lock:
        reading = False
        if reader_client:
            reader_client.set_inventory_round_complete_callback(None)
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
    global reader_diagnostics, reader_error
    with _state_lock:
        if not reader_client or not connected:
            return jsonify({"error": "Not connected"}), 400
        client = reader_client
    # Brief pause so receive thread can process any in-flight data, then delays between get commands
    time.sleep(0.15)
    fw = client.get_firmware_version()
    time.sleep(0.12)
    temp = client.get_reader_temperature()
    time.sleep(0.12)
    ant = client.get_work_antenna()
    time.sleep(0.12)
    power = client.get_output_power()
    time.sleep(0.12)
    freq = client.get_frequency_region()
    err_code = client.get_last_error_code()
    with _state_lock:
        # Temperature: (btPlusMinus, btTemperature) per Manuels — 0 = negative, non-zero = positive
        temp_str = None
        if temp is not None and len(temp) >= 2:
            temp_str = f"{'-' if temp[0] == 0 else ''}{temp[1]}°C"
        # Power: show as decimal (e.g. 31 instead of "1f")
        power_str = None
        if power:
            power_str = ", ".join(str(b) for b in power) if len(power) > 1 else str(power[0])
        # Frequency: decode region bytes to MHz (900s). 3 bytes = region, start, end per Manuels.
        freq_str = None
        if freq and len(freq) >= 3:
            _, start_byte, end_byte = freq[0], freq[1], freq[2]
            def _freq_byte_to_mhz(b):
                if b < 0x07:
                    return 865.0 + b * 0.5
                return 902.0 + (b - 7) * 0.5
            freq_str = f"{_freq_byte_to_mhz(start_byte):.1f}-{_freq_byte_to_mhz(end_byte):.1f} MHz"
        elif freq:
            freq_str = freq.hex()
        reader_diagnostics = {
            "firmware": f"{fw[0]}.{fw[1]}" if fw and len(fw) >= 2 else None,
            "temperature": temp_str,
            "work_antenna": ant if ant is not None else None,
            "output_power": power_str,
            "frequency_region": freq_str,
        }
        if err_code is not None and err_code != 0:
            reader_error = _reader_error_message(err_code)
    out = {"status": "ok", "diagnostics": reader_diagnostics}
    if err_code is not None and err_code != 0:
        out["error"] = _reader_error_message(err_code)
    return jsonify(out)


@app.route("/api/reader-diagnostics-debug", methods=["GET"])
def api_reader_diagnostics_debug():
    """One get_firmware call; returns raw response payload so we can see if reader responds. GET only."""
    with _state_lock:
        if not reader_client or not connected:
            return jsonify({"error": "Not connected", "payload_hex": None}), 400
        client = reader_client
    time.sleep(0.1)
    raw_payload, err_code = client.get_firmware_version_raw()
    payload_hex = " ".join(f"{b:02X}" for b in raw_payload) if raw_payload else None
    fw = (raw_payload[0], raw_payload[1]) if len(raw_payload) >= 2 else None
    return jsonify({
        "ok": fw is not None,
        "firmware": f"{fw[0]}.{fw[1]}" if fw else None,
        "payload_hex": payload_hex,
        "payload_len": len(raw_payload) if raw_payload else 0,
        "error_code": err_code,
    })


# ---------------------------------------------------------------------------
# Start background workers
# ---------------------------------------------------------------------------
def main():
    t0 = threading.Thread(target=inventory_restart_worker, daemon=True)
    t1 = threading.Thread(target=upload_worker, daemon=True)
    t2 = threading.Thread(target=webhook_worker, daemon=True)
    t0.start()
    t1.start()
    t2.start()
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
