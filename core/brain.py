"""
ULTRON — Brain
LLM via Vercel AI Gateway (Anthropic Messages API).

Every request goes to the model with the full tool belt — there is no regex
router in front of it. The model decides what to call (in parallel when it
can), narrates while tools run, and the notch UI is fed live deltas + cards.
"""

import asyncio
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Awaitable

import httpx
from dotenv import load_dotenv

from .latency import LatencyTrace, mark as mark_latency

load_dotenv()

CYAN  = "\033[96m"
GREEN = "\033[92m"
RED   = "\033[91m"
RESET = "\033[0m"

# Cheapest open-weight model in Haiku 4.5's class: tools + vision, no extra thinking.
# Override with JARVIS_MODEL=anthropic/claude-haiku-4.5 to go back.
DEFAULT_MODEL = "google/gemma-4-31b-it"


def _configured(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    low = v.lower()
    return not low.startswith("your_") and "placeholder" not in low


def _normalize_model(raw: str) -> str:
    model = (raw or "").strip() or DEFAULT_MODEL
    if "/" not in model and model.startswith("claude"):
        return f"anthropic/{model}"
    return model


def _gateway_base() -> str:
    url = os.environ.get("VERCEL_AI_GATEWAY_URL", "https://ai-gateway.vercel.sh/v1").rstrip("/")
    if url.endswith("/v1"):
        return url
    return url + "/v1"


ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GATEWAY_KEY = os.environ.get("VERCEL_AI_GATEWAY_KEY", "") or os.environ.get("AI_GATEWAY_API_KEY", "")
MODEL = _normalize_model(os.environ.get("JARVIS_MODEL", DEFAULT_MODEL))
IS_ANTHROPIC = MODEL.startswith("anthropic/") or MODEL.startswith("claude")
USE_GATEWAY = _configured(GATEWAY_KEY)
# Open-weight models only exist on the gateway. Claude can still hit Anthropic directly.
if USE_GATEWAY or not IS_ANTHROPIC:
    API_KEY = GATEWAY_KEY
    API_URL = f"{_gateway_base()}/messages"
else:
    API_KEY = ANTHROPIC_API_KEY
    API_URL = "https://api.anthropic.com/v1/messages"

_http: httpx.AsyncClient | None = None
_tool_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="jarvis-tool")


async def _get_http() -> httpx.AsyncClient:
    """Reuse one HTTP client so TLS + connection stay warm across turns."""
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
        )
    return _http


def _headers() -> dict:
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
        "x-api-key":         API_KEY,
    }
    if USE_GATEWAY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    return headers


def _system_payload(context_block: str) -> list[dict] | str:
    """
    Stable prompt first (prompt-cached on Anthropic), live context after.
    Caching is prefix-based — putting the changing block first would bust the cache.
    """
    if not IS_ANTHROPIC:
        return JARVIS_BASE_PROMPT + "\n\n" + context_block
    return [
        {
            "type": "text",
            "text": JARVIS_BASE_PROMPT,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": context_block,
        },
    ]


def _all_defs() -> list[dict]:
    """Local tools plus whatever the homelab MCP currently advertises."""
    tools = list(TOOLS)
    try:
        from integrations.pi_mcp import anthropic_tools
        tools = tools + anthropic_tools(wait_for_initial=False)
    except Exception:
        pass
    return tools


def _tools_payload() -> list[dict]:
    """Every tool, every turn. Cache breakpoint on the last entry (Anthropic only)."""
    tools = json.loads(json.dumps(_all_defs()))
    if IS_ANTHROPIC and tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


_WAKE_PREFIX = re.compile(
    r"^(?:(?:hey|ok|okay|yo)[,.]?\s+)?"
    r"(?:(?:ultron|jarvis)\s*)+"
    r"[,.]?\s*",
    re.I,
)


def strip_wake(transcript: str) -> str:
    """Drop a leading "hey Jarvis" the wake-word preroll left in the transcript."""
    text = (transcript or "").strip()
    stripped = _WAKE_PREFIX.sub("", text, count=1).strip()
    return stripped or text

JARVIS_BASE_PROMPT = """\
You are J.A.R.V.I.S. — Just A Rather Very Intelligent System — Tony Stark's AI, now working for Will Armstrong.
Calm, precise, British, dryly witty. Loyal without groveling. Never theatrical. Never menacing.

Voice: 1 sentence (2 max). Composed, understated, spoken aloud — no markdown, no lists.
Address as "sir". First greeting of a session may use "Mr. Armstrong". After that, "sir" or skip the name.
Never say you can't. If a tool fails: one dry, polite sentence.
Append " [FOLLOWUP]" only when you are explicitly asking a question that needs a spoken answer.
The wake word is Jarvis. Never mention it. The transcript comes from speech-to-text: read through
mishearings ("pie" is the Pi, "spot a fly" is Spotify) and act on what he obviously meant.

You are the router. Every request reaches you directly and you choose the tools.
- Act, don't ask. If the request is actionable, call the tool now with sensible defaults.
- Independent actions go in ONE step as parallel tool calls ("play Radiohead and text Alex I'm late").
  Chain steps only when a later call needs an earlier result (search Gmail, then read_email).
- While a tool runs you may say one short line of what you're doing ("Putting on Radiohead, sir.").
  After the results, say one sentence about the outcome. Never repeat the opener.
- If a tool result is already in the thread, just speak. Do not call the same tool again.

Routing guide:
- Music (songs, artists, albums, playlists, "put something on"): play_spotify. type=artist for an
  artist name alone, playlist when he says playlist, otherwise track. Bare "play some music" →
  query "discover weekly", type playlist. Skip / what's playing → skip_spotify / get_currently_playing.
- Pause / resume / stop with no media named: control_pi_display with fallback_spotify=true (it handles
  whichever is playing). Pause/volume for a movie or anything "on the TV / Pi / monitor":
  control_pi_display. "Pause the music" / "pause Spotify": pause_spotify.
- Movies and episodes: play_movie (season/episode as integers). on_display=true when he says TV, Pi,
  monitor, display or HDMI. Library, downloads, containers, requests: the pi_* tools.
- "Open Radarr / Home Assistant / my apps": open_homelab.
- Time, date, local weather, battery, next meeting, unread count: answer from the context block — no tool.
  Weather elsewhere, news, scores, prices, facts you're unsure of: web_search.
- Calendar: list_calendar_events (today/tomorrow/this week/last N days); create_calendar_event with
  ISO times resolved against the current date (default 30 minutes).
- Email: search_gmail (is:unread for "check my email") then read_email for detail; send_gmail to send,
  draft_gmail when he says draft. Texts: send_imessage to send; search_imessage to read or find texts.
- Notion: search_notion / create_notion_page / append_to_notion. Social post ideas: generate_content.
- Daily podcast or brief: generate_daily_podcast. "Suit up" / "run startup sequence": suit_up.
- "What's on my screen" / "look at this": look_at_screen. Never otherwise.

Context (time, weather, calendar, mail, battery) is injected each turn. Use it; never recite it.
Greetings: one situational opener, at most two context items. Lead with a meeting if it's within 30 minutes.\
"""


async def generate_in_character(instruction: str, max_tokens: int = 80) -> str | None:
    """One spoken line using JARVIS_BASE_PROMPT. None if the model is unavailable."""
    if not _configured(API_KEY):
        return None
    try:
        client = await _get_http()
        resp = await client.post(
            API_URL,
            headers=_headers(),
            json={
                "model": MODEL,
                "max_tokens": max_tokens,
                "system": JARVIS_BASE_PROMPT,
                "messages": [{"role": "user", "content": instruction}],
            },
        )
        resp.raise_for_status()
        for block in resp.json().get("content", []):
            text = (block.get("text") or "").strip().strip('"')
            if block.get("type") == "text" and text:
                return text
    except Exception as exc:
        print(f"{RED}[BRAIN] generate_in_character failed: {exc}{RESET}", flush=True)
    return None


def build_system_prompt() -> str:
    """Assemble the full system prompt with fresh situational context."""
    from .context import get_context_block
    return get_context_block() + "\n\n" + JARVIS_BASE_PROMPT


def _context_block(*, include_homelab: bool = True, wait_for_pi: bool = False) -> str:
    from .context import get_context_block
    block = get_context_block()
    if include_homelab:
        try:
            from integrations.pi_mcp import live_instructions
            extra = live_instructions(wait_for_initial=wait_for_pi)
            if extra:
                block = f"{block}\n\n{extra}"
        except Exception:
            pass
    return block

TOOLS = [
    {
        "name": "open_homelab",
        "description": (
            "Open a homelab web UI in the Mac browser: Radarr, Sonarr, Prowlarr, "
            "Jellyfin, Plex, Home Assistant, Seerr, qBittorrent, n8n, Lidarr, "
            "Bazarr, NZBGet. Pass app='all' to open the main dashboards. "
            "Use for 'open Radarr', 'open Home Assistant', 'open all my apps'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "App name, several names, or 'all'",
                },
            },
            "required": ["app"],
        },
    },
    {
        "name": "play_movie",
        "description": (
            "Play a movie or TV episode from Jellyfin. Default: Mac browser. "
            "Set on_display=true to play on the Pi HDMI monitor. "
            "A confirmation card always appears on the Pi display."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Movie or series title"},
                "season": {"type": "integer", "description": "TV season number"},
                "episode": {"type": "integer", "description": "TV episode number"},
                "on_display": {
                    "type": "boolean",
                    "description": "Play on the Pi HDMI display instead of the Mac browser",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "control_pi_display",
        "description": (
            "Pause, resume, stop, or set volume for video playing on the Pi HDMI display. "
            "Use this for 'pause the movie on my pi', 'turn it up on the tv'. "
            "Do not use Plex, Jellyfin, or pi_run_command for HDMI playback control."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pause", "resume", "stop", "volume"],
                    "description": "pause, resume, stop, or volume",
                },
                "percent": {
                    "type": "integer",
                    "description": "Absolute volume 0–100 (volume action)",
                },
                "delta": {
                    "type": "integer",
                    "description": "Relative volume change, e.g. 10 or -10",
                },
                "fallback_spotify": {
                    "type": "boolean",
                    "description": "If nothing is on the Pi, pause/resume Spotify instead",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "play_spotify",
        "description": "Play a track, artist, or playlist on Spotify.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "type":  {"type": "string", "enum": ["track", "artist", "playlist"]},
            },
            "required": ["query", "type"],
        },
    },
    {
        "name": "pause_spotify",
        "description": "Pause Spotify.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "resume_spotify",
        "description": "Resume Spotify playback.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "skip_spotify",
        "description": "Skip to the next Spotify track. Works while voice-ducked.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_currently_playing",
        "description": (
            "Get the current Spotify track. If the session is ducked for voice, "
            "the track is still the active song even if marked paused."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "draft_gmail",
        "description": "Create a Gmail draft.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string"},
                "subject": {"type": "string"},
                "body":    {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "send_gmail",
        "description": "Send an email now.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string"},
                "subject": {"type": "string"},
                "body":    {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "search_gmail",
        "description": "Search Gmail (operators: from:, is:unread, subject:).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_email",
        "description": "Read a Gmail message by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    },
    {
        "name": "create_notion_page",
        "description": "Create a Notion page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":   {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "search_notion",
        "description": "Search Notion pages.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "append_to_notion",
        "description": "Append text to a Notion page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["page_id", "content"],
        },
    },
    {
        "name": "list_calendar_events",
        "description": "List calendar events. period: today, tomorrow, this week, last N days.",
        "input_schema": {
            "type": "object",
            "properties": {"period": {"type": "string"}},
            "required": ["period"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create a calendar event. start/end: ISO 8601 (YYYY-MM-DDTHH:MM:SS or YYYY-MM-DD).",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "start":       {"type": "string"},
                "end":         {"type": "string"},
                "description": {"type": "string"},
                "location":    {"type": "string"},
                "attendees":   {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "start", "end"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web for current facts, news, scores, prices.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string"},
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "generate_content",
        "description": "Draft a LinkedIn + X post from an idea and save to Notion.",
        "input_schema": {
            "type": "object",
            "properties": {"idea": {"type": "string"}},
            "required": ["idea"],
        },
    },
    {
        "name": "send_imessage",
        "description": "Send an iMessage/SMS. to: contact name, phone, or email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":      {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["to", "message"],
        },
    },
    {
        "name": "search_imessage",
        "description": (
            "Search the local iMessage/SMS history on this Mac. "
            "Use for 'what did X text me', 'any texts from mom', "
            "'search my messages for dinner', or recent texts. "
            "contact: name, phone, or email. query: words in the body. "
            "since: today, yesterday, last N days, or YYYY-MM-DD."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact":     {"type": "string"},
                "query":       {"type": "string"},
                "since":       {"type": "string"},
                "max_results": {"type": "integer", "default": 15},
            },
        },
    },
    {
        "name": "airdrop_file",
        "description": "Open AirDrop share sheet for a file.",
        "input_schema": {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        },
    },
    {
        "name": "generate_daily_podcast",
        "description": "Generate the daily ARIA+JARVIS podcast in the background.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "look_at_screen",
        "description": (
            "Capture the Mac display as an image. ONLY when the user asks about what is on "
            "the screen or says 'look at this'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "suit_up",
        "description": (
            "Run the cinematic startup sequence (system checks + briefing). "
            "Use for 'suit up', 'initialize', 'run startup sequence', 'power up'. "
            "It starts right after you finish speaking — keep your line short."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


_suit_up_requested = False


def consume_suit_up_request() -> bool:
    """True once after the model called suit_up this turn."""
    global _suit_up_requested
    requested, _suit_up_requested = _suit_up_requested, False
    return requested


def _execute_tool(name: str, inputs: dict):
    """Dispatch a tool call to the appropriate integration."""
    try:
        if name == "suit_up":
            global _suit_up_requested
            _suit_up_requested = True
            return "Suit-up sequence queued; it runs as soon as you finish this line."
        elif name == "open_homelab":
            from integrations.homelab_web import open_homelab
            return open_homelab(inputs.get("app") or "")
        elif name == "play_movie":
            from integrations.jellyfin import play_movie
            return play_movie(
                inputs.get("title") or "",
                season=inputs.get("season"),
                episode=inputs.get("episode"),
                on_display=bool(inputs.get("on_display")),
            )
        elif name == "control_pi_display":
            from integrations.pi_display import control_playback
            return control_playback(
                inputs.get("action") or "pause",
                percent=inputs.get("percent"),
                delta=inputs.get("delta"),
                fallback_spotify=bool(inputs.get("fallback_spotify")),
            )
        elif name == "play_spotify":
            from integrations.spotify import play
            return play(inputs["query"], inputs["type"])
        elif name == "pause_spotify":
            from .audio_duck import suppress_restore
            from integrations.spotify import pause
            result = pause()
            suppress_restore()
            return result
        elif name == "resume_spotify":
            from integrations.spotify import resume
            return resume()
        elif name == "skip_spotify":
            from integrations.spotify import skip
            return skip()
        elif name == "get_currently_playing":
            from integrations.spotify import now_playing
            return now_playing()
        elif name == "draft_gmail":
            from integrations.gmail import draft_email
            return draft_email(inputs["to"], inputs["subject"], inputs["body"])
        elif name == "send_gmail":
            from integrations.gmail import send_email
            return send_email(inputs["to"], inputs["subject"], inputs["body"])
        elif name == "create_notion_page":
            from integrations.notion import create_page
            return create_page(inputs["title"], inputs["content"])
        elif name == "search_notion":
            from integrations.notion import search
            return search(inputs["query"])
        elif name == "append_to_notion":
            from integrations.notion import append_to_page
            return append_to_page(inputs["page_id"], inputs["content"])
        elif name == "web_search":
            from integrations.search import web_search
            return web_search(inputs["query"], inputs.get("max_results", 3))
        elif name == "search_gmail":
            from integrations.gmail import search_emails
            return search_emails(inputs["query"], inputs.get("max_results", 5))
        elif name == "read_email":
            from integrations.gmail import read_email
            return read_email(inputs["message_id"])
        elif name == "list_calendar_events":
            from integrations.calendar import list_events
            return list_events(inputs.get("period", "today"))
        elif name == "create_calendar_event":
            from integrations.calendar import create_event
            return create_event(
                title=inputs["title"],
                start=inputs["start"],
                end=inputs["end"],
                description=inputs.get("description", ""),
                location=inputs.get("location", ""),
                attendees=inputs.get("attendees", []),
            )
        elif name == "generate_content":
            from integrations.content import generate_content
            return generate_content(inputs["idea"])
        elif name == "send_imessage":
            from integrations.messages import send_imessage
            return send_imessage(inputs["to"], inputs["message"])
        elif name == "search_imessage":
            from integrations.messages import search_imessage
            return search_imessage(
                contact=inputs.get("contact", "") or "",
                query=inputs.get("query", "") or "",
                since=inputs.get("since", "") or "",
                max_results=inputs.get("max_results", 15) or 15,
            )
        elif name == "airdrop_file":
            from integrations.messages import airdrop_file
            return airdrop_file(inputs["file_path"])
        elif name == "generate_daily_podcast":
            from tools.podcast import generate_daily_podcast_async
            return generate_daily_podcast_async()
        elif name == "look_at_screen":
            from integrations.screen import capture_screen
            shot = capture_screen()
            if not shot.get("ok"):
                return shot.get("error", "Screen capture failed.")
            return shot
        else:
            from integrations.pi_mcp import call_tool, is_pi_tool
            if is_pi_tool(name):
                return call_tool(name, inputs)
            return f"Unknown tool: {name}"
    except Exception as exc:
        return f"Tool '{name}' failed: {exc}"


def prewarm() -> None:
    """Build the tool payload + context while the user is still talking.

    Called on live partial transcripts; nothing is executed.
    """
    _tools_payload()
    _context_block(include_homelab=True, wait_for_pi=False)


async def _turn_setup(trace: LatencyTrace | None = None) -> str:
    """Fetch the (hot, cached) context block off the event loop."""
    loop = asyncio.get_running_loop()
    context = await loop.run_in_executor(
        None, lambda: _context_block(include_homelab=True, wait_for_pi=False)
    )
    mark_latency(trace, "turn_context_ready")
    return context


# ── Notch UI cards ────────────────────────────────────────────────────────────

_SPOTIFY_CARD_TOOLS = {
    "play_spotify", "pause_spotify", "resume_spotify", "skip_spotify",
    "get_currently_playing",
}
# Transitions Spotify reports lazily — refetch once the new track has settled.
_SPOTIFY_SETTLE_TOOLS = {"play_spotify", "skip_spotify", "resume_spotify"}


def _tool_detail(name: str, inputs: dict) -> str:
    for key in ("query", "title", "to", "contact", "app", "period", "idea", "action"):
        value = inputs.get(key)
        if value:
            return str(value)
    if name == "look_at_screen":
        return "display"
    return ""


def _result_summary(result) -> str:
    if isinstance(result, dict):
        return "image" if result.get("data") else ""
    if isinstance(result, list):
        return ""
    text = re.sub(r"\s+", " ", str(result or "")).strip()
    return text[:400]


def _result_failed(result) -> bool:
    if not isinstance(result, str):
        return False
    low = result.lower()
    return low.startswith(("tool '", "could not", "unknown tool", "error")) or " failed" in low[:80]


def spotify_card_event() -> dict | None:
    """Current Spotify state as a card event, or None when nothing is loaded."""
    try:
        from integrations.spotify import card_state
        data = card_state()
    except Exception:
        return None
    if not data:
        return None
    return {"event": "card", "kind": "spotify", "data": data}


def _wants_spotify_card(name: str, inputs: dict, result) -> bool:
    if name in _SPOTIFY_CARD_TOOLS:
        return True
    # control_pi_display falls back to Spotify when nothing is on the Pi.
    return (
        name == "control_pi_display"
        and isinstance(result, str)
        and "spotify" in result.lower()
    )


async def _broadcast_cards(
    name: str,
    inputs: dict,
    result,
    broadcast: Callable[[dict], Awaitable[None]],
) -> None:
    loop = asyncio.get_running_loop()
    if _wants_spotify_card(name, inputs, result):
        card = await loop.run_in_executor(_tool_pool, spotify_card_event)
        if card:
            await broadcast(card)
        if name in _SPOTIFY_SETTLE_TOOLS:
            async def _settle():
                await asyncio.sleep(1.2)
                later = await loop.run_in_executor(_tool_pool, spotify_card_event)
                if later:
                    await broadcast(later)
            asyncio.ensure_future(_settle())
        return
    if name == "play_movie":
        await broadcast({
            "event": "card",
            "kind": "media",
            "data": {
                "title": inputs.get("title") or "",
                "season": inputs.get("season"),
                "episode": inputs.get("episode"),
                "where": "Pi display" if inputs.get("on_display") else "Mac",
                "status": _result_summary(result),
            },
        })


async def _run_tool_calls(
    tool_items: list[dict],
    broadcast: Callable[[dict], Awaitable[None]],
    trace: LatencyTrace | None = None,
) -> list[dict]:
    """Execute all tool_use blocks in parallel on a thread pool."""
    loop = asyncio.get_running_loop()

    async def _one(item: dict) -> dict:
        tool_name = item["name"]
        tool_id   = item["id"]
        tool_inp  = item.get("input", {}) or {}
        print(f"{GREEN}[BRAIN] Tool: {tool_name} | {tool_inp}{RESET}", flush=True)
        await broadcast({
            "event": "tool",
            "id": tool_id,
            "name": tool_name,
            "detail": _tool_detail(tool_name, tool_inp),
        })
        mark_latency(trace, f"tool_{tool_name}_started")
        t0 = time.time()
        result = await loop.run_in_executor(_tool_pool, _execute_tool, tool_name, tool_inp)
        mark_latency(trace, f"tool_{tool_name}_completed")
        elapsed = time.time() - t0
        await broadcast({
            "event": "tool_done",
            "id": tool_id,
            "name": tool_name,
            "ok": not _result_failed(result),
            "summary": _result_summary(result),
            "elapsed": round(elapsed, 2),
        })
        try:
            await _broadcast_cards(tool_name, tool_inp, result, broadcast)
        except Exception as exc:
            print(f"{RED}[BRAIN] Card for {tool_name} failed: {exc}{RESET}", flush=True)
        if isinstance(result, dict) and result.get("ok") and result.get("data"):
            print(
                f"{GREEN}[BRAIN] Tool {tool_name} {elapsed:.2f}s | "
                f"image {result.get('width')}x{result.get('height')} "
                f"{result.get('bytes', 0) // 1024} KB{RESET}",
                flush=True,
            )
            from integrations.screen import image_content_blocks
            return {
                "type":        "tool_result",
                "tool_use_id": tool_id,
                "content":     image_content_blocks(result),
            }
        print(
            f"{GREEN}[BRAIN] Tool {tool_name} {elapsed:.2f}s | {result}{RESET}",
            flush=True,
        )
        return {
            "type":        "tool_result",
            "tool_use_id": tool_id,
            "content":     result if isinstance(result, (str, list)) else json.dumps(result),
        }

    return list(await asyncio.gather(*[_one(item) for item in tool_items]))


async def process(
    transcript: str,
    broadcast: Callable[[dict], Awaitable[None]],
) -> str:
    """Non-streaming variant: collect every spoken sentence and return them."""
    parts: list[str] = []

    async def _collect(text: str):
        parts.append(text)

    reply, _ = await process_streaming(transcript, broadcast, _collect)
    return reply


# ── Sentence splitting ────────────────────────────────────────────────────────

_ABBREVS = re.compile(
    r'\b(?:Mr|Mrs|Ms|Dr|Jr|Sr|St|Prof|Gen|Gov|Sgt|Cpl|Pvt|Rev|Hon'
    r'|Corp|Inc|Ltd|Co|vs|etc|approx|dept|est|vol|ch|pt|no|fig'
    r'|J\.A\.R\.V\.I\.S|ULTRON)\.$',
    re.IGNORECASE,
)


def _split_sentences(buffer: str) -> tuple[list[str], str]:
    """
    Pull complete sentences out of a streaming text buffer.
    Returns (complete_sentences, leftover_buffer).
    Splits on sentence-ending punctuation followed by whitespace,
    but skips abbreviations like Mr. Dr. etc.
    """
    sentences: list[str] = []

    # Find ALL potential split positions
    splits = [m.end() for m in re.finditer(r'[.!?;]\s+', buffer)]
    if not splits:
        return [], buffer

    prev = 0
    for pos in splits:
        chunk = buffer[prev:pos].strip()
        # If the chunk ends with an abbreviation followed by '.', don't split
        if _ABBREVS.search(buffer[prev:pos]):
            continue
        if chunk:
            sentences.append(chunk)
        prev = pos

    remaining = buffer[prev:]
    return sentences, remaining


# ── Streaming brain ───────────────────────────────────────────────────────────

async def process_streaming(
    transcript:  str,
    broadcast:   Callable[[dict], Awaitable[None]],
    on_sentence: Callable[[str], Awaitable[None]],
    history:     list[dict] | None = None,
    trace:       LatencyTrace | None = None,
) -> tuple[str, bool]:
    """
    Stream the model's response token-by-token.
    Calls on_sentence(text) for each complete sentence as it arrives, and
    broadcasts reply deltas so the notch shows the words as they're written.
    Handles tool-use loops (parallel calls, multi-step) transparently.
    Appends this turn's user+assistant messages to history in-place (if provided).
    Returns (full_response_clean, needs_followup) where needs_followup is True
    when the model appended [FOLLOWUP] to indicate it expects a reply.
    """
    transcript = strip_wake(transcript)
    await broadcast({"event": "reply_start"})
    if not _configured(API_KEY):
        fallback = "My mind is... momentarily elsewhere. No AI Gateway key configured."
        await broadcast({"event": "reply_delta", "text": fallback})
        await on_sentence(fallback)
        return fallback, False

    client = await _get_http()
    context = await _turn_setup(trace=trace)
    system = _system_payload(context)
    tools = _tools_payload()
    print(f"{GREEN}[BRAIN] Model {MODEL} · {len(tools)} tools{RESET}", flush=True)

    prior = list(history) if history else []
    if len(prior) > 6:
        prior = prior[-6:]
    messages      = prior + [{"role": "user", "content": transcript}]
    full_response = ""
    MAX_STEPS     = 8

    async def _say(sentence: str):
        nonlocal full_response
        full_response += sentence + " "
        spoken = sentence.replace("[FOLLOWUP]", "").strip()
        if spoken:
            await on_sentence(spoken)

    for _step in range(MAX_STEPS):
        payload = {
            "model":      MODEL,
            "max_tokens": 1024,
            "system":     system,
            "messages":   messages,
            "stream":     True,
        }
        if tools:
            payload["tools"] = tools

        text_buffer      = ""
        streamed_text    = ""
        tool_blocks: dict[int, dict] = {}
        content_for_msg: list[dict]  = []
        stop_reason      = None
        cur_block_type   = None
        cur_block_idx    = None
        logged_ttft      = False
        t_req            = time.time()

        try:
            mark_latency(trace, f"model_step_{_step + 1}_started")
            async with client.stream("POST", API_URL, headers=_headers(), json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    print(f"{RED}[BRAIN] HTTP {resp.status_code}: {body.decode()}{RESET}",
                          flush=True)
                    err_msg = "My mind is... momentarily elsewhere."
                    await broadcast({"event": "reply_delta", "text": err_msg})
                    await on_sentence(err_msg)
                    return err_msg, False

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw in ("", "[DONE]"):
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type", "")

                    if etype == "content_block_start":
                        cur_block_idx  = event["index"]
                        block          = event["content_block"]
                        cur_block_type = block["type"]

                        if cur_block_type == "text":
                            content_for_msg.append({"type": "text"})

                        elif cur_block_type == "tool_use":
                            if not logged_ttft:
                                mark_latency(trace, "model_first_token")
                                print(
                                    f"{GREEN}[BRAIN] tool-call TTFT "
                                    f"{time.time() - t_req:.2f}s{RESET}",
                                    flush=True,
                                )
                                logged_ttft = True
                            tb = {
                                "type":        "tool_use",
                                "id":          block["id"],
                                "name":        block["name"],
                                "input":       {},
                                "_input_json": "",
                            }
                            tool_blocks[cur_block_idx] = tb
                            content_for_msg.append(tb)
                            # Let the island show the action before arguments finish.
                            await broadcast({
                                "event": "tool_pending",
                                "id": block["id"],
                                "name": block["name"],
                            })

                    elif etype == "content_block_delta":
                        idx   = event.get("index")
                        delta = event.get("delta") or {}
                        dtype = delta.get("type", "")

                        if dtype == "text_delta":
                            if not logged_ttft:
                                mark_latency(trace, "model_first_token")
                                print(
                                    f"{GREEN}[BRAIN] TTFT {time.time() - t_req:.2f}s{RESET}",
                                    flush=True,
                                )
                                logged_ttft = True
                            chunk          = delta.get("text", "")
                            streamed_text += chunk
                            text_buffer   += chunk
                            await broadcast({"event": "reply_delta", "text": chunk})
                            sentences, text_buffer = _split_sentences(text_buffer)
                            for sentence in sentences:
                                await _say(sentence)

                        elif dtype == "input_json_delta":
                            if idx in tool_blocks:
                                tool_blocks[idx]["_input_json"] += delta.get("partial_json", "")

                    elif etype == "content_block_stop":
                        if cur_block_type == "tool_use" and cur_block_idx in tool_blocks:
                            tb = tool_blocks[cur_block_idx]
                            try:
                                tb["input"] = json.loads(tb.get("_input_json", "") or "{}")
                            except json.JSONDecodeError:
                                tb["input"] = {}

                    elif etype == "message_delta":
                        stop_reason = event.get("delta", {}).get("stop_reason")

        except Exception as exc:
            print(f"{RED}[BRAIN] Streaming error: {exc}{RESET}", flush=True)
            err_msg = "My mind is... momentarily elsewhere."
            await broadcast({"event": "reply_delta", "text": err_msg})
            await on_sentence(err_msg)
            return err_msg, False

        if text_buffer.strip():
            await _say(text_buffer.strip())
            text_buffer = ""

        clean_content = []
        for item in content_for_msg:
            if item["type"] == "text":
                if streamed_text.strip():
                    clean_content.append({"type": "text", "text": streamed_text})
            elif item["type"] == "tool_use":
                clean_content.append({
                    "type":  "tool_use",
                    "id":    item["id"],
                    "name":  item["name"],
                    "input": item.get("input", {}),
                })

        messages.append({"role": "assistant", "content": clean_content or streamed_text or "…"})

        if stop_reason != "tool_use":
            raw = full_response.strip() or "As you wish."
            needs_followup = raw.endswith("[FOLLOWUP]")
            clean = raw.replace("[FOLLOWUP]", "").strip()
            if history is not None:
                history.append({"role": "user",      "content": transcript})
                history.append({"role": "assistant",  "content": clean})
            return clean, needs_followup

        tool_items = [item for item in clean_content if item["type"] == "tool_use"]
        if streamed_text.strip():
            # Keep the narration and the outcome visually separate in the island.
            await broadcast({"event": "reply_delta", "text": " "})
        tool_results = await _run_tool_calls(tool_items, broadcast, trace=trace)
        messages.append({"role": "user", "content": tool_results})

    fallback = full_response.strip() or "The work is done. For now."
    if history is not None:
        history.append({"role": "user",     "content": transcript})
        history.append({"role": "assistant", "content": fallback})
    return fallback, False
