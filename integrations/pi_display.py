"""
Push cards / Spotify / video to the Pi HDMI display.

Fire-and-forget HTTP, 0.8s timeout — never blocks the voice pipeline.
"""

from __future__ import annotations

import os
import threading
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from core.paths import ROOT

load_dotenv(ROOT / ".env")

GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"

TIMEOUT = 0.8
STATUS_TIMEOUT = 2.5


def _host() -> str:
    explicit = (os.environ.get("HOMELAB_HOST") or os.environ.get("PI_DISPLAY_HOST") or "").strip()
    if explicit:
        return explicit.split("://", 1)[-1].split("/", 1)[0]
    jelly = (os.environ.get("JELLYFIN_URL") or "").strip()
    if not jelly or "your-pi" in jelly:
        return ""
    parsed = urlparse(jelly if "://" in jelly else f"http://{jelly}")
    return parsed.hostname or ""


def _url() -> str:
    explicit = (os.environ.get("PI_DISPLAY_URL") or "").rstrip("/")
    if explicit and "your-pi" not in explicit:
        return explicit
    host = _host()
    if not host:
        return ""
    return f"http://{host}:8766"


def _token() -> str:
    return (os.environ.get("PI_MCP_TOKEN") or os.environ.get("PI_DISPLAY_TOKEN") or "").strip()


def configured() -> bool:
    url, tok = _url(), _token()
    return bool(url and tok and not tok.lower().startswith("your_"))


def status() -> dict:
    """Sync GET /status. Used by --test-display, not the voice path."""
    url, tok = _url(), _token()
    if not url or not tok or tok.lower().startswith("your_"):
        return {"ok": False, "error": "PI_DISPLAY_URL / PI_MCP_TOKEN not set"}
    try:
        r = httpx.get(
            f"{url}/status",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=STATUS_TIMEOUT,
        )
        data = r.json() if "json" in (r.headers.get("content-type") or "") else {}
        data["http"] = r.status_code
        data["url"] = url
        if r.status_code != 200:
            data["ok"] = False
            data.setdefault("error", r.text[:200])
        return data
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}


def health() -> dict:
    url = _url()
    if not url:
        return {"ok": False, "error": "no display URL"}
    try:
        r = httpx.get(f"{url}/health", timeout=STATUS_TIMEOUT)
        data = r.json() if "json" in (r.headers.get("content-type") or "") else {}
        data["http"] = r.status_code
        data["url"] = url
        return data
    except Exception as exc:
        return {"ok": False, "error": str(exc), "url": url}


def post(payload: dict) -> None:
    """Send a display update in the background. Never raises to the caller."""

    def _run():
        url = _url()
        tok = _token()
        if not url or not tok or tok.lower().startswith("your_"):
            return
        try:
            r = httpx.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {tok}",
                    "Content-Type": "application/json",
                },
                timeout=TIMEOUT,
            )
            kind = payload.get("type") or "?"
            print(f"{GREEN}[DISPLAY] {kind} HTTP {r.status_code}{RESET}", flush=True)
        except Exception as exc:
            print(f"{RED}[DISPLAY] {exc}{RESET}", flush=True)

    threading.Thread(target=_run, daemon=True).start()


def post_wait(payload: dict) -> dict:
    """Same as post(), but wait for the HTTP response (tests / diagnostics)."""
    url, tok = _url(), _token()
    if not url or not tok or tok.lower().startswith("your_"):
        return {"ok": False, "error": "not configured"}
    try:
        r = httpx.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
            },
            timeout=STATUS_TIMEOUT,
        )
        return {"ok": r.status_code == 200, "http": r.status_code, "body": r.text[:200]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def show_card(
    title: str,
    subtitle: str = "",
    overview: str = "",
    poster: str = "",
    lines: list | None = None,
    prompt: str = "",
    header: str = "JARVIS",
    ttl_seconds: int = 120,
) -> None:
    post({
        "type": "card",
        "ttl_seconds": ttl_seconds,
        "header": header,
        "title": title,
        "subtitle": subtitle,
        "overview": (overview or "")[:400],
        "poster": poster,
        "lines": lines or [],
        "prompt": prompt,
    })


def push_spotify(item: dict | None, playing: bool = True, progress_ms: int = 0) -> None:
    if not item:
        if not playing:
            post({"type": "spotify", "playing": False, "track": "", "artists": []})
        return
    album = item.get("album") or {}
    images = album.get("images") or []
    poster = ""
    if images:
        poster = images[0].get("url") or ""
    artists = item.get("artists") or []
    names = [a.get("name") for a in artists if isinstance(a, dict)]
    post({
        "type": "spotify",
        "playing": bool(playing),
        "id": item.get("id") or item.get("uri") or "",
        "track": item.get("name") or "",
        "artists": names,
        "poster": poster,
        "progress_ms": int(progress_ms or 0),
        "duration_ms": int(item.get("duration_ms") or 0),
    })


def play_video(path: str, title: str = "") -> None:
    if not path:
        return
    post({"type": "video", "path": path, "title": title})


def control_playback(
    action: str,
    percent: int | None = None,
    delta: int | None = None,
    fallback_spotify: bool = False,
) -> str:
    """Pause / resume / stop / volume for HDMI VLC. Used by the voice tool."""
    action = (action or "pause").lower().strip()
    if action in ("play", "unpause", "continue"):
        action = "resume"
    payload: dict = {"type": "control", "action": action}
    if percent is not None:
        payload["percent"] = int(percent)
    if delta is not None:
        payload["delta"] = int(delta)
    r = post_wait(payload)
    if r.get("ok"):
        print(f"{GREEN}[DISPLAY] control {action} ok{RESET}", flush=True)
        if action == "pause":
            return "Paused the Pi display, sir."
        if action == "resume":
            return "Resumed the Pi display, sir."
        if action == "stop":
            return "Stopped the Pi display, sir."
        if action == "volume":
            if percent is not None:
                return f"Pi volume is {int(percent)} percent, sir."
            if delta is not None and int(delta) > 0:
                return "Turned the Pi volume up, sir."
            return "Turned the Pi volume down, sir."
        return "Done, sir."
    if fallback_spotify and action in ("pause", "resume"):
        if action == "pause":
            from core.audio_duck import suppress_restore
            from integrations.spotify import pause
            suppress_restore()
            return pause()
        from integrations.spotify import resume
        return resume()
    print(f"{RED}[DISPLAY] control {action} failed {r}{RESET}", flush=True)
    return "Nothing is playing on the Pi display, sir."


def clear() -> None:
    post({"type": "clear"})
