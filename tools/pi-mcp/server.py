#!/usr/bin/env python3
"""pi-lab MCP server — homelab tools for Jarvis.

Typed *arr / Plex / Docker helpers plus a bounded shell so the Mac-side
assistant can operate this Raspberry Pi without a raw unattended root login.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from mcp.server.mcpserver import MCPServer

SERVARR = "/docker/servarr"
RUNDIR = os.environ.get("PI_DISPLAY_DIR", "/run/pi-display")
OVERRIDE = os.path.join(RUNDIR, "override.json")
_HOME = os.path.expanduser(os.environ.get("HOME") or "~")
PLEX_PREFS = os.environ.get("PLEX_PREFS") or os.path.join(
    _HOME,
    "docker/plex/config/Library/Application Support/Plex Media Server/Preferences.xml",
)
SEERR_SETTINGS = "/docker/servarr/seerr/settings.json"

RADARR = ("radarr", 7878, "v3")
SONARR = ("sonarr", 8989, "v3")
LIDARR = ("lidarr", 8686, "v1")

SERVICES = [
    "plex", "jellyfin", "sonarr", "radarr", "prowlarr", "qbittorrent",
    "gluetun", "seerr", "bazarr", "lidarr", "nzbget", "flaresolverr",
    "homeassistant", "n8n", "caddy", "deunhealth",
]

mcp = MCPServer(
    "pi-lab",
    instructions=(
        "Tools for this Raspberry Pi homelab. "
        "Radarr movies live in /data/movies, Sonarr TV in /data/tv, Plex on :32400. "
        "For 'what movies do I have' call list_movies (Radarr library). "
        "For TV call list_series. Plex search is plex_search. "
        "Pi 4 cannot transcode — prefer 1080p x264/AVC; list_releases marks recommended=true. "
        "Confirm with the user via show_card before grabbing anything over 10 GB. "
        "Use run_command for anything without a typed tool. docker_restart only named stack services."
    ),
)


def _key(app: str) -> str:
    with open(os.path.join(SERVARR, app, "config.xml")) as f:
        m = re.search("<ApiKey>([^<]+)", f.read())
    if not m:
        raise RuntimeError("no api key for " + app)
    return m.group(1)


def _api(target, path: str, method: str = "GET", body: Any = None, timeout: int = 45):
    app, port, ver = target
    url = "http://127.0.0.1:%d/api/%s%s" % (port, ver, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"X-Api-Key": _key(app), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _sh(cmd: str, timeout: int = 15) -> str:
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout,
        ).stdout.strip()
    except Exception:
        return ""


def _gb(n) -> float:
    return round((n or 0) / 1024 ** 3, 2)


def _poster(images):
    for i in images or []:
        if i.get("coverType") == "poster":
            return i.get("remoteUrl") or i.get("url")
    return None


_BAD = re.compile(r"2160p|4k|hevc|x265|h\.?265|av1|remux|10.?bit", re.I)
_GOOD = re.compile(r"1080p", re.I)
_AVC = re.compile(r"x264|h\.?264|avc", re.I)


def _rank(title: str, size_bytes: int):
    t = title or ""
    if _BAD.search(t):
        return False, "needs transcoding or is direct-play-only on this Pi"
    if not _GOOD.search(t):
        return False, "not 1080p"
    if size_bytes > 15 * 1024 ** 3:
        return False, "oversized for available disk"
    if _AVC.search(t):
        return True, "1080p AVC - direct plays everywhere"
    return True, "1080p, codec unstated"


def _plex_token() -> str:
    text = open(PLEX_PREFS).read()
    m = re.search(r'PlexOnlineToken="([^"]+)"', text)
    if not m:
        raise RuntimeError("no Plex token")
    return m.group(1)


def _plex(path: str, timeout: int = 20):
    url = "http://127.0.0.1:32400" + path
    sep = "&" if "?" in path else "?"
    url = "%s%sX-Plex-Token=%s" % (url, sep, urllib.parse.quote(_plex_token()))
    req = urllib.request.Request(url, headers={"Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ET.fromstring(r.read())


def _seerr_key() -> str:
    data = json.load(open(SEERR_SETTINGS))
    key = ((data.get("main") or {}).get("apiKey")) or ""
    if not key:
        raise RuntimeError("no Seerr API key")
    return key


def _seerr(path: str, method: str = "GET", body: Any = None, timeout: int = 20):
    url = "http://127.0.0.1:5055/api/v1" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"X-Api-Key": _seerr_key(), "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _movie_row(m: dict) -> dict:
    mf = m.get("movieFile") or {}
    q = ((mf.get("quality") or {}).get("quality") or {}).get("name")
    return {
        "movie_id": m.get("id"),
        "title": m.get("title"),
        "year": m.get("year"),
        "has_file": bool(m.get("hasFile")),
        "size_gb": _gb(m.get("sizeOnDisk")),
        "quality": q,
        "tmdb_id": m.get("tmdbId"),
        "monitored": m.get("monitored"),
        "path": m.get("path"),
    }


def _series_row(s: dict) -> dict:
    stats = s.get("statistics") or {}
    return {
        "series_id": s.get("id"),
        "title": s.get("title"),
        "year": s.get("year"),
        "network": s.get("network"),
        "seasons": s.get("seasonCount") or stats.get("seasonCount"),
        "episode_file_count": stats.get("episodeFileCount"),
        "episode_count": stats.get("episodeCount"),
        "size_gb": _gb(stats.get("sizeOnDisk") or s.get("sizeOnDisk")),
        "monitored": s.get("monitored"),
        "status": s.get("status"),
        "tvdb_id": s.get("tvdbId"),
        "path": s.get("path"),
    }


@mcp.tool()
def get_status() -> dict:
    """Health of the homelab: containers, disk, memory, temperature, active
    downloads, and whether the host currently has working outbound internet."""
    out = {}
    svc = {}
    for line in _sh("docker ps -a --format \"{{.Names}}|{{.State}}|{{.Status}}\"", 20).splitlines():
        p = line.split("|")
        if len(p) >= 3 and p[0] in SERVICES:
            svc[p[0]] = "down" if p[1] != "running" else ("unhealthy" if "unhealthy" in p[2] else "up")
    out["services"] = svc
    out["services_down"] = sorted([k for k, v in svc.items() if v != "up"])

    try:
        v = os.statvfs("/")
        out["disk"] = {
            "free_gb": _gb(v.f_bavail * v.f_frsize),
            "used_pct": round(100 * (v.f_blocks - v.f_bfree) / v.f_blocks, 1),
        }
    except Exception:
        pass
    mem = {}
    try:
        for line in open("/proc/meminfo"):
            k, _, rest = line.partition(":")
            mem[k] = float(rest.strip().split()[0]) * 1024
        out["memory"] = {
            "used_pct": round(100 * (mem["MemTotal"] - mem["MemAvailable"]) / mem["MemTotal"], 1),
            "swap_used_gb": _gb(mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)),
        }
    except Exception:
        pass
    try:
        out["cpu_temp_c"] = round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000.0, 1)
        out["load_1m"] = float(open("/proc/loadavg").read().split()[0])
    except Exception:
        pass

    dl = []
    for tgt, label in ((RADARR, "movie"), (SONARR, "tv")):
        try:
            q = _api(tgt, "/queue?pageSize=40", timeout=20) or {}
            seen = set()
            for r in q.get("records", []):
                t = (r.get("title") or "")[:70]
                if t in seen:
                    continue
                seen.add(t)
                size = r.get("size") or 1
                dl.append({
                    "kind": label, "title": t,
                    "percent": round(100 * (1 - (r.get("sizeleft") or 0) / size), 1),
                    "status": r.get("status"), "eta": r.get("timeleft"),
                })
        except Exception as e:
            dl.append({"kind": label, "error": str(e)[:80]})
    out["downloads"] = dl

    s = socket.socket(); s.settimeout(5)
    try:
        s.connect(("1.1.1.1", 443)); out["internet"] = True
    except Exception:
        out["internet"] = False
    finally:
        s.close()
    return out


@mcp.tool()
def list_movies(query: str = "", downloaded_only: bool = False, limit: int = 50) -> dict:
    """List movies in the Radarr library (what is on this Pi). Filter by title
    with query. downloaded_only=true hides titles that are monitored but not on disk."""
    movies = _api(RADARR, "/movie") or []
    q = (query or "").strip().lower()
    rows = []
    for m in movies:
        if downloaded_only and not m.get("hasFile"):
            continue
        title = m.get("title") or ""
        if q and q not in title.lower() and q not in str(m.get("year") or ""):
            continue
        rows.append(_movie_row(m))
    rows.sort(key=lambda r: ((r.get("title") or "").lower(), r.get("year") or 0))
    cap = max(1, min(int(limit or 50), 200))
    return {
        "total": len(rows),
        "downloaded": sum(1 for r in rows if r.get("has_file")),
        "movies": rows[:cap],
    }


@mcp.tool()
def search_movie(query: str, limit: int = 5) -> list:
    """Look up movies by title through Radarr and TMDB. Returns candidates with a
    tmdb_id and a poster URL suitable for show_card. Adds nothing."""
    res = _api(RADARR, "/movie/lookup?term=" + urllib.parse.quote(query)) or []
    outs = []
    for m in res[:max(1, min(limit, 10))]:
        outs.append({
            "tmdb_id": m.get("tmdbId"), "title": m.get("title"), "year": m.get("year"),
            "runtime_min": m.get("runtime"),
            "tmdb_rating": ((m.get("ratings") or {}).get("tmdb") or {}).get("value"),
            "overview": (m.get("overview") or "")[:400],
            "poster": _poster(m.get("images")),
            "already_in_library": bool(m.get("id")),
            "radarr_movie_id": m.get("id") or None,
        })
    return outs


@mcp.tool()
def add_movie(tmdb_id: int, quality_profile_id: int = 7) -> dict:
    """Add a movie to Radarr so releases can be searched. Idempotent - returns the
    existing entry if already present. Profile 7 is Most Compatible, which prefers x264."""
    existing = _api(RADARR, "/movie") or []
    for m in existing:
        if m.get("tmdbId") == tmdb_id:
            return {
                "movie_id": m["id"], "title": m["title"], "already_present": True,
                "has_file": m.get("hasFile"),
            }
    look = _api(RADARR, "/movie/lookup?term=tmdb:%d" % tmdb_id) or []
    if not look:
        return {"error": "tmdb id not found"}
    m = look[0] if isinstance(look, list) else look
    m["qualityProfileId"] = quality_profile_id
    m["rootFolderPath"] = "/data/movies"
    m["monitored"] = True
    m["minimumAvailability"] = "released"
    m["addOptions"] = {"searchForMovie": False, "monitor": "movieOnly"}
    added = _api(RADARR, "/movie", "POST", m)
    return {
        "movie_id": added.get("id"), "title": added.get("title"),
        "already_present": False, "path": added.get("path"),
    }


@mcp.tool()
def list_releases(movie_id: int, limit: int = 12) -> list:
    """Interactive-search a movie already added to Radarr. Returns releases with the
    guid and indexer_id that grab_release needs, plus a recommended flag that
    accounts for this host being unable to transcode."""
    rel = _api(RADARR, "/release?movieId=%d" % movie_id, timeout=180) or []
    outs = []
    for r in rel:
        if r.get("rejected"):
            continue
        rec, why = _rank(r.get("title", ""), r.get("size") or 0)
        outs.append({
            "title": r.get("title"), "size_gb": _gb(r.get("size")),
            "seeders": r.get("seeders"), "indexer": r.get("indexer"),
            "quality": ((r.get("quality") or {}).get("quality") or {}).get("name"),
            "recommended": rec, "reason": why,
            "guid": r.get("guid"), "indexer_id": r.get("indexerId"),
        })
    outs.sort(key=lambda x: (not x["recommended"], -(x["seeders"] or 0)))
    return outs[:max(1, min(limit, 30))]


@mcp.tool()
def grab_release(movie_id: int, guid: str, indexer_id: int) -> dict:
    """Send one specific release to the download client. This starts a real download,
    so confirm with the user first for anything large."""
    _api(RADARR, "/release", "POST", {"guid": guid, "indexerId": indexer_id}, timeout=120)
    time.sleep(2)
    q = _api(RADARR, "/queue?pageSize=20") or {}
    for r in q.get("records", []):
        if r.get("movieId") == movie_id:
            return {"queued": True, "title": r.get("title"), "status": r.get("status")}
    return {"queued": True, "note": "accepted by Radarr, not yet visible in the queue"}


@mcp.tool()
def list_series(query: str = "", limit: int = 50) -> dict:
    """List TV series in the Sonarr library on this Pi."""
    series = _api(SONARR, "/series") or []
    q = (query or "").strip().lower()
    rows = []
    for s in series:
        title = s.get("title") or ""
        if q and q not in title.lower():
            continue
        rows.append(_series_row(s))
    rows.sort(key=lambda r: (r.get("title") or "").lower())
    cap = max(1, min(int(limit or 50), 200))
    return {"total": len(rows), "series": rows[:cap]}


@mcp.tool()
def search_series(query: str, limit: int = 5) -> list:
    """Look up TV series through Sonarr/TVDB. Adds nothing."""
    res = _api(SONARR, "/series/lookup?term=" + urllib.parse.quote(query)) or []
    outs = []
    for s in res[:max(1, min(limit, 10))]:
        outs.append({
            "tvdb_id": s.get("tvdbId"), "title": s.get("title"), "year": s.get("year"),
            "network": s.get("network"), "overview": (s.get("overview") or "")[:400],
            "poster": _poster(s.get("images")),
            "already_in_library": bool(s.get("id")),
            "sonarr_series_id": s.get("id") or None,
        })
    return outs


@mcp.tool()
def add_series(tvdb_id: int, quality_profile_id: int = 1) -> dict:
    """Add a TV series to Sonarr. Idempotent if already present."""
    existing = _api(SONARR, "/series") or []
    for s in existing:
        if s.get("tvdbId") == tvdb_id:
            return {"series_id": s["id"], "title": s["title"], "already_present": True}
    look = _api(SONARR, "/series/lookup?term=tvdb:%d" % tvdb_id) or []
    if not look:
        return {"error": "tvdb id not found"}
    s = look[0] if isinstance(look, list) else look
    s["qualityProfileId"] = quality_profile_id
    s["rootFolderPath"] = "/data/tv"
    s["monitored"] = True
    s["addOptions"] = {"searchForMissingEpisodes": False, "monitor": "all"}
    added = _api(SONARR, "/series", "POST", s)
    return {
        "series_id": added.get("id"), "title": added.get("title"),
        "already_present": False, "path": added.get("path"),
    }


@mcp.tool()
def list_episodes(series_id: int, season: int | None = None, missing_only: bool = False,
                  limit: int = 40) -> list:
    """List episodes for a Sonarr series. Optionally one season, or only missing files."""
    path = "/episode?seriesId=%d" % series_id
    eps = _api(SONARR, path) or []
    outs = []
    for e in eps:
        if season is not None and e.get("seasonNumber") != season:
            continue
        has = bool(e.get("hasFile"))
        if missing_only and has:
            continue
        outs.append({
            "episode_id": e.get("id"),
            "season": e.get("seasonNumber"),
            "episode": e.get("episodeNumber"),
            "title": e.get("title"),
            "has_file": has,
            "air_date": e.get("airDate"),
            "monitored": e.get("monitored"),
        })
    return outs[:max(1, min(int(limit or 40), 200))]


@mcp.tool()
def plex_library(section: str = "movie", limit: int = 50) -> dict:
    """List items in Plex. section: movie or show."""
    root = _plex("/library/sections")
    want = "movie" if (section or "movie").lower().startswith("movie") else "show"
    key = None
    title = None
    for d in root.findall("Directory"):
        if d.get("type") == want:
            key, title = d.get("key"), d.get("title")
            break
    if not key:
        return {"error": "no Plex section for " + want}
    lib = _plex("/library/sections/%s/all" % key)
    items = []
    tag = "Video" if want == "movie" else "Directory"
    for el in lib.findall(tag):
        items.append({
            "title": el.get("title"),
            "year": el.get("year"),
            "rating": el.get("rating"),
            "summary": (el.get("summary") or "")[:240],
        })
        if len(items) >= max(1, min(int(limit or 50), 200)):
            break
    return {"section": title, "type": want, "total_listed": len(items), "items": items}


@mcp.tool()
def plex_search(query: str, limit: int = 8) -> list:
    """Search Plex movies and shows by title."""
    root = _plex("/search?query=" + urllib.parse.quote(query))
    outs = []
    for el in list(root):
        if el.tag not in ("Video", "Directory"):
            continue
        outs.append({
            "type": el.get("type"),
            "title": el.get("title"),
            "year": el.get("year"),
            "summary": (el.get("summary") or "")[:240],
        })
        if len(outs) >= max(1, min(int(limit or 8), 20)):
            break
    return outs


@mcp.tool()
def plex_now_playing() -> dict:
    """What Plex is currently playing, if anything."""
    root = _plex("/status/sessions")
    sessions = []
    for v in root.findall("Video"):
        player = v.find("Player")
        user = v.find("User")
        sessions.append({
            "title": v.get("grandparentTitle") or v.get("title"),
            "episode": v.get("title") if v.get("grandparentTitle") else None,
            "player": None if player is None else player.get("title"),
            "user": None if user is None else user.get("title"),
            "state": None if player is None else player.get("state"),
        })
    return {"playing": bool(sessions), "sessions": sessions}


@mcp.tool()
def list_requests(limit: int = 15) -> list:
    """Pending / recent Seerr (Overseerr-style) media requests."""
    data = _seerr("/request?take=%d&skip=0&sort=added" % max(1, min(int(limit or 15), 50)))
    results = (data or {}).get("results") or data or []
    if isinstance(results, dict):
        results = results.get("results") or []
    outs = []
    for r in results[: max(1, min(int(limit or 15), 50))]:
        media = r.get("media") or {}
        outs.append({
            "id": r.get("id"),
            "status": r.get("status"),
            "type": media.get("mediaType"),
            "tmdb_id": media.get("tmdbId"),
            "tvdb_id": media.get("tvdbId"),
        })
    return outs


@mcp.tool()
def list_media_files(kind: str = "movies", limit: int = 80) -> dict:
    """List folders on disk. kind: movies (/data/movies), tv (/data/tv),
    downloads (/data/downloads)."""
    roots = {
        "movies": "/data/movies",
        "tv": "/data/tv",
        "downloads": "/data/downloads",
    }
    k = (kind or "movies").lower().strip()
    path = roots.get(k)
    if not path:
        return {"error": "kind must be movies, tv, or downloads"}
    names = []
    try:
        names = sorted(os.listdir(path))
    except Exception as exc:
        return {"error": str(exc)}
    cap = max(1, min(int(limit or 80), 300))
    return {"path": path, "total": len(names), "entries": names[:cap]}


@mcp.tool()
def docker_restart(name: str) -> dict:
    """Restart one Docker service from the homelab stack (plex, radarr, sonarr, …)."""
    name = (name or "").strip().lower()
    if name not in SERVICES:
        return {"error": "unknown service", "allowed": SERVICES}
    out = _sh("docker restart %s" % name, timeout=45)
    return {"restarted": name, "output": out or "ok"}


@mcp.tool()
def docker_logs(name: str, tail: int = 40) -> dict:
    """Tail logs for one Docker service in the homelab stack."""
    name = (name or "").strip().lower()
    if name not in SERVICES:
        return {"error": "unknown service", "allowed": SERVICES}
    n = max(5, min(int(tail or 40), 200))
    out = _sh("docker logs --tail %d %s 2>&1" % (n, name), timeout=20)
    return {"name": name, "log": (out or "")[-8000:]}


@mcp.tool()
def run_command(command: str, timeout_seconds: int = 20) -> dict:
    """Run a shell command on the Pi as the logged-in user (not root).
    Use for anything without a typed tool: files, grep, systemctl --user, curl to
    localhost services. Timeout 5–60s. Output truncated."""
    cmd = (command or "").strip()
    if not cmd:
        return {"error": "empty command"}
    t = max(5, min(int(timeout_seconds or 20), 60))
    try:
        p = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=t,
            cwd=_HOME,
        )
        stdout = (p.stdout or "")[-6000:]
        stderr = (p.stderr or "")[-2000:]
        return {
            "returncode": p.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        return {"error": "timed out after %ds" % t}
    except Exception as exc:
        return {"error": str(exc)[:200]}


@mcp.tool()
def show_card(title: str, subtitle: str = "", overview: str = "", poster: str = "",
              lines: list | None = None, prompt: str = "",
              header: str = "AGENT", ttl_seconds: int = 180) -> dict:
    """Display a full-screen card on the monitor attached to the Pi - poster image on
    the left, metadata on the right, prompt along the bottom. Powers the display on and
    takes priority over the Spotify now-playing screen until it expires or is cleared.
    Entries in lines are rendered as key: value rows."""
    os.makedirs(RUNDIR, exist_ok=True)
    payload = {
        "header": header, "title": title, "subtitle": subtitle,
        "overview": overview, "poster": poster, "lines": lines or [],
        "prompt": prompt,
    }
    tmp = OVERRIDE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"expires_at": time.time() + max(5, min(ttl_seconds, 1800)),
                   "payload": payload}, f)
    os.replace(tmp, OVERRIDE)
    return {"shown": True, "expires_in_seconds": ttl_seconds}


@mcp.tool()
def clear_card() -> dict:
    """Remove any card from the monitor, handing the display back to the Spotify
    now-playing screen, or powering it off if nothing is playing."""
    try:
        os.remove(OVERRIDE)
    except FileNotFoundError:
        pass
    return {"cleared": True}


@mcp.tool()
def now_playing() -> dict:
    """What the Spotify display is currently showing, read from state published by
    the display service. Read-only."""
    try:
        d = json.load(open(os.path.join(RUNDIR, "nowplaying.json")))
        if time.time() - d.get("updated_at", 0) > 30:
            return {"playing": False, "note": "display service state is stale"}
        return d
    except Exception:
        return {"playing": False, "note": "no state published yet"}
