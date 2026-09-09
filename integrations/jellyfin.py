"""
Jellyfin — open a movie or TV episode in the Mac browser against the Pi.
"""

from __future__ import annotations

import os
import re
import subprocess
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

from core.paths import ROOT

load_dotenv(ROOT / ".env")

GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"

_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}

_SE = re.compile(r"\bs(\d{1,2})e(\d{1,3})\b", re.I)
_SEASON_EP = re.compile(
    r"season\s+(\w+)\s+episode\s+(\w+)",
    re.I,
)


def _base() -> str:
    return (os.environ.get("JELLYFIN_URL") or "").rstrip("/")


def _key() -> str:
    return (os.environ.get("JELLYFIN_API_KEY") or "").strip()


def configured() -> bool:
    k = _key()
    url = _base()
    if not url or "your-pi" in url or "tailscale-name" in url:
        return False
    return bool(k) and not k.lower().startswith("your_")


def _num(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    s = str(value).strip().lower()
    if s.isdigit():
        n = int(s)
        return n if n > 0 else None
    return _WORDS.get(s)


def _parse_title(title: str) -> tuple[str, int | None, int | None]:
    q = (title or "").strip()
    season = episode = None
    m = _SE.search(q)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        q = (_SE.sub(" ", q)).strip(" -–—")
    m = _SEASON_EP.search(q)
    if m:
        season = _num(m.group(1)) or season
        episode = _num(m.group(2)) or episode
        q = (_SEASON_EP.sub(" ", q)).strip(" -–—")
    q = re.sub(r"\s+", " ", q).strip(" .")
    q = re.sub(r"^(?:the movie|movie|of)\s+", "", q, flags=re.I)
    return q, season, episode


def _get(path: str, params: dict) -> dict:
    with httpx.Client(timeout=15.0) as client:
        r = client.get(
            f"{_base()}{path}",
            params=params,
            headers={"X-Emby-Token": _key(), "Accept": "application/json"},
        )
        r.raise_for_status()
        return r.json()


def _search(query: str, types: str, extra: dict | None = None, limit: int = 10) -> list[dict]:
    params = {
        "Recursive": "true",
        "SearchTerm": query,
        "IncludeItemTypes": types,
        "Limit": str(max(1, min(limit, 25))),
        "Fields": "ProductionYear,ParentIndexNumber,IndexNumber,SeriesName",
    }
    if extra:
        params.update(extra)
    return _get("/Items", params).get("Items") or []


def _pick_named(query: str, items: list[dict], field: str = "Name") -> dict | None:
    q = query.strip().lower()
    if not items:
        return None
    for it in items:
        name = (it.get(field) or it.get("Name") or "").lower()
        if name == q:
            return it
    for it in items:
        name = (it.get(field) or it.get("Name") or "").lower()
        series = (it.get("SeriesName") or "").lower()
        if name.startswith(q) or q in name or q == series or q in series:
            return it
    return items[0]


def _episode_for(series: dict, season: int, episode: int) -> dict | None:
    data = _get(
        f"/Shows/{series['Id']}/Episodes",
        {"season": str(season), "Fields": "ProductionYear,ParentIndexNumber,IndexNumber,SeriesName"},
    )
    items = data.get("Items") or []
    for it in items:
        if it.get("ParentIndexNumber") == season and it.get("IndexNumber") == episode:
            return it
    for it in items:
        if it.get("IndexNumber") == episode:
            return it
    return None


def _details_url(item_id: str) -> str:
    return f"{_base()}/web/#/details?id={quote(item_id, safe='')}"


def _open_url(url: str) -> None:
    # AppleScript keeps the fragment; `/usr/bin/open` often strips it.
    subprocess.run(["osascript", "-e", f'open location "{url}"'], check=False)


def _open(item: dict) -> str:
    itype = item.get("Type") or ""
    name = item.get("Name") or "that title"
    if itype == "Episode":
        series = item.get("SeriesName") or ""
        s = item.get("ParentIndexNumber")
        e = item.get("IndexNumber")
        label = f"{series} S{s:02d}E{e:02d} — {name}" if s and e else f"{series} — {name}"
    else:
        year = item.get("ProductionYear")
        label = f"{name} ({year})" if year else name
    url = _details_url(item["Id"])
    _open_url(url)
    print(f"{GREEN}[JELLYFIN] Opened {label} → {url}{RESET}", flush=True)
    return f"Opening {label} in Jellyfin, sir. Hit play in the browser if it doesn't start itself."


def play_movie(title: str, season=None, episode=None) -> str:
    """Open a Jellyfin movie or TV episode in the Mac browser."""
    raw = (title or "").strip()
    if not raw and not (season and episode):
        return "Which title should I put on, sir?"
    if not configured():
        return "Jellyfin isn't configured on this Mac, sir. Set JELLYFIN_API_KEY in .env."

    query, parsed_s, parsed_e = _parse_title(raw)
    season = _num(season) or parsed_s
    episode = _num(episode) or parsed_e
    if not query:
        return "Which show or movie, sir?"

    try:
        if season and episode:
            series = _pick_named(query, _search(query, "Series"))
            if not series:
                return f"I couldn't find the series '{query}' in Jellyfin, sir."
            item = _episode_for(series, season, episode)
            if not item:
                return (
                    f"I found {series.get('Name')}, but not season {season} "
                    f"episode {episode} in Jellyfin, sir."
                )
            return _open(item)

        movies = _search(query, "Movie")
        movie = _pick_named(query, movies)
        if movie and (movie.get("Name") or "").lower() == query.lower():
            return _open(movie)

        series = _pick_named(query, _search(query, "Series"))
        if series:
            return _open(series)

        if movie:
            return _open(movie)

        eps = _search(query, "Episode")
        ep = _pick_named(query, eps, "SeriesName")
        if ep:
            return _open(ep)
    except Exception as exc:
        print(f"{RED}[JELLYFIN] failed: {exc}{RESET}", flush=True)
        return f"Couldn't reach Jellyfin on the Pi, sir: {exc}"

    return f"I couldn't find '{query}' in Jellyfin, sir."
