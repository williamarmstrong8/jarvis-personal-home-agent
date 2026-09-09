"""
Open homelab web UIs in the Mac browser over Tailscale.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

from core.paths import ROOT

load_dotenv(ROOT / ".env")

GREEN = "\033[92m"
RESET = "\033[0m"


@dataclass(frozen=True)
class HomelabApp:
    key: str
    label: str
    port: int
    path: str
    aliases: tuple[str, ...]
    in_all: bool = True


APPS: tuple[HomelabApp, ...] = (
    HomelabApp("radarr", "Radarr", 7878, "/", ("radarr", "radar", "movie arr")),
    HomelabApp("sonarr", "Sonarr", 8989, "/", ("sonarr", "sonar", "tv arr")),
    HomelabApp("prowlarr", "Prowlarr", 9696, "/", ("prowlarr", "prowlar", "prowler", "prowlare")),
    HomelabApp("jellyfin", "Jellyfin", 8096, "/web/", ("jellyfin", "jelly fin", "jellyfan")),
    HomelabApp("plex", "Plex", 32400, "/web", ("plex",)),
    HomelabApp(
        "homeassistant", "Home Assistant", 8123, "/",
        ("homeassistant", "home assistant", "home assistance", "hass", "ha"),
    ),
    HomelabApp("seerr", "Seerr", 5055, "/", ("seerr", "seer", "overseerr", "overseer", "jellyseerr")),
    HomelabApp(
        "qbittorrent", "qBittorrent", 8080, "/",
        ("qbittorrent", "qbit", "q bit", "torrent", "torrents"),
    ),
    HomelabApp("n8n", "n8n", 5678, "/", ("n8n", "n eight n", "n8 n", "automation")),
    HomelabApp("lidarr", "Lidarr", 8686, "/", ("lidarr", "lidar", "music arr"), False),
    HomelabApp("bazarr", "Bazarr", 6767, "/", ("bazarr", "bazaar", "subtitles"), False),
    HomelabApp("nzbget", "NZBGet", 6789, "/", ("nzbget", "nzb get", "nzb"), False),
)

_ALL_ALIASES = {
    "all", "allapps", "allmyapps", "alltheapps", "everything",
    "homelab", "homelabapps", "piapps", "dashboards", "alldashboards",
    "theapps", "mysites", "thesites",
}


def _host() -> str:
    explicit = (os.environ.get("HOMELAB_HOST") or "").strip()
    if explicit:
        return explicit.split("://", 1)[-1].split("/", 1)[0]
    jelly = (os.environ.get("JELLYFIN_URL") or "").strip()
    if not jelly or "your-pi" in jelly or "tailscale-name" in jelly:
        return ""
    parsed = urlparse(jelly if "://" in jelly else f"http://{jelly}")
    return parsed.hostname or ""


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _open_url(url: str) -> None:
    subprocess.run(["osascript", "-e", f'open location "{url}"'], check=False)


def url_for(app: HomelabApp) -> str:
    path = app.path if app.path.startswith("/") else f"/{app.path}"
    return f"http://{_host()}:{app.port}{path}"


def _match_one(token: str) -> HomelabApp | None:
    compact = _compact(token)
    if not compact:
        return None
    for app in APPS:
        names = {_compact(app.key), _compact(app.label), *(_compact(a) for a in app.aliases)}
        if compact in names:
            return app
    for app in APPS:
        names = {_compact(app.key), *(_compact(a) for a in app.aliases)}
        if any(compact.startswith(n) or n.startswith(compact) for n in names if len(compact) >= 4):
            return app
    return None


def resolve_apps(name: str) -> list[HomelabApp] | None:
    raw = (name or "").strip().lower()
    raw = re.sub(r"\s+in (?:my |the )?browser$", "", raw)
    raw = re.sub(r"^(?:the |my |web ui for |website for )+", "", raw)
    if not raw:
        return None
    if _compact(raw) in _ALL_ALIASES:
        return [app for app in APPS if app.in_all]
    parts = [p for p in re.split(r"\s*(?:,|&| and | plus | then )\s*", raw) if p]
    found: list[HomelabApp] = []
    seen: set[str] = set()
    for part in parts or [raw]:
        if _compact(part) in _ALL_ALIASES:
            return [app for app in APPS if app.in_all]
        app = _match_one(part)
        if app and app.key not in seen:
            seen.add(app.key)
            found.append(app)
    return found or None


def known_apps() -> str:
    return ", ".join(app.label for app in APPS)


def open_homelab(app: str) -> str:
    """Open one or more homelab web UIs in the Mac browser."""
    if not _host():
        return "Homelab host isn't configured, sir. Set HOMELAB_HOST or JELLYFIN_URL in .env."
    targets = resolve_apps(app)
    if not targets:
        return (
            f"Which app, sir? I can open {known_apps()}, "
            "or all of the main dashboards."
        )
    opened = []
    for i, target in enumerate(targets):
        url = url_for(target)
        _open_url(url)
        opened.append(target.label)
        print(f"{GREEN}[HOMELAB] Opened {target.label} → {url}{RESET}", flush=True)
        if i + 1 < len(targets):
            time.sleep(0.2)
    if len(opened) == 1:
        return f"Opening {opened[0]} in the browser, sir."
    return f"Opening {len(opened)} homelab apps: {', '.join(opened)}."
