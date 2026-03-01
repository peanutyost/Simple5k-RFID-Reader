"""
Simple5K Tracker API client.
API doc: https://github.com/peanutyost/Simple5K/blob/main/Simple5K/docs/API.md
"""
import requests
from typing import List, Dict, Any, Optional


def _url(base: str, path: str) -> str:
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def record_laps(
    base_url: str,
    api_key: str,
    records: List[Dict[str, Any]],
) -> tuple[bool, Optional[str], List[Dict[str, Any]]]:
    """
    POST to tracker/api/record-lap/. Each record: runner_rfid (hex), race_id (int), timestamp (ISO UTC).
    Returns (success, error_message, results_list).
    """
    if not records:
        return True, None, []
    url = _url(base_url, "tracker/api/record-lap/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    body = [
        {
            "runner_rfid": r["runner_rfid"],
            "race_id": r["race_id"],
            "timestamp": r["timestamp"],
        }
        for r in records
    ]
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", []
        data = resp.json()
        results = data.get("results", [])
        return True, None, results
    except requests.RequestException as e:
        return False, str(e), []


def available_races(base_url: str, api_key: str) -> tuple[bool, Optional[str], List[Dict[str, Any]]]:
    """GET tracker/api/available-races/. Returns (success, error_message, races_list)."""
    url = _url(base_url, "tracker/api/available-races/")
    headers = {"X-API-Key": api_key}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", []
        data = resp.json()
        if data.get("status") != "success":
            return False, data.get("error", "Unknown error"), []
        return True, None, data.get("races", [])
    except requests.RequestException as e:
        return False, str(e), []


def update_race_time(
    base_url: str,
    api_key: str,
    race_id: int,
    action: str,
    timestamp: str,
) -> tuple[bool, Optional[str]]:
    """POST tracker/api/update-race-time/. action is 'start' or 'stop'. timestamp in ISO UTC with 6 decimals."""
    url = _url(base_url, "tracker/api/update-race-time/")
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    body = {"race_id": race_id, "action": action, "timestamp": timestamp}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        if data.get("status") != "success":
            return False, data.get("error", "Unknown error")
        return True, None
    except requests.RequestException as e:
        return False, str(e)
