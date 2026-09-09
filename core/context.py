"""
J.A.R.V.I.S. — Situational Context
Gathers real-time environmental data for Claude prompt injection.
All functions are wrapped in try/except — never crashes the voice pipeline.
"""

import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from .paths import CREDENTIALS, LOGS

# ── Logging (file only — never pollutes stdout) ───────────────────────────────
LOGS.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOGS / "context_errors.log"),
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Module-level caches: (value, timestamp) ───────────────────────────────────
_weather_cache:  tuple | None = None
_calendar_cache: tuple | None = None
_gmail_cache:    tuple | None = None
_block_cache:    tuple | None = None   # (text, timestamp)

WEATHER_TTL  = 600   # 10 minutes
CALENDAR_TTL = 120   # 2 minutes
GMAIL_TTL    = 120   # 2 minutes
BLOCK_TTL    = 8     # assembled prompt — skip refetch inside a tool loop

_ctx_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jarvis-ctx")

# ── Google OAuth — shared read-only credentials ───────────────────────────────
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",        # read + write events
]
_CREDENTIALS_PATH = str(CREDENTIALS / "gmail_credentials.json")
_TOKEN_PATH       = str(CREDENTIALS / "context_token.json")
_google_creds     = None


def _get_google_creds():
    """Return valid Google credentials, refreshing or re-authing as needed."""
    global _google_creds
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        if _google_creds and _google_creds.valid:
            return _google_creds

        creds = None
        if os.path.exists(_TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(_TOKEN_PATH, _SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(_CREDENTIALS_PATH):
                    log.warning("gmail_credentials.json not found at %s", _CREDENTIALS_PATH)
                    return None
                flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS_PATH, _SCOPES)
                creds = flow.run_local_server(port=0)
            with open(_TOKEN_PATH, "w") as f:
                f.write(creds.to_json())

        _google_creds = creds
        return creds
    except Exception as exc:
        log.warning("Google creds error: %s", exc)
        return None


# ── 1. Time & Date ────────────────────────────────────────────────────────────

def get_time_info() -> dict:
    """Return current time, date, period, and weekend flag."""
    try:
        now  = datetime.now()
        hour = now.hour
        wday = now.weekday()  # 0=Monday … 6=Sunday

        if 5 <= hour <= 11:
            period = "morning"
        elif 12 <= hour <= 16:
            period = "afternoon"
        elif 17 <= hour <= 21:
            period = "evening"
        else:
            period = "late night"

        return {
            "time":       now.strftime("%H:%M"),
            "weekday":    now.strftime("%A"),
            "date":       now.strftime("%-B %-d, %Y"),   # e.g. "April 6, 2026"
            "period":     period,
            "is_weekend": wday >= 5,
        }
    except Exception as exc:
        log.warning("Time info error: %s", exc)
        return {
            "time": "unavailable", "weekday": "unavailable",
            "date": "unavailable", "period":  "unavailable",
            "is_weekend": False,
        }


# ── 2. Battery ────────────────────────────────────────────────────────────────

def get_battery() -> int | None:
    """Return battery percentage (0–100) from pmset, or None on failure."""
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            capture_output=True, text=True, timeout=2,
        )
        m = re.search(r"(\d+)%", result.stdout)
        if m:
            return int(m.group(1))
    except Exception as exc:
        log.warning("Battery error: %s", exc)
    return None


# ── 3. Weather (10-min cache) ─────────────────────────────────────────────────

def get_weather() -> str:
    """Return a human-readable weather string, cached for 10 minutes."""
    global _weather_cache
    try:
        now_ts = time.time()
        if _weather_cache and (now_ts - _weather_cache[1]) < WEATHER_TTL:
            return _weather_cache[0]

        resp = requests.get(
            "https://wttr.in/?format=j1",
            timeout=3,
            headers={"User-Agent": "JARVIS/2.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        cond = data["current_condition"][0]

        temp_c   = cond["temp_C"]
        desc     = cond["weatherDesc"][0]["value"].lower()
        wind_kmh = cond["windspeedKmph"]
        result   = f"{temp_c}°C, {desc}, {wind_kmh} km/h wind"

        _weather_cache = (result, now_ts)
        return result
    except Exception as exc:
        log.warning("Weather error: %s", exc)
        return "weather unavailable"


# ── 4. Next Calendar Event (2-min cache) ─────────────────────────────────────

def get_next_event() -> dict | None:
    """Return {'title': str, 'minutes_away': int} for the next event today, or None."""
    global _calendar_cache
    try:
        now_ts = time.time()
        if _calendar_cache and (now_ts - _calendar_cache[1]) < CALENDAR_TTL:
            return _calendar_cache[0]

        creds = _get_google_creds()
        if not creds:
            _calendar_cache = (None, now_ts)
            return None

        from googleapiclient.discovery import build

        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        now_utc     = datetime.now(timezone.utc)
        end_of_day  = now_utc.replace(hour=23, minute=59, second=59, microsecond=0)

        events_result = service.events().list(
            calendarId="primary",
            timeMin=now_utc.isoformat(),
            timeMax=end_of_day.isoformat(),
            maxResults=5,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = events_result.get("items", [])
        if not events:
            _calendar_cache = (None, now_ts)
            return None

        event = events[0]
        title = event.get("summary", "Unnamed event")
        start = event["start"].get("dateTime", event["start"].get("date"))

        # Parse ISO 8601 start time
        if "T" in start:
            # Strip timezone offset and parse — handle both +HH:MM and Z suffixes
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            minutes_away = max(0, int((start_dt - now_utc).total_seconds() / 60))
        else:
            minutes_away = 0  # All-day event

        result = {"title": title, "minutes_away": minutes_away}
        _calendar_cache = (result, now_ts)
        return result

    except Exception as exc:
        log.warning("Calendar error: %s", exc)
        _calendar_cache = (None, time.time())
        return None


# ── 5. Unread Email Count (2-min cache) ──────────────────────────────────────

def get_unread_count() -> int | None:
    """Return unread inbox count via Gmail API, cached for 2 minutes."""
    global _gmail_cache
    try:
        now_ts = time.time()
        if _gmail_cache and (now_ts - _gmail_cache[1]) < GMAIL_TTL:
            return _gmail_cache[0]

        creds = _get_google_creds()
        if not creds:
            _gmail_cache = (None, now_ts)
            return None

        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        result  = service.users().messages().list(
            userId="me",
            q="is:unread in:inbox",
            maxResults=1,
        ).execute()

        count = result.get("resultSizeEstimate", 0)
        _gmail_cache = (count, now_ts)
        return count

    except Exception as exc:
        log.warning("Gmail unread error: %s", exc)
        _gmail_cache = (None, time.time())
        return None


# ── Assemble the full context block ──────────────────────────────────────────

def _safe_result(fut, default, timeout: float):
    try:
        return fut.result(timeout=timeout)
    except Exception as exc:
        log.warning("Context field timeout/error: %s", exc)
        return default


def get_context_block() -> str:
    """
    Build and return the formatted context string injected into every
    Claude system prompt. Completes in <500ms; all fields degrade gracefully.
    Weather/calendar/gmail/battery are fetched in parallel.
    """
    global _block_cache
    try:
        now_ts = time.time()
        if _block_cache and (now_ts - _block_cache[1]) < BLOCK_TTL:
            return _block_cache[0]

        t = get_time_info()
        f_battery = _ctx_pool.submit(get_battery)
        f_weather = _ctx_pool.submit(get_weather)
        f_event   = _ctx_pool.submit(get_next_event)
        f_unread  = _ctx_pool.submit(get_unread_count)

        battery = _safe_result(f_battery, None, 3)
        weather = _safe_result(f_weather, "weather unavailable", 4)
        event   = _safe_result(f_event, None, 4)
        unread  = _safe_result(f_unread, None, 4)

        time_str   = f"{t['time']}, {t['weekday']}"
        bat_str    = f"{battery}%" if battery is not None else "unavailable"
        unread_str = str(unread)   if unread  is not None else "unavailable"
        event_str  = (f"{event['title']} in {event['minutes_away']} minutes"
                      if event else "none today")

        block = (
            "--- CURRENT CONTEXT ---\n"
            f"TIME: {time_str}\n"
            f"DATE: {t['date']}\n"
            f"PERIOD: {t['period']}\n"
            f"WEATHER: {weather}\n"
            f"NEXT EVENT: {event_str}\n"
            f"UNREAD EMAILS: {unread_str}\n"
            f"BATTERY: {bat_str}\n"
            "-----------------------"
        )
        _block_cache = (block, now_ts)
        return block
    except Exception as exc:
        log.warning("get_context_block fatal error: %s", exc)
        return "--- CURRENT CONTEXT ---\n[context unavailable]\n-----------------------"
