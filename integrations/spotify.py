"""
J.A.R.V.I.S. — Spotify Integration
"""

import os
import time

import spotipy
from core.paths import DATA
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

SCOPES = (
    "user-read-playback-state "
    "user-modify-playback-state "
    "user-read-currently-playing "
    "streaming"
)

_sp: spotipy.Spotify | None = None


def _client() -> spotipy.Spotify:
    global _sp
    if _sp is None:
        _sp = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=os.environ.get("SPOTIFY_CLIENT_ID", ""),
                client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
                redirect_uri=os.environ.get("SPOTIFY_REDIRECT_URI",
                                            "http://127.0.0.1:8888/callback"),
                scope=SCOPES,
                cache_path=str(DATA / ".spotify_cache"),
                open_browser=True,
            ),
            # Never sleep on HTTP 429 — a Retry-After of ~1 hour was freezing
            # the wake-word loop so Jarvis detected "Hey Jarvis" then went mute.
            requests_timeout=3,
            retries=0,
            status_retries=0,
        )
    return _sp


def playback_state() -> dict | None:
    """Current Spotify playback payload, or None if unavailable."""
    try:
        return _client().current_playback()
    except Exception:
        return None


def _device_list(sp=None) -> list[dict]:
    sp = sp or _client()
    return (sp.devices() or {}).get("devices") or []


def _active_device_id(current: dict | None = None) -> str | None:
    if current:
        device_id = (current.get("device") or {}).get("id")
        if device_id:
            return device_id
    devices = _device_list()
    for d in devices:
        if d.get("is_active"):
            return d["id"]
    for d in devices:
        if (d.get("type") or "").lower() == "computer":
            return d["id"]
    return devices[0]["id"] if devices else None


def _pause_all_devices(sp) -> None:
    """Stop every Connect target so a new play can't layer on a stale session."""
    for d in _device_list(sp):
        did = d.get("id")
        if not did:
            continue
        try:
            sp.pause_playback(device_id=did)
        except Exception:
            pass


def _start_playback(sp, device: str, **kwargs) -> None:
    _pause_all_devices(sp)
    time.sleep(0.2)
    sp.start_playback(device_id=device, **kwargs)


def _track_label(item: dict | None) -> str | None:
    if not item:
        return None
    artists = item.get("artists") or []
    artist = artists[0]["name"] if artists else "Unknown"
    return f"{item.get('name', 'Unknown')} by {artist}"


def _uris_after_track(sp, item: dict | None) -> list[str]:
    """More tracks after a seed so skip isn't a dead end on a single URI."""
    if not item or not item.get("uri"):
        return []
    seed = item["uri"]
    uris: list[str] = []

    album_id = (item.get("album") or {}).get("id")
    if album_id:
        try:
            tracks = sp.album_tracks(album_id, limit=50)
            seen_seed = False
            for t in (tracks or {}).get("items") or []:
                if not t or not t.get("uri"):
                    continue
                if t["uri"] == seed:
                    seen_seed = True
                    continue
                if seen_seed:
                    uris.append(t["uri"])
        except Exception:
            pass

    artist_id = ((item.get("artists") or [{}])[0] or {}).get("id")
    if artist_id:
        try:
            top = sp.artist_top_tracks(artist_id, country="US")
            for t in (top or {}).get("tracks") or []:
                uri = t.get("uri") if t else None
                if uri and uri != seed and uri not in uris:
                    uris.append(uri)
        except Exception:
            pass
    return uris[:20]


def set_volume(percent: int, device_id: str | None = None) -> None:
    _client().volume(max(0, min(100, int(percent))), device_id=device_id)


def play(query: str, type: str = "track") -> str:
    try:
        sp     = _client()
        device = _active_device_id()
        if not device:
            return ("No active Spotify device detected, sir. "
                    "Open Spotify on any device first.")

        search_type = type if type in ("track", "artist", "playlist") else "track"
        item = _first_search_hit(sp, query, search_type)

        # Vague "play something" playlist searches often return null/unavailable
        # entries. Fall back to a track so we still play music.
        if item is None and search_type == "playlist":
            search_type = "track"
            item = _first_search_hit(sp, query, "track")

        if item is None:
            return f"No {search_type} found for '{query}', sir."

        if search_type == "track":
            uri  = item["uri"]
            name = _track_label(item) or item.get("name", query)
            # Album context so skip has a next track, without dumping a
            # 20-URI list that some clients start as overlapping playback.
            album_uri = (item.get("album") or {}).get("uri")
            if album_uri:
                _start_playback(
                    sp, device,
                    context_uri=album_uri,
                    offset={"uri": uri},
                )
            else:
                _start_playback(sp, device, uris=[uri])
        elif search_type == "artist":
            name = item["name"]
            track_results = sp.search(q=f"artist:{name}", type="track", limit=10)
            tracks = [
                t for t in (track_results or {}).get("tracks", {}).get("items", [])
                if t
            ]
            uris = [t["uri"] for t in tracks]
            if not uris:
                return f"No tracks found for {name}, sir."
            _start_playback(sp, device, uris=uris)
        else:  # playlist
            uri  = item["uri"]
            name = item["name"]
            _start_playback(sp, device, context_uri=uri, offset={"position": 0})

        print(f"{GREEN}[SPOTIFY] Playing: {name}{RESET}", flush=True)
        try:
            from core.audio_duck import notify_playback_started
            notify_playback_started()
        except Exception:
            pass
        return f"Playing {name}, sir."

    except Exception as exc:
        print(f"{RED}[SPOTIFY] play error: {exc}{RESET}", flush=True)
        return f"Spotify encountered an issue, sir: {exc}"


def _first_search_hit(sp, query: str, search_type: str) -> dict | None:
    """Return the first non-null search result, or None."""
    results = sp.search(q=query, type=search_type, limit=5)
    if not results:
        return None
    items = (results.get(f"{search_type}s") or {}).get("items") or []
    for item in items:
        if item and item.get("uri"):
            return item
    return None


def pause() -> str:
    try:
        current = playback_state()
        if current and not current.get("is_playing"):
            return "Playback paused, sir."
        device = _active_device_id(current)
        if not device:
            return "No active Spotify device, sir."
        _client().pause_playback(device_id=device)
        return "Playback paused, sir."
    except Exception as exc:
        if "Restriction violated" in str(exc) and not is_playing():
            return "Playback paused, sir."
        print(f"{RED}[SPOTIFY] pause error: {exc}{RESET}", flush=True)
        return f"Could not pause Spotify, sir: {exc}"


def _queue_has_next(sp) -> bool:
    try:
        queued = (sp.queue() or {}).get("queue") or []
        return any(t and t.get("uri") for t in queued)
    except Exception:
        return False


def skip() -> str:
    try:
        sp = _client()
        current = playback_state()
        device = _active_device_id(current)
        if not device:
            return "No active Spotify device, sir."

        # next_track on a single-URI play returns 204 then leaves an empty
        # session (same song / resume 403). If the queue is empty, jump to
        # related tracks instead.
        if not _queue_has_next(sp):
            follow = _uris_after_track(sp, (current or {}).get("item"))
            if follow:
                _start_playback(sp, device, uris=follow)
                label = _track_label((current or {}).get("item"))
                print(f"{GREEN}[SPOTIFY] Skip had no queue — continued from related tracks{RESET}", flush=True)
                return (
                    f"No next track in queue; started related songs"
                    f"{' after ' + label if label else ''}, sir."
                )

        sp.next_track(device_id=device)
        return "Skipped to the next track, sir."
    except Exception as exc:
        print(f"{RED}[SPOTIFY] skip error: {exc}{RESET}", flush=True)
        return f"Could not skip track, sir: {exc}"


def now_playing() -> str:
    try:
        current = playback_state()
        item = (current or {}).get("item") if current else None
        if not item:
            return "Nothing is playing, sir."
        artists = item.get("artists") or []
        artist = artists[0]["name"] if artists else "Unknown"
        track = item.get("name", "Unknown")
        label = f"{artist} — {track}"
        if current.get("is_playing"):
            return label
        ducked = False
        try:
            from core.audio_duck import is_ducked
            ducked = is_ducked()
        except Exception:
            pass
        if ducked:
            return f"{label} (active session, temporarily ducked for voice — treat as currently playing)"
        return f"{label} (paused)"
    except Exception as exc:
        print(f"{RED}[SPOTIFY] now_playing error: {exc}{RESET}", flush=True)
        return f"Could not retrieve playback info, sir: {exc}"


def is_playing() -> bool:
    """Return True if Spotify is currently playing."""
    try:
        current = playback_state()
        return bool(current and current.get("is_playing"))
    except Exception:
        return False


def resume() -> str:
    """Resume Spotify playback."""
    try:
        current = playback_state()
        device = _active_device_id(current)
        if not device:
            return "No active Spotify device, sir."
        if current and current.get("is_playing"):
            return "Resuming playback, sir."
        try:
            _client().start_playback(device_id=device)
            return "Resuming playback, sir."
        except Exception as exc:
            if "Restriction violated" in str(exc) and is_playing():
                return "Resuming playback, sir."
            item = (current or {}).get("item") or {}
            uri = item.get("uri")
            if uri:
                _client().start_playback(device_id=device, uris=[uri])
                return "Resuming playback, sir."
            raise exc
    except Exception as exc:
        print(f"{RED}[SPOTIFY] resume error: {exc}{RESET}", flush=True)
        return f"Could not resume Spotify, sir: {exc}"
