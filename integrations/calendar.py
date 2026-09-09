"""
J.A.R.V.I.S. — Google Calendar Integration
List past and future events using the read-only context credentials.
"""

import os
from datetime import datetime, timezone, timedelta

import re

GREEN  = "\033[92m"
RED    = "\033[91m"
RESET  = "\033[0m"


def _get_service():
    """Reuse the context module's read-only Google credentials."""
    from core.context import _get_google_creds
    from googleapiclient.discovery import build
    creds = _get_google_creds()
    if not creds:
        raise RuntimeError("Google credentials unavailable.")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_period(period: str) -> tuple[datetime, datetime]:  # noqa: F811
    """
    Convert a natural language period string into UTC (start, end) datetimes.
    Supports: today, yesterday, this week, last week, last 7 days,
              last N days, this month, last month.
    """
    now   = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    p = period.lower().strip()

    if p == "today":
        return today, today + timedelta(days=1)

    if p == "yesterday":
        return today - timedelta(days=1), today

    if p in ("this week", "current week"):
        week_start = today - timedelta(days=today.weekday())
        return week_start, week_start + timedelta(days=7)

    if p == "last week":
        week_start = today - timedelta(days=today.weekday() + 7)
        return week_start, week_start + timedelta(days=7)

    if p in ("last 7 days", "past 7 days", "past week"):
        return today - timedelta(days=7), now

    if p in ("last 30 days", "past 30 days", "this month"):
        return today - timedelta(days=30), now

    if p == "last month":
        first_this_month = today.replace(day=1)
        last_month_end   = first_this_month - timedelta(seconds=1)
        last_month_start = last_month_end.replace(day=1,
                                                   hour=0, minute=0, second=0)
        return last_month_start, first_this_month

    # Fallback: last N days
    import re
    m = re.search(r"(\d+)\s*days?", p)
    if m:
        n = int(m.group(1))
        return today - timedelta(days=n), now

    # Default: last 7 days
    return today - timedelta(days=7), now


def list_events(period: str = "today") -> str:
    """
    List calendar events for a given period.
    Returns a human-readable summary of events with title, time, and attendees.
    """
    try:
        service             = _get_service()
        time_min, time_max  = _parse_period(period)

        results = service.events().list(
            calendarId="primary",
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = results.get("items", [])
        if not events:
            return f"No calendar events found for '{period}', sir."

        lines = [f"Calendar events for {period} ({len(events)} found):\n"]
        for ev in events:
            title = ev.get("summary", "Unnamed event")
            start = ev["start"].get("dateTime", ev["start"].get("date", ""))
            end   = ev["end"].get("dateTime",   ev["end"].get("date",   ""))

            # Format times nicely
            if "T" in start:
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                end_dt   = datetime.fromisoformat(end.replace("Z", "+00:00"))
                # Convert to local time for display
                start_dt = start_dt.astimezone()
                end_dt   = end_dt.astimezone()
                time_str = (f"{start_dt.strftime('%a %b %-d, %-I:%M %p')} – "
                            f"{end_dt.strftime('%-I:%M %p')}")
            else:
                time_str = f"{start} (all day)"

            # Attendees
            attendees = ev.get("attendees", [])
            attendee_names = [
                a.get("displayName") or a.get("email", "")
                for a in attendees
                if not a.get("self")
            ]
            attendee_str = (", ".join(attendee_names[:4])
                            if attendee_names else "")

            location = ev.get("location", "")
            desc     = ev.get("description", "")[:100]

            entry = f"• {title} — {time_str}"
            if attendee_str:
                entry += f"\n  Attendees: {attendee_str}"
            if location:
                entry += f"\n  Location: {location}"
            if desc:
                entry += f"\n  Notes: {desc}"
            lines.append(entry)

        print(f"{GREEN}[CALENDAR] Listed {len(events)} events for '{period}'{RESET}",
              flush=True)
        return "\n".join(lines)

    except Exception as exc:
        print(f"{RED}[CALENDAR] list_events error: {exc}{RESET}", flush=True)
        return f"Could not retrieve calendar events, sir: {exc}"


def create_event(
    title: str,
    start: str,
    end: str,
    description: str = "",
    location: str    = "",
    attendees: list  = None,
) -> str:
    """
    Create a Google Calendar event.

    start / end: ISO 8601 strings — "2026-04-07T21:00:00" (local time assumed)
                 or "YYYY-MM-DD" for all-day events.
    attendees:   optional list of email address strings.
    Returns a confirmation string.
    """
    try:
        service = _get_service()

        # Determine if all-day or timed event
        all_day = "T" not in start

        if all_day:
            start_obj = {"date": start[:10]}
            end_obj   = {"date": end[:10]}
        else:
            # Attach local timezone offset so Google stores it correctly
            local_tz = datetime.now().astimezone().strftime("%z")  # e.g. "-0700"
            tz_str   = f"{local_tz[:3]}:{local_tz[3:]}"           # e.g. "-07:00"

            def _ensure_tz(dt_str: str) -> str:
                if "+" in dt_str[10:] or dt_str.endswith("Z"):
                    return dt_str
                return dt_str[:19] + tz_str

            start_obj = {"dateTime": _ensure_tz(start), "timeZone": _local_tz_name()}
            end_obj   = {"dateTime": _ensure_tz(end),   "timeZone": _local_tz_name()}

        body: dict = {
            "summary":     title,
            "start":       start_obj,
            "end":         end_obj,
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]

        event = service.events().insert(calendarId="primary", body=body).execute()
        link  = event.get("htmlLink", "")

        # Format confirmation
        if all_day:
            time_str = start[:10]
        else:
            start_dt = datetime.fromisoformat(event["start"]["dateTime"].replace("Z", "+00:00"))
            end_dt   = datetime.fromisoformat(event["end"]["dateTime"].replace("Z", "+00:00"))
            start_dt = start_dt.astimezone()
            end_dt   = end_dt.astimezone()
            time_str = (f"{start_dt.strftime('%a %b %-d at %-I:%M %p')} – "
                        f"{end_dt.strftime('%-I:%M %p')}")

        print(f"{GREEN}[CALENDAR] Created '{title}' — {time_str}{RESET}", flush=True)
        return f"Done, sir. '{title}' added to your calendar: {time_str}."

    except Exception as exc:
        print(f"{RED}[CALENDAR] create_event error: {exc}{RESET}", flush=True)
        return f"Could not create calendar event, sir: {exc}"


def _local_tz_name() -> str:
    """Return the local IANA timezone name, falling back to UTC offset."""
    try:
        import time as _time
        return _time.tzname[0]
    except Exception:
        return "UTC"
