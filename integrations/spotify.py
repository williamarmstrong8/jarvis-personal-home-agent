"""
J.A.R.V.I.S. — Spotify Integration
"""

import os
import re
import socket
import subprocess
import sys
import threading
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
_display_sync_lock = threading.Lock()
_display_sync_generation = 0
DISPLAY_SYNC_TIMEOUT = max(
    1.0, float(os.environ.get("SPOTIFY_DISPLAY_SYNC_TIMEOUT", "4.0"))
)
DISPLAY_SYNC_INTERVAL = max(
    0.12, float(os.environ.get("SPOTIFY_DISPLAY_SYNC_INTERVAL", "0.25"))
)


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


def _mirror_display(item: dict | None, playing: bool = True, progress_ms: int = 0) -> None:
    try:
        from integrations.pi_display import push_spotify
        push_spotify(item, playing=playing, progress_ms=progress_ms)
    except Exception:
        pass


def _item_keys(item: dict | None) -> set[str]:
    if not item:
        return set()
    return {str(v) for v in (item.get("id"), item.get("uri")) if v}


def _next_display_sync() -> int:
    global _display_sync_generation
    with _display_sync_lock:
        _display_sync_generation += 1
        return _display_sync_generation


def _display_sync_current(generation: int) -> bool:
    with _display_sync_lock:
        return generation == _display_sync_generation


def _playback_matches_transition(
    current: dict | None,
    *,
    expected_keys: set[str],
    previous_keys: set[str],
    expected_context_uri: str | None,
    require_playing: bool | None,
) -> bool:
    item = (current or {}).get("item")
    item_keys = _item_keys(item)
    context_uri = ((current or {}).get("context") or {}).get("uri")
    playing = bool(current and current.get("is_playing"))
    return bool(
        item is not None
        and (require_playing is None or playing == require_playing)
        and (not expected_keys or bool(expected_keys & item_keys))
        and (not previous_keys or not bool(previous_keys & item_keys))
        and (not expected_context_uri or context_uri == expected_context_uri)
    )


def _sync_display_after_transition(
    *,
    expected_item: dict | str | None = None,
    previous_item: dict | None = None,
    expected_context_uri: str | None = None,
    require_playing: bool | None = True,
) -> None:
    """Poll briefly until Spotify exposes the post-command state.

    Spotify Connect is eventually consistent. A single read at 250 ms often
    returns the old song, which used to leave the Pi stale until its slow
    fallback poll. Only the newest transition owns the display sync worker.
    """
    generation = _next_display_sync()
    if isinstance(expected_item, dict):
        expected_keys = _item_keys(expected_item)
    elif expected_item:
        expected_keys = {str(expected_item)}
    else:
        expected_keys = set()
    previous_keys = _item_keys(previous_item)

    def _run():
        deadline = time.monotonic() + DISPLAY_SYNC_TIMEOUT
        time.sleep(0.12)
        while _display_sync_current(generation) and time.monotonic() < deadline:
            cur = playback_state()
            item = (cur or {}).get("item")
            playing = bool(cur and cur.get("is_playing"))
            state_matches = _playback_matches_transition(
                cur,
                expected_keys=expected_keys,
                previous_keys=previous_keys,
                expected_context_uri=expected_context_uri,
                require_playing=require_playing,
            )
            if state_matches:
                _mirror_display(
                    item,
                    playing=playing,
                    progress_ms=(cur or {}).get("progress_ms") or 0,
                )
                return
            time.sleep(DISPLAY_SYNC_INTERVAL)

    threading.Thread(
        target=_run,
        daemon=True,
        name="jarvis-spotify-display-sync",
    ).start()


def playback_state() -> dict | None:
    """Current Spotify playback payload, or None if unavailable."""
    try:
        return _client().current_playback()
    except Exception:
        return None


def pause_for_voice() -> bool:
    """Pause active playback after a single playback-state request."""
    current = playback_state()
    if not current or not current.get("is_playing"):
        return False
    device = _active_device_id(current)
    if not device:
        return False
    _client().pause_playback(device_id=device)
    _next_display_sync()
    _mirror_display(
        current.get("item"),
        playing=False,
        progress_ms=current.get("progress_ms") or 0,
    )
    return True


def _device_list(sp=None) -> list[dict]:
    sp = sp or _client()
    return (sp.devices() or {}).get("devices") or []


def _local_spotify_enabled() -> bool:
    raw = os.environ.get("SPOTIFY_LAUNCH_LOCAL", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _name_matches(device_name: str, aliases: list[str]) -> bool:
    device_key = _name_key(device_name)
    if len(device_key) < 4:
        return False
    for alias in aliases:
        alias_key = _name_key(alias)
        if len(alias_key) < 4:
            continue
        if alias_key in device_key or device_key in alias_key:
            return True
    return False


def _this_machine_names() -> list[str]:
    """Names Spotify may use for this Mac (override, ComputerName, hostname)."""
    names: list[str] = []
    override = os.environ.get("SPOTIFY_DEVICE_NAME", "").strip()
    if override:
        names.append(override)
    if sys.platform == "darwin":
        for key in ("ComputerName", "LocalHostName"):
            try:
                result = subprocess.run(
                    ["scutil", "--get", key],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                value = (result.stdout or "").strip()
                if value:
                    names.append(value)
            except Exception:
                pass
    host = (socket.gethostname() or "").strip()
    if host:
        names.append(host)
        names.append(host.split(".")[0])
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        key = name.lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def _pick_device_id(devices: list[dict], preferred: str | None = None) -> str | None:
    """Choose a Connect target for pause/skip of whatever is already playing."""
    if not devices:
        return None
    if preferred is None:
        preferred = os.environ.get("SPOTIFY_DEVICE_NAME", "")
    needle = (preferred or "").strip().lower()
    if needle:
        for d in devices:
            name = (d.get("name") or "").lower()
            if needle in name and d.get("id"):
                return d["id"]
    for d in devices:
        if d.get("is_active") and d.get("id"):
            return d["id"]
    for d in devices:
        if (d.get("type") or "").lower() == "computer" and d.get("id"):
            return d["id"]
    for d in devices:
        if d.get("id"):
            return d["id"]
    return None


def _pick_local_device_id(
    devices: list[dict], aliases: list[str] | None = None
) -> str | None:
    """This Mac's Connect device — never the other computer that was last active."""
    aliases = _this_machine_names() if aliases is None else aliases
    for d in devices:
        if d.get("id") and _name_matches(d.get("name") or "", aliases):
            return d["id"]
    return None


def _device_name(devices: list[dict], device_id: str | None) -> str:
    for d in devices:
        if d.get("id") == device_id:
            return d.get("name") or device_id or ""
    return device_id or ""


def _active_device_id(current: dict | None = None) -> str | None:
    if current:
        device_id = (current.get("device") or {}).get("id")
        if device_id:
            return device_id
    return _pick_device_id(_device_list())


def _launch_local_spotify() -> bool:
    """Start the Mac Spotify app in the background so it registers as Connect."""
    if not _local_spotify_enabled() or sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["open", "-gj", "-a", "Spotify"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            print(f"{YELLOW}[SPOTIFY] Could not open local app: {err}{RESET}", flush=True)
            return False
        print(f"{GREEN}[SPOTIFY] Opening Spotify on this Mac{RESET}", flush=True)
        return True
    except Exception as exc:
        print(f"{YELLOW}[SPOTIFY] Local launch failed: {exc}{RESET}", flush=True)
        return False


def _play_uri_local(uri: str) -> bool:
    """Play a Spotify URI through the Mac desktop app (no Connect device required)."""
    if not _local_spotify_enabled() or sys.platform != "darwin":
        return False
    if not uri or '"' in uri or "\\" in uri:
        return False
    try:
        result = subprocess.run(
            ["osascript", "-e", f'tell application "Spotify" to play track "{uri}"'],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            print(f"{YELLOW}[SPOTIFY] AppleScript play failed: {err}{RESET}", flush=True)
            return False
        print(f"{GREEN}[SPOTIFY] Started on this Mac via local player{RESET}", flush=True)
        return True
    except Exception as exc:
        print(f"{YELLOW}[SPOTIFY] AppleScript play failed: {exc}{RESET}", flush=True)
        return False


def _wait_for_local_device(sp, timeout: float | None = None) -> str | None:
    if timeout is None:
        timeout = float(os.environ.get("SPOTIFY_DEVICE_WAIT_SECS", "8") or 8)
    deadline = time.monotonic() + max(0.5, timeout)
    while True:
        devices = _device_list(sp)
        device = _pick_local_device_id(devices)
        if device:
            print(
                f"{GREEN}[SPOTIFY] This Mac is online as "
                f"{_device_name(devices, device)}{RESET}",
                flush=True,
            )
            return device
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.35)


def _ensure_playback_device(sp) -> str | None:
    """This Mac's Spotify app only — other computers stay out of the way."""
    devices = _device_list(sp)
    device = _pick_local_device_id(devices)
    if device:
        print(
            f"{GREEN}[SPOTIFY] Using this Mac ({_device_name(devices, device)}){RESET}",
            flush=True,
        )
        return device
    if not _launch_local_spotify():
        return None
    return _wait_for_local_device(sp)


def _pause_other_devices(sp, keep_id: str | None = None) -> None:
    """Stop every Connect target except the one we are about to use."""
    for d in _device_list(sp):
        did = d.get("id")
        if not did or did == keep_id:
            continue
        try:
            sp.pause_playback(device_id=did)
        except Exception:
            pass


def _start_playback(sp, device: str, **kwargs) -> None:
    devices = _device_list(sp)
    target_active = any(
        d.get("id") == device and d.get("is_active") for d in devices
    )
    _pause_other_devices(sp, keep_id=device)
    if not target_active:
        try:
            sp.transfer_playback(device_id=device, force_play=False)
        except Exception:
            pass
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
        sp = _client()
        _next_display_sync()

        search_type = type if type in ("track", "artist", "playlist") else "track"
        item = _first_search_hit(sp, query, search_type)

        # Vague "play something" playlist searches often return null/unavailable
        # entries. Fall back to a track so we still play music.
        if item is None and search_type == "playlist":
            search_type = "track"
            item = _first_search_hit(sp, query, "track")

        if item is None:
            return f"No {search_type} found for '{query}', sir."

        play_kwargs: dict = {}
        local_uri = ""
        tracks: list[dict] = []
        if search_type == "track":
            uri = item["uri"]
            local_uri = uri
            name = _track_label(item) or item.get("name", query)
            # Album context so skip has a next track, without dumping a
            # 20-URI list that some clients start as overlapping playback.
            album_uri = (item.get("album") or {}).get("uri")
            if album_uri:
                play_kwargs = {"context_uri": album_uri, "offset": {"uri": uri}}
            else:
                play_kwargs = {"uris": [uri]}
        elif search_type == "artist":
            name = item["name"]
            track_results = sp.search(q=f"artist:{name}", type="track", limit=10)
            tracks = [
                t for t in (track_results or {}).get("tracks", {}).get("items", [])
                if t
            ]
            uris = [t["uri"] for t in tracks if t.get("uri")]
            if not uris:
                return f"No tracks found for {name}, sir."
            local_uri = uris[0]
            play_kwargs = {"uris": uris}
        else:  # playlist
            uri = item["uri"]
            local_uri = uri
            name = item["name"]
            play_kwargs = {"context_uri": uri, "offset": {"position": 0}}

        # Prefer one Connect start when this Mac is already registered. Starting
        # first through AppleScript and then again through Connect caused an
        # audible/display buffering reset on every command.
        devices = _device_list(sp)
        device = _pick_local_device_id(devices)
        started = False
        if not device:
            started = bool(local_uri and _play_uri_local(local_uri))
        if started and not device:
            _pause_other_devices(sp, keep_id=_pick_local_device_id(_device_list(sp)))
            device = _wait_for_local_device(sp, timeout=3)
        elif not device:
            device = _ensure_playback_device(sp)
        if device:
            try:
                _start_playback(sp, device, **play_kwargs)
                started = True
            except Exception as exc:
                if started:
                    print(
                        f"{YELLOW}[SPOTIFY] Connect sync failed; already playing on this Mac: {exc}{RESET}",
                        flush=True,
                    )
                else:
                    print(f"{YELLOW}[SPOTIFY] Connect play failed: {exc}{RESET}", flush=True)

        if not started:
            return (
                "No Spotify player on this Mac, sir. "
                "Install Spotify, or open it once so I can take over."
            )

        print(f"{GREEN}[SPOTIFY] Playing: {name}{RESET}", flush=True)
        try:
            from core.audio_duck import notify_playback_started
            notify_playback_started()
        except Exception:
            pass
        expected_item = item if search_type == "track" else (tracks[0] if tracks else None)
        expected_context = item.get("uri") if search_type == "playlist" else None
        if expected_item:
            # Immediate optimistic screen, followed by authoritative progress.
            _mirror_display(expected_item, True, 0)
        _sync_display_after_transition(
            expected_item=expected_item,
            expected_context_uri=expected_context,
            require_playing=True,
        )
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
        _next_display_sync()
        current = playback_state()
        if current and not current.get("is_playing"):
            _mirror_display(
                current.get("item"),
                playing=False,
                progress_ms=current.get("progress_ms") or 0,
            )
            return "Playback paused, sir."
        device = _active_device_id(current)
        if not device:
            return "No active Spotify device, sir."
        _client().pause_playback(device_id=device)
        _mirror_display(
            (current or {}).get("item"),
            playing=False,
            progress_ms=(current or {}).get("progress_ms") or 0,
        )
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
        _next_display_sync()
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
                _sync_display_after_transition(
                    expected_item=follow[0],
                    previous_item=(current or {}).get("item"),
                )
                return (
                    f"No next track in queue; started related songs"
                    f"{' after ' + label if label else ''}, sir."
                )

        sp.next_track(device_id=device)
        _sync_display_after_transition(
            previous_item=(current or {}).get("item"),
        )
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
        _next_display_sync()
        current = playback_state()
        device = _active_device_id(current)
        if not device:
            return "No active Spotify device, sir."
        if current and current.get("is_playing"):
            _mirror_display((current or {}).get("item"), True, (current or {}).get("progress_ms") or 0)
            return "Resuming playback, sir."
        try:
            _client().start_playback(device_id=device)
            _sync_display_after_transition(
                expected_item=(current or {}).get("item"),
                require_playing=True,
            )
            return "Resuming playback, sir."
        except Exception as exc:
            if "Restriction violated" in str(exc) and is_playing():
                _sync_display_after_transition(
                    expected_item=(current or {}).get("item"),
                    require_playing=True,
                )
                return "Resuming playback, sir."
            item = (current or {}).get("item") or {}
            uri = item.get("uri")
            if uri:
                _client().start_playback(device_id=device, uris=[uri])
                _mirror_display(item, True, 0)
                _sync_display_after_transition(
                    expected_item=item,
                    require_playing=True,
                )
                return "Resuming playback, sir."
            raise exc
    except Exception as exc:
        print(f"{RED}[SPOTIFY] resume error: {exc}{RESET}", flush=True)
        return f"Could not resume Spotify, sir: {exc}"
