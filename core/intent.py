"""
ULTRON — Intent router

Alexa-style pattern matching so obvious commands fire tools immediately.
Claude still handles personality, ambiguous requests, and multi-step work.

Modes:
  execute — args are complete; run the tool now, Claude only speaks
  force   — we know the tool; Claude fills args (tool_choice forced)
  filter  — we know the domain; send only that family's tools
  chat    — clearly conversational; stream the model without tool schemas
  None    — mixed / unclear; full Claude with every tool
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Intent:
    mode: str                          # execute | force | filter
    tool: str | None = None
    inputs: dict | None = None
    family: str | None = None
    note: str = ""


FAMILY_TOOLS: dict[str, list[str]] = {
    "spotify":  [
        "play_spotify", "pause_spotify", "skip_spotify",
        "resume_spotify", "get_currently_playing",
    ],
    "gmail":    ["draft_gmail", "send_gmail", "search_gmail", "read_email"],
    "notion":   ["create_notion_page", "search_notion", "append_to_notion"],
    "calendar": ["list_calendar_events", "create_calendar_event"],
    "search":   ["web_search"],
    "content":  ["generate_content"],
    "messages": ["send_imessage", "search_imessage", "airdrop_file"],
    "podcast":  ["generate_daily_podcast"],
    "homelab":  [
        "pi_get_status", "pi_list_movies", "pi_search_movie", "pi_add_movie",
        "pi_list_releases", "pi_grab_release", "pi_list_series", "pi_search_series",
        "pi_add_series", "pi_list_episodes", "pi_plex_library", "pi_plex_search",
        "pi_plex_now_playing", "pi_list_requests", "pi_list_media_files",
        "pi_docker_restart", "pi_docker_logs", "pi_run_command",
        "pi_show_card", "pi_clear_card", "pi_now_playing",
        "play_movie", "open_homelab", "control_pi_display",
    ],
    "screen":   [],   # screenshot is pre-attached; Claude just looks
}

# Domain keywords — used for mixed-intent detection and filter fallback.
_FAMILY_KEYWORDS: list[tuple[str, re.Pattern]] = [
    ("spotify",  re.compile(r"\b(spotify|playlist|album|\bsongs?\b|\btracks?\b|\bmusic\b|now playing)\b")),
    ("gmail",    re.compile(r"\b(gmail|inbox|emails?|e-?mails?)\b")),
    ("calendar", re.compile(r"\b(calendar|schedule|meetings?|appointments?)\b")),
    ("notion",   re.compile(r"\b(notion|note to self)\b")),
    ("messages", re.compile(r"\b(imessage|i message|airdrop|texts?|sms)\b")),
    ("content",  re.compile(r"\b(linkedin|tweet|twitter|social post)\b")),
    ("podcast",  re.compile(r"\b(podcast|daily brief|morning brief)\b")),
    ("homelab",  re.compile(
        r"\b(homelab|radarr|sonarr|prowlarr|prowlar|jellyfin|\bplex\b|seerr|"
        r"overseerr|qbittorrent|qbit|lidarr|bazarr|n8n|home assistant|"
        r"raspberry pi|the pi|pi lab|pi display|pi monitor|"
        r"grab (?:a |the )?movie|download (?:a |the )?movie|"
        r"\bmovies?\b|\bseries\b|\btv shows?\b)\b"
    )),
    ("search",   re.compile(r"\b(google|look up|search the web)\b")),
    ("screen",   re.compile(r"\b(?:on (?:my |the )?(?:screen|display|monitor)|screenshot|look at my screen)\b")),
]

_WAKE_PREFIX = re.compile(
    r"^(?:(?:hey|ok|okay|yo)[,.]?\s+)?"
    r"(?:(?:ultron|jarvis)\s*)+"
    r"[,.]?\s*",
    re.I,
)

_PREFIX = re.compile(
    r"^(?:(?:hey|ok|okay|yo)[,.]?\s+)?"
    r"(?:(?:ultron|jarvis)[,.]?\s+)?"
    r"(?:please\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+)?"
    r"(?:please\s+)?(?:just\s+)?",
    re.I,
)

_COMPOUND = re.compile(
    r"\b(?:and then|after that|and also|, then)\b"
    r"|\band\s+(?:email|text|message|schedule|search|google|look up|"
    r"create|draft|send|skip|pause|play)\b",
    re.I,
)

# "don't play that" is not a play command. "stop the music" is pause — handled first.
_NEGATE = re.compile(r"\b(?:don't|do not|never|not going to)\b", re.I)

_NOT_MUSIC = re.compile(
    r"\b(video|movie|clip|trailer|game|voicemail|recording|podcast|"
    r"season|episode|s\d{1,2}e\d{1,3}|jellyfin|plex|"
    r"raspberry\s+pi)\b",
    re.I,
)

_WATCH_EPISODE_OF = re.compile(
    r"season\s+(\w+)\s+episode\s+(\w+)\s+of\s+(.+)",
    re.I,
)
_WATCH_EPISODE_SHOW = re.compile(
    r"\b(?:watch|play|put on|stream)\s+(?:(?:this|that|the|a)\s+)*"
    r"(?!season\b)(.+?)\s+season\s+(\w+)\s+episode\s+(\w+)",
    re.I,
)
_WATCH_EPISODE_SE = re.compile(
    r"\b(?:watch|play|put on|stream)\s+(.+?)\s+s(\d{1,2})\s*e(\d{1,3})\b",
    re.I,
)

_OPEN_HOMELAB = re.compile(
    r"^(?:open(?: up)?|launch|bring up|pull up|go to)\s+"
    r"(?:the |my )?(?:web(?:site| ui) (?:for |of )?)?(.+)$",
    re.I,
)

_WATCH_MOVIE = re.compile(
    r"^(?:watch)\s+(?:the )?(?:movie\s+)?(.+)$"
    r"|^(?:play|put on)\s+(?:the )?movie\s+(.+)$"
    r"|^(?:play|put on)\s+(.+?)\s+on\s+(?:jellyfin|plex|"
    r"(?:the |my )?(?:raspberry\s+pi|raspi|pie|pi(?:'s)?(?:\s+display)?|"
    r"tv|television|monitor|display|hdmi))$",
    re.I,
)

_ON_DISPLAY = re.compile(
    r"\bon (?:the |my )?(?:raspberry\s+pi|raspi|pie|pi(?:'s)?(?:\s+display)?|"
    r"tv|television|monitor|display|hdmi)\b",
    re.I,
)

_ORDINAL = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}

_GARBLED_WAKE = re.compile(
    r"^(?:(?:hey|ok|okay|yo)[,.]?\s+)?"
    r"\w{2,16}[,.]?\s+"
    r"(?:please\s+)?(?:can|could|would|will)\s+you\s+",
    re.I,
)

_VAGUE_PLAY = re.compile(
    r"^(?:that|this|it|(?:the |that |this )?(?:song|track|one))$",
    re.I,
)

_BARE_MUSIC = re.compile(
    r"^(?:some )?(?:music|tunes|songs|something|anything)$",
    re.I,
)

_CONTEXT_ONLY = re.compile(
    r"^(?:(?:what(?:'s| is)|how(?:'s| is)) (?:the )?)?"
    r"(?:weather|temperature|forecast|battery|date|time)"
    r"(?:\s+today)?$"
    r"|^(?:what time is it)$",
    re.I,
)

_TIME_QUERY = re.compile(r"^(?:what(?:'s| is) the time|what time is it|time)$", re.I)
_DATE_QUERY = re.compile(
    r"^(?:what(?:'s| is) (?:the )?date|what day is it|date)(?: today)?$", re.I
)
_LOCAL_WEATHER_QUERY = re.compile(
    r"^(?:(?:what(?:'s| is)|how(?:'s| is)) (?:the )?)?"
    r"(?:weather|temperature|forecast)(?: today)?$",
    re.I,
)
_BATTERY_QUERY = re.compile(
    r"^(?:(?:what(?:'s| is)|how(?:'s| is)) (?:the )?)?"
    r"(?:battery|battery level|battery percentage)$",
    re.I,
)
_NEXT_EVENT_QUERY = re.compile(
    r"^(?:what(?:'s| is) my next (?:meeting|event|appointment)|"
    r"when is my next (?:meeting|event|appointment)|next (?:meeting|event|appointment))$",
    re.I,
)
_UNREAD_COUNT_QUERY = re.compile(
    r"^(?:how many unread (?:emails?|messages?) (?:do i have|are there)|"
    r"what(?:'s| is) my unread (?:email|mail) count|unread (?:email|mail) count)$",
    re.I,
)

_CHAT_ONLY = re.compile(
    r"^(?:hello|hi|good (?:morning|afternoon|evening|night)|"
    r"how are you|who are you|what are you|thank you|thanks|"
    r"tell me a joke|say something witty)$",
    re.I,
)

_WEATHER_ELSEWHERE = re.compile(
    r"\b(?:weather|temperature|forecast)\s+in\s+(.+)$",
    re.I,
)

_PAUSE = re.compile(
    r"^(?:pause(?: it| that)?(?: (?:the )?(?:music|song|track|spotify|playback))?|"
    r"stop (?:the )?(?:music|song|track|playback)|stop playing)$",
    re.I,
)

_PAUSE_MEDIA = re.compile(
    r"\b(?:pause|unpause|resume)\b"
    r"|\bstop (?:the |this |that )?(?:movie|show|episode|video|film|playback)\b",
    re.I,
)

_STOP_MEDIA = re.compile(
    r"\b(?:stop|quit|end|kill|turn off)\b.+\b(?:movie|show|episode|video|film)\b"
    r"|\b(?:stop|quit|end) (?:playing|playback)\b",
    re.I,
)

_RESUME_MEDIA = re.compile(
    r"\b(?:resume|unpause|continue|keep playing|play again)\b",
    re.I,
)

_PI_PLAYBACK = re.compile(
    r"\b(?:movie|movies|show|episode|video|film|"
    r"on (?:the |my )?(?:raspberry\s+pi|raspi|pie|pi(?:'s)?(?:\s+display)?|"
    r"tv|television|monitor|hdmi))\b",
    re.I,
)

_MUSIC_ONLY = re.compile(r"\b(?:music|song|track|spotify|playlist)\b", re.I)

_VOL_SET = re.compile(
    r"\b(?:(?:set|turn|put|make) )?(?:the )?(?:volume|sound)(?: (?:to|at))? (\d{1,3})\s*(?:percent|%)?",
    re.I,
)
_VOL_UP = re.compile(
    r"\b(?:volume up|turn (?:the )?(?:volume|sound|it) up|louder|raise (?:the )?volume)\b",
    re.I,
)
_VOL_DOWN = re.compile(
    r"\b(?:volume down|turn (?:the )?(?:volume|sound|it) down|quieter|softer|lower (?:the )?volume)\b",
    re.I,
)

_RESUME = re.compile(
    r"^(?:resume|unpause|keep playing|continue playing|"
    r"start playing again|play it again|play)$",
    re.I,
)

_SKIP = re.compile(
    r"^(?:skip(?: (?:this |the )?(?:song|track|one))?|"
    r"next (?:song|track)|play the next (?:song|track))$",
    re.I,
)

_NOW_PLAYING = re.compile(
    r"^(?:what(?:'s| is) (?:this |the )?(?:song|track|playing)|"
    r"what (?:song|track) is (?:this|that)|"
    r"what am i listening to|who(?:'s| is) (?:this|playing|this song)|"
    r"currently playing|what(?:'s| is) playing)$",
    re.I,
)

_PLAY = re.compile(
    r"^(?:play|put on|queue)\s+"
    r"(?:me\s+)?(?:some\s+|a\s+|the\s+)?"
    r"(?:song\s+|track\s+|artist\s+|playlist\s+|album\s+)?"
    r"(?:called\s+|named\s+)?"
    r"(.+)$",
    re.I,
)

_PODCAST = re.compile(
    r"\b(?:(?:make|create|generate|start|play) (?:my |the |a )?(?:daily )?(?:podcast|brief)|"
    r"(?:daily|morning) (?:podcast|brief)|brief me|"
    r"my (?:daily )?(?:podcast|brief))\b",
    re.I,
)

_PI_STATUS = re.compile(
    r"^(?:how(?:'s| is) (?:the )?(?:pi|raspberry(?: pi)?|homelab)(?: doing)?|"
    r"(?:check|status(?: of)?) (?:the )?(?:pi|homelab)|"
    r"homelab status|"
    r"is (?:the )?(?:pi|homelab) (?:up|ok|okay|online))$",
    re.I,
)

_PI_MOVIES = re.compile(
    r"\b(?:what movies|which movies|my movies|movie library|"
    r"movies (?:do i have|have i got|on (?:the |my )?(?:pi|plex|jellyfin|radarr|homelab|raspberry))|"
    r"what(?:'s| is) (?:in )?(?:my )?(?:movie )?library|"
    r"list (?:my )?movies|show (?:me )?(?:my )?movies)\b",
    re.I,
)

_PI_SHOWS = re.compile(
    r"\b(?:what (?:shows|series)|which (?:shows|series)|my (?:shows|series)|tv library|"
    r"(?:shows|series) (?:do i have|on (?:the |my )?(?:pi|plex|jellyfin|sonarr|homelab))|"
    r"list (?:my )?(?:shows|series)|show (?:me )?(?:my )?(?:shows|series))\b",
    re.I,
)

_CAL_LIST = re.compile(
    r"^(?:what(?:'s| is) (?:on )?(?:my )?(?:calendar|schedule)|"
    r"(?:show|check|read) (?:my )?(?:calendar|schedule)|"
    r"any (?:meetings?|events?|appointments?)|"
    r"what(?:'s| is) coming up)(?:\s+(.+))?$",
    re.I,
)

_CAL_CREATE = re.compile(
    r"\b(?:schedule|book|add|create|put)\b.+\b(?:meeting|event|appointment|on my calendar|reminder)\b"
    r"|\b(?:meeting|event|appointment)\b.+\b(?:schedule|book|add|create)\b",
    re.I,
)

_UNREAD_MAIL = re.compile(
    r"^(?:(?:any|check|show|read|get) )?(?:my )?(?:unread )?(?:emails?|e-?mails?|inbox|gmail|mail)"
    r"(?:s)?(?:\s+(?:today|for me))?$",
    re.I,
)

_SEND_MAIL = re.compile(
    r"\b(?:email|e-mail|send (?:an )?email to|draft (?:an )?email)\b",
    re.I,
)

_STRUCTURED_EMAIL = re.compile(
    r"^(draft|send) (?:an )?(?:email|e-mail) to\s+(\S+@\S+)\s+"
    r"subject\s+(.+?)\s+body\s+(.+)$",
    re.I,
)

_WEB_SEARCH = re.compile(
    r"^(?:search the web(?: for)?|google|look up|look this up)\s+(.+)$",
    re.I,
)

_IMESSAGE = re.compile(
    r"^(?:text|message|imessage|i message|sms)\s+"
    r"(?:to\s+)?(.+?)\s+"
    r"(?:that|saying|and say|and tell (?:them|him|her)|:)\s+"
    r"(.+)$",
    re.I,
)

_IMESSAGE_FORCE = re.compile(
    r"^(?:text|message|imessage|i message|sms)\s+(?:to\s+)?(?!from\b).+$",
    re.I,
)

# Read/search local texts — must win over send.
_IMESSAGE_QUERY = re.compile(
    r"\b(?:what(?:'s| is| did)|(?:any|check|show|read|search|find|get))\b"
    r".+\b(?:texts?|messages?|imessages?|sms)\b"
    r"|\b(?:did|has|have)\s+\S.+\b(?:text|message|imessage)(?:d|s)?(?:\s+me)?\b"
    r"|\b(?:texts?|messages?|imessages?)\s+(?:from|about|containing|mentioning)\b"
    r"|\b(?:unread|recent|latest|last)\s+(?:few\s+)?(?:texts?|messages?)\b"
    r"|\b(?:search|find|look through)\s+(?:my\s+)?(?:texts?|messages?)\b",
    re.I,
)

_IMESSAGE_RECENT = re.compile(
    r"^(?:(?:any|check|show|read|get|what(?:'s| is))\s+)?"
    r"(?:my\s+)?"
    r"(?:unread\s+|recent\s+|latest\s+|last\s+(?:few\s+)?)?"
    r"(?:texts?|messages?|imessages?|sms)"
    r"(?:\s+(?:today|for me))?$",
    re.I,
)

_IMESSAGE_FROM = re.compile(
    r"\bfrom\s+(?!today\b|yesterday\b|this week\b|last week\b|last \d+ days\b)"
    r"(.+?)(?:\s+(?:today|yesterday|this week|last week|"
    r"last \d+ days|about|containing|mentioning|for)\b|$)",
    re.I,
)

_IMESSAGE_DID = re.compile(
    r"\bwhat did\s+(.+?)\s+(?:just\s+)?(?:text|message|imessage)"
    r"|\b(?:did|has|have)\s+(.+?)\s+(?:just\s+)?(?:text(?:ed)?|message(?:d)?|imessage)"
    r"(?:d)?(?:\s+me)\b",
    re.I,
)

_IMESSAGE_ABOUT = re.compile(
    r"\b(?:search|find|look through)\s+(?:my\s+)?(?:texts?|messages?).+?"
    r"\b(?:for|about)\s+(.+)$"
    r"|\b(?:about|containing|mentioning)\s+(.+)$",
    re.I,
)

_CONTENT = re.compile(
    r"\b(?:linkedin|tweet|twitter|x post|social post|write a post|draft (?:a |some )?content|capture (?:this )?idea)\b",
    re.I,
)

_NOTION = re.compile(
    r"\b(?:notion|note to self|save (?:this|that) (?:note|page)|search notion)\b",
    re.I,
)

_PERIODS = re.compile(
    r"\b(today|tomorrow|yesterday|this week|next week|last week|"
    r"last \d+ days|last 7 days|last 30 days)\b",
    re.I,
)


def strip_wake(transcript: str) -> str:
    """Drop 'Hey Jarvis/Ultron' so Claude doesn't spend a sentence on the name."""
    t = transcript.strip()
    if not t:
        return t
    stripped = _WAKE_PREFIX.sub("", t, count=1).strip()
    return stripped if stripped else "Hello."


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    s = value.strip().lower()
    if s.isdigit():
        n = int(s)
        return n if n > 0 else None
    return _ORDINAL.get(s)


def _clean_title(title: str) -> str:
    t = title.strip(" .")
    t = re.sub(r"\s+for me$", "", t, flags=re.I)
    t = re.sub(r"^(?:the )?(?:show|series|tv show)\s+", "", t, flags=re.I)
    t = re.sub(
        r"\s+on\s+(?:jellyfin|plex|"
        r"(?:the |my )?(?:raspberry\s+pi|raspi|pie|pi(?:'s)?(?:\s+display)?|"
        r"tv|television|monitor|display|hdmi))$",
        "", t, flags=re.I,
    )
    return t.strip(" .")


def _episode_intent(t: str) -> Intent | None:
    of = _WATCH_EPISODE_OF.search(t)
    show = _WATCH_EPISODE_SHOW.search(t)
    se = _WATCH_EPISODE_SE.search(t)
    if of:
        season, episode, title = of.group(1), of.group(2), of.group(3)
    elif show:
        title, season, episode = show.group(1), show.group(2), show.group(3)
    elif se:
        title, season, episode = se.group(1), se.group(2), se.group(3)
    else:
        return None
    title = _clean_title(title or "")
    title = re.sub(r"^(?:this|that|the|a|some)\s+", "", title, flags=re.I).strip()
    if title.lower() in {"this", "that", "the", "a", "some", "it"}:
        return None
    season_n = _to_int(season)
    episode_n = _to_int(episode)
    if not title or not season_n or not episode_n:
        return Intent("force", "play_movie", None, "homelab", "watch episode")
    inputs = {"title": title, "season": season_n, "episode": episode_n}
    if _ON_DISPLAY.search(t):
        inputs["on_display"] = True
    return Intent(
        "execute",
        "play_movie",
        inputs,
        "homelab",
        f"watch {title} S{season_n:02d}E{episode_n:02d}",
    )


def _normalize(transcript: str) -> str:
    t = transcript.strip().lower()
    t = re.sub(r"[.?!,:;]+$", "", t)
    t = re.sub(r"\s+please$", "", t)
    t = re.sub(r"\s+for me$", "", t)
    t = re.sub(r"\bmy pie\b", "my pi", t)
    t = re.sub(r"\bthe pie\b", "the pi", t)
    t = re.sub(r"\bon (the |my )?pie\b", r"on \1pi", t)
    t = re.sub(r"\bpie display\b", "pi display", t)
    # Whisper glues "Minds on" → "Mindson"
    t = re.sub(r"(\w+)on\s+my\s+pi\b", r"\1 on my pi", t)
    t = re.sub(r"(\w+)on\s+the\s+pi\b", r"\1 on the pi", t)
    for _ in range(3):
        nxt = _PREFIX.sub("", t).strip()
        if nxt == t:
            break
        t = nxt
    if _GARBLED_WAKE.match(t):
        t = _GARBLED_WAKE.sub("", t).strip()
    # Whisper often prepends junk ("i drive this pause the movie…").
    verb = re.search(
        r"\b(?:pause|unpause|resume|stop playing|volume|"
        r"turn (?:it |the volume )?(?:up|down)|louder|quieter)\b",
        t,
    )
    if verb and verb.start() > 0 and not _NEGATE.search(t[:verb.start()]):
        t = t[verb.start():]
    t = re.sub(r"\s+please$", "", t)
    t = re.sub(r"\s+for me$", "", t)
    return t.strip()


def _normalize_preserving_case(transcript: str) -> str:
    """Apply command-prefix cleanup without lowercasing extracted content."""
    text = transcript.strip()
    text = re.sub(r"[.?!,:;]+$", "", text)
    text = re.sub(r"\s+please$", "", text, flags=re.I)
    text = re.sub(r"\s+for me$", "", text, flags=re.I)
    for _ in range(3):
        cleaned = _PREFIX.sub("", text).strip()
        if cleaned == text:
            break
        text = cleaned
    return text.strip()


def _wants_pi_playback(t: str) -> bool:
    return bool(_ON_DISPLAY.search(t) or _PI_PLAYBACK.search(t))


def _media_control_intent(t: str) -> Intent | None:
    """Pause / resume / volume for HDMI VLC, including garbled 'pause the movie on my pie'."""
    if not _wants_pi_playback(t):
        return None
    if _MUSIC_ONLY.search(t) and not re.search(
        r"\b(?:movie|show|episode|video|film|pi|pie|tv|hdmi|display)\b", t
    ):
        return None
    vol = _VOL_SET.search(t)
    if vol:
        n = int(next(g for g in vol.groups() if g))
        n = max(0, min(100, n))
        return Intent(
            "execute", "control_pi_display",
            {"action": "volume", "percent": n},
            "homelab", f"pi volume {n}",
        )
    if _VOL_UP.search(t):
        return Intent(
            "execute", "control_pi_display",
            {"action": "volume", "delta": 10},
            "homelab", "pi volume up",
        )
    if _VOL_DOWN.search(t):
        return Intent(
            "execute", "control_pi_display",
            {"action": "volume", "delta": -10},
            "homelab", "pi volume down",
        )
    if _RESUME_MEDIA.search(t) and not re.search(r"\bpause\b", t):
        return Intent(
            "execute", "control_pi_display",
            {"action": "resume"},
            "homelab", "pi resume",
        )
    if re.search(r"\b(?:quit|kill|turn off)\b", t) and _STOP_MEDIA.search(t):
        return Intent(
            "execute", "control_pi_display",
            {"action": "stop"},
            "homelab", "pi stop",
        )
    if re.search(r"\bpause\b", t) or _STOP_MEDIA.search(t):
        return Intent(
            "execute", "control_pi_display",
            {"action": "pause"},
            "homelab", "pi pause",
        )
    return None


def _families(text: str) -> list[str]:
    return [name for name, pat in _FAMILY_KEYWORDS if pat.search(text)]


def _spotify_type(normalized: str) -> str:
    if re.search(r"\bplaylists?\b", normalized):
        return "playlist"
    if re.search(r"\bartists?\b", normalized):
        return "artist"
    return "track"


def _cal_period(tail: str | None) -> str:
    if not tail:
        return "today"
    m = _PERIODS.search(tail)
    return m.group(1).lower() if m else "today"


def match_intent(transcript: str) -> Intent | None:
    """Return a routing intent, or None to use the full Claude tool loop."""
    if not transcript or not transcript.strip():
        return None

    t = _normalize(transcript)
    if not t:
        return None

    if _NEGATE.search(t):
        return None

    # Chained verbs across actions → Claude orchestrates.
    if _COMPOUND.search(t):
        return None

    if _CHAT_ONLY.match(t):
        return Intent("chat", family="chat", note="conversation")

    elsewhere = _WEATHER_ELSEWHERE.search(t)
    if elsewhere:
        place = elsewhere.group(1).strip(" .")
        return Intent(
            "execute", "web_search",
            {"query": f"weather in {place}", "max_results": 3},
            "search", f"weather {place}",
        )
    for pattern, kind in (
        (_TIME_QUERY, "time"),
        (_DATE_QUERY, "date"),
        (_LOCAL_WEATHER_QUERY, "weather"),
        (_BATTERY_QUERY, "battery"),
        (_NEXT_EVENT_QUERY, "next_event"),
        (_UNREAD_COUNT_QUERY, "unread_count"),
    ):
        if pattern.match(t):
            return Intent(
                "execute", "get_context_value", {"kind": kind},
                "context", f"context {kind}",
            )
    if _CONTEXT_ONLY.match(t):
        return None

    # ── Pi HDMI playback (before Spotify — "pause the movie on my pie") ───────
    media = _media_control_intent(t)
    if media:
        return media

    # ── Spotify controls (no args) ────────────────────────────────────────────
    if _PAUSE.match(t):
        return Intent(
            "execute", "control_pi_display",
            {"action": "pause", "fallback_spotify": True},
            "homelab", "pause",
        )
    if _SKIP.match(t):
        return Intent("execute", "skip_spotify", {}, "spotify", "skip")
    if _RESUME.match(t):
        return Intent(
            "execute", "control_pi_display",
            {"action": "resume", "fallback_spotify": True},
            "homelab", "resume",
        )
    if _NOW_PLAYING.match(t):
        return Intent("execute", "get_currently_playing", {}, "spotify", "now playing")

    # ── Podcast (before "play …" so "play my podcast" isn't Spotify) ──────────
    if _PODCAST.search(t):
        return Intent("execute", "generate_daily_podcast", {}, "podcast", "podcast")

    if _PI_STATUS.match(t):
        return Intent("execute", "pi_get_status", {}, "homelab", "pi status")
    if _PI_MOVIES.search(t):
        return Intent("execute", "pi_list_movies", {}, "homelab", "list movies")
    if _PI_SHOWS.search(t):
        return Intent("execute", "pi_list_series", {}, "homelab", "list series")

    open_lab = _OPEN_HOMELAB.match(t)
    if open_lab:
        from integrations.homelab_web import resolve_apps
        target = (open_lab.group(1) or "").strip(" .")
        if resolve_apps(target):
            return Intent(
                "execute", "open_homelab", {"app": target},
                "homelab", f"open {target}",
            )

    episode = _episode_intent(t)
    if episode:
        return episode

    watch = _WATCH_MOVIE.match(t)
    if watch:
        title = _clean_title(next((g for g in watch.groups() if g), ""))
        if title:
            inputs = {"title": title}
            if _ON_DISPLAY.search(t):
                inputs["on_display"] = True
            return Intent(
                "execute", "play_movie", inputs, "homelab", f"watch {title}",
            )
        return Intent("force", "play_movie", None, "homelab", "watch movie")

    # ── Play / put on ─────────────────────────────────────────────────────────
    play = _PLAY.match(t)
    if play and not _NOT_MUSIC.search(t):
        query = re.sub(r"\s+on\s+spotify$", "", play.group(1).strip()).strip(" .")
        query = re.sub(r"^(?:the artist|artist)\s+", "", query).strip()
        if not query or _VAGUE_PLAY.match(query):
            return Intent("force", "play_spotify", None, "spotify", "play (vague)")
        if _BARE_MUSIC.match(query):
            return Intent(
                "execute", "play_spotify",
                {"query": "discover weekly", "type": "playlist"},
                "spotify", "play default",
            )
        return Intent(
            "execute", "play_spotify",
            {"query": query, "type": _spotify_type(t)},
            "spotify", f"play {query}",
        )

    # ── Screen vision (screenshot is pre-attached in brain) ───────────────────
    try:
        from integrations.screen import looks_like_screen_question
        if looks_like_screen_question(transcript):
            return Intent("filter", family="screen", note="screen vision")
    except Exception:
        pass

    # ── Calendar ──────────────────────────────────────────────────────────────
    if _CAL_CREATE.search(t):
        return Intent("force", "create_calendar_event", None, "calendar", "create event")
    cal = _CAL_LIST.match(t)
    if cal:
        period = _cal_period(cal.group(1))
        return Intent(
            "execute", "list_calendar_events", {"period": period},
            "calendar", f"calendar {period}",
        )

    # ── Gmail ─────────────────────────────────────────────────────────────────
    structured_mail = _STRUCTURED_EMAIL.match(_normalize_preserving_case(transcript))
    if structured_mail:
        action, recipient, subject, body = structured_mail.groups()
        tool = "draft_gmail" if action.lower() == "draft" else "send_gmail"
        return Intent(
            "execute", tool,
            {"to": recipient, "subject": subject.strip(), "body": body.strip()},
            "gmail", f"{tool} structured",
        )
    if _UNREAD_MAIL.match(t):
        return Intent(
            "execute", "search_gmail", {"query": "is:unread", "max_results": 5},
            "gmail", "unread mail",
        )
    if _SEND_MAIL.search(t):
        tool = "draft_gmail" if "draft" in t else "send_gmail"
        return Intent("force", tool, None, "gmail", tool)

    # ── Web search ────────────────────────────────────────────────────────────
    web = _WEB_SEARCH.match(t)
    if web:
        q = web.group(1).strip(" .")
        if q:
            return Intent("execute", "web_search", {"query": q, "max_results": 3}, "search", f"search {q}")

    # ── iMessage (query before send so "texts from mom" is not a send) ─────────
    if _IMESSAGE_QUERY.search(t):
        args: dict = {}
        who = _IMESSAGE_FROM.search(t) or _IMESSAGE_DID.search(t)
        if who:
            name = next((g for g in who.groups() if g), "")
            if name:
                args["contact"] = name.strip(" .")
        about = _IMESSAGE_ABOUT.search(t)
        if about:
            args["query"] = (about.group(1) or about.group(2) or "").strip(" .")
        period = _PERIODS.search(t)
        if period:
            args["since"] = period.group(1).lower()
        if args or _IMESSAGE_RECENT.match(t):
            args.setdefault("max_results", 15)
            note = args.get("contact") or args.get("query") or "recent texts"
            return Intent(
                "execute", "search_imessage", args, "messages", f"search texts {note}",
            )
        return Intent("force", "search_imessage", None, "messages", "search texts")

    im = _IMESSAGE.match(t)
    if im:
        return Intent(
            "execute", "send_imessage",
            {"to": im.group(1).strip(), "message": im.group(2).strip()},
            "messages", "imessage",
        )
    if _IMESSAGE_FORCE.match(t):
        return Intent("force", "send_imessage", None, "messages", "imessage (incomplete)")

    # ── Content / Notion ──────────────────────────────────────────────────────
    if _CONTENT.search(t):
        return Intent("force", "generate_content", None, "content", "content")
    if _NOTION.search(t):
        return Intent("filter", family="notion", note="notion")

    # ── Domain keyword fallback ───────────────────────────────────────────────
    families = _families(t)
    if len(families) == 1:
        fam = families[0]
        return Intent("filter", family=fam, note=f"{fam} family")

    return None
