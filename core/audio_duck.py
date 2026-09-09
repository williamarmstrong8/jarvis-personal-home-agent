"""
J.A.R.V.I.S. — Audio Ducking

Pauses Spotify while JARVIS is listening/speaking so the mic doesn't
pick up lyrics. Playback is treated as still-active for skip / now-playing
via is_ducked(); music resumes after the conversation unless the user
asked to pause.

System volume is left untouched (afplay uses it for JARVIS's own voice).
"""

import threading
import time

CYAN   = "\033[96m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"

_DUCK_TIMEOUT_SECS = 2.0

# Spotify's speaker buffer keeps going after the pause API returns.
_PAUSE_SETTLE_SECS = 0.3
# Resume blast otherwise retriggers OpenWakeWord (lyrics ≈ "Hey Jarvis").
_RESTORE_WAKE_SUPPRESS_SECS = 2.5

_should_resume: bool = False
_ducked: bool = False
_restored_at: float = 0.0


def is_ducked() -> bool:
    """True while a wake session has Spotify paused for voice."""
    return _ducked


def wake_suppressed() -> bool:
    """True for a short window after music is restored — ignore wake scores."""
    return time.time() < _restored_at + _RESTORE_WAKE_SUPPRESS_SECS


def duck() -> bool:
    """Pause Spotify if playing. Returns True if a session was silenced.

    Hard-capped so a Spotify 429/hang can never block the mic.
    """
    global _should_resume, _ducked

    _should_resume = False
    _ducked = False
    box: dict = {"ok": False, "err": None}

    def _go():
        try:
            from integrations.spotify import is_playing, pause
            if not is_playing():
                print(f"{CYAN}[DUCK] Spotify not playing — nothing to duck{RESET}", flush=True)
                return
            print(f"{CYAN}[DUCK] Pausing Spotify{RESET}", flush=True)
            pause()
            time.sleep(_PAUSE_SETTLE_SECS)
            box["ok"] = True
        except Exception as exc:
            box["err"] = exc

    t = threading.Thread(target=_go, daemon=True, name="jarvis-duck")
    t.start()
    t.join(timeout=_DUCK_TIMEOUT_SECS)
    if t.is_alive():
        print(f"{YELLOW}[DUCK] Spotify timed out — recording anyway{RESET}", flush=True)
        return False
    if box["err"]:
        print(f"{YELLOW}[DUCK] Spotify pause skipped: {box['err']}{RESET}", flush=True)
        return False
    if box["ok"]:
        _should_resume = True
        _ducked = True
        return True
    return False


def suppress_restore():
    """User asked to leave Spotify paused. Do not resume after the conversation."""
    global _should_resume
    if _should_resume:
        print(f"{CYAN}[DUCK] Leaving Spotify paused (user requested){RESET}", flush=True)
    _should_resume = False


def notify_playback_started():
    """User started new playback during the conversation. Don't also resume."""
    global _should_resume
    _should_resume = False


def restore():
    """Undo ducking after the conversation ends."""
    global _should_resume, _ducked, _restored_at

    should = _should_resume
    _should_resume = False
    _ducked = False
    if not should:
        return

    def _go():
        try:
            from integrations.spotify import resume
            print(f"{CYAN}[DUCK] Resuming Spotify{RESET}", flush=True)
            resume()
        except Exception as exc:
            print(f"{YELLOW}[DUCK] Spotify resume failed: {exc}{RESET}", flush=True)

    _restored_at = time.time()
    t = threading.Thread(target=_go, daemon=True, name="jarvis-unduck")
    t.start()
    t.join(timeout=_DUCK_TIMEOUT_SECS)
    if t.is_alive():
        print(f"{YELLOW}[DUCK] Spotify resume timed out{RESET}", flush=True)
