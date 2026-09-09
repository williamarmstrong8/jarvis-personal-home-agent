"""Deterministic spoken responses for high-confidence command paths.

Integrations already return user-facing strings for most direct actions.  This
module keeps those commands off the response-model path and provides compact,
predictable summaries for the few structured homelab results.
"""

from __future__ import annotations

import json
import re
from typing import Any


DIRECT_RESPONSE_TOOLS = {
    "get_context_value",
    "open_homelab",
    "play_movie",
    "play_spotify",
    "pause_spotify",
    "resume_spotify",
    "skip_spotify",
    "get_currently_playing",
    "list_calendar_events",
    "search_gmail",
    "draft_gmail",
    "send_gmail",
    "create_calendar_event",
    "create_notion_page",
    "append_to_notion",
    "generate_content",
    "web_search",
    "send_imessage",
    "airdrop_file",
    "generate_daily_podcast",
    "pi_get_status",
    "pi_list_movies",
    "pi_list_series",
    "pi_now_playing",
}


def supports_direct_response(tool: str | None) -> bool:
    return bool(tool and tool in DIRECT_RESPONSE_TOOLS)


def _json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _plain(value: Any, limit: int = 420) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return None
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "."
    return text


def _pi_status(result: Any) -> str | None:
    data = _json(result)
    if not isinstance(data, dict):
        return _plain(result)
    if data.get("error"):
        return f"I couldn't retrieve the Pi status, sir: {data['error']}"
    if not any(key in data for key in ("services", "services_down", "disk", "cpu_temp_c")):
        return _plain(result)
    down = data.get("services_down") or []
    disk = data.get("disk") or {}
    temp = data.get("cpu_temp_c")
    internet = data.get("internet")
    parts = []
    if down:
        parts.append(f"{len(down)} services need attention: {', '.join(map(str, down[:4]))}")
    else:
        parts.append("all reported services are up")
    if disk.get("used_pct") is not None:
        parts.append(f"disk usage is {disk['used_pct']} percent")
    if temp is not None:
        parts.append(f"CPU temperature is {temp} degrees Celsius")
    if internet is False:
        parts.append("outbound internet is down")
    return "The Pi is online; " + ", and ".join(parts) + ", sir."


def _pi_library(result: Any, key: str, label: str) -> str | None:
    data = _json(result)
    if not isinstance(data, dict):
        return _plain(result)
    if data.get("error"):
        return f"I couldn't read the Pi {label} library, sir: {data['error']}"
    rows = data.get(key) or []
    total = data.get("total", len(rows))
    names = [str(row.get("title")) for row in rows[:3] if isinstance(row, dict) and row.get("title")]
    if not total:
        return f"There are no {label} in the Pi library, sir."
    sample = f" The first few are {', '.join(names)}." if names else ""
    return f"The Pi library has {total} {label}, sir.{sample}"


def _calendar(result: Any) -> str | None:
    text = _plain(result, limit=1200)
    if not text:
        return None
    if text.startswith("No calendar") or text.startswith("Could not"):
        return text
    count = re.search(r"\((\d+) found\)", text)
    first = re.search(r"(?:found\):?\s*)?•\s*([^—]+)—\s*([^•]+)", text)
    if count and first:
        title = first.group(1).strip()
        when = first.group(2).split(" Attendees:", 1)[0].split(" Location:", 1)[0].strip()
        return f"You have {count.group(1)} events; next is {title}, {when}, sir."
    return text


def _gmail(result: Any) -> str | None:
    text = _plain(result, limit=1600)
    if not text:
        return None
    if text.startswith("No emails") or text.startswith("Could not"):
        return text
    subjects = re.findall(r"Subject:\s*([^\n]+?)(?=\s+Date:|\s+Snippet:|\s+From:|$)", result)
    if subjects:
        return f"I found {len(subjects)} unread messages; the latest subjects are {', '.join(subjects[:3])}, sir."
    return text


def _search(result: Any) -> str | None:
    if not isinstance(result, str):
        return None
    if result.startswith(("Web search", "No results", "Could not")):
        return _plain(result)
    match = re.search(r"Summary:\s*(.+?)(?:\n\s*Top \d+ results|$)", result, re.S)
    return _plain(match.group(1)) if match else None


def direct_response(tool: str | None, result: Any, inputs: dict | None = None) -> str | None:
    """Return speech-ready text, or ``None`` to retain model verbalization."""
    if not supports_direct_response(tool):
        return None
    if tool == "pi_get_status":
        return _pi_status(result)
    if tool == "pi_list_movies":
        return _pi_library(result, "movies", "movies")
    if tool == "pi_list_series":
        return _pi_library(result, "series", "series")
    if tool == "list_calendar_events":
        return _calendar(result)
    if tool == "search_gmail":
        return _gmail(result)
    if tool == "web_search":
        return _search(result)
    if tool == "create_notion_page":
        text = _plain(result)
        return re.sub(r"\s+URL:\s*\S+.*$", "", text or "").strip() or None
    return _plain(result)
