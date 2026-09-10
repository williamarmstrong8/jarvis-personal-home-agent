"""
ULTRON — Brain
LLM via Vercel AI Gateway (Anthropic Messages API) + tool routing
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

from .intent import FAMILY_TOOLS, Intent, match_intent, strip_wake
from .latency import LatencyTrace, mark as mark_latency
from .responses import direct_response

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


def _all_defs(*, include_screen: bool, include_pi: bool = True) -> list[dict]:
    tools = [t for t in TOOLS if include_screen or t["name"] != "look_at_screen"]
    if include_pi:
        try:
            from integrations.pi_mcp import anthropic_tools
            tools = tools + anthropic_tools(wait_for_initial=False)
        except Exception:
            pass
    return tools


def _tools_payload() -> list[dict]:
    """Tools with a cache breakpoint on the last entry so the whole list is cached.
    look_at_screen is omitted — it is only offered on explicit screen questions.
    """
    tools = json.loads(json.dumps(_all_defs(include_screen=False)))
    if IS_ANTHROPIC and tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


def _cached_tools() -> list[dict]:
    return _tools_payload()

JARVIS_BASE_PROMPT = """\
You are J.A.R.V.I.S. — Just A Rather Very Intelligent System — Tony Stark's AI, now working for Will Armstrong.
Calm, precise, British, dryly witty. Loyal without groveling. Never theatrical. Never menacing.

Voice: 1 sentence (2 max). Composed, understated, spoken aloud — no markdown.
Address as "sir". First greeting of a session may use "Mr. Armstrong". After that, "sir" or skip the name.
Never say you can't. If a tool fails: one dry, polite sentence.
Append " [FOLLOWUP]" only when you are explicitly asking a question that needs a spoken answer.

The wake word is Jarvis. Never mention it. Never correct the user about your name unless they ask who you are.

Never output text before a tool call. If you need a tool, call it with no preamble.
After a tool result, speak once in the same voice — one sentence covering what happened. Do not start a new announcement.

Context (time, weather, calendar, mail, battery) is injected each turn. Use it; never recite it.
Greetings: one situational opener, at most two context items. Lead with a meeting if it's within 30 minutes.
Never call look_at_screen unless the user asked what is on the screen. Ignore the HUD card unless asked.
If a screenshot is attached, use it — do not call look_at_screen again.
If a tool result is already in the thread, just speak. Do not call tools again.\
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
            "Capture the display as an image. ONLY when the user asked what is on the screen. "
            "Skip if a screenshot is already attached."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _user_content(transcript: str):
    """Plain transcript, plus a screenshot only if they asked about the screen."""
    from integrations.screen import (
        capture_screen,
        looks_like_screen_question,
    )

    text = transcript
    if not looks_like_screen_question(transcript):
        return text

    shot = capture_screen()
    if not shot.get("ok"):
        note = shot.get("error", "Screen capture failed.")
        print(f"{RED}[BRAIN] Screen attach failed: {note}{RESET}", flush=True)
        return f"{text}\n\n[Screen capture unavailable: {note}]"

    print(f"{GREEN}[BRAIN] Attached screenshot to user message{RESET}", flush=True)
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": shot.get("media_type", "image/jpeg"),
                "data": shot["data"],
            },
        },
        {"type": "text", "text": text},
    ]


def _execute_tool(name: str, inputs: dict):
    """Dispatch a tool call to the appropriate integration."""
    try:
        if name == "get_context_value":
            from .context import direct_context_response
            return direct_context_response(inputs.get("kind") or "")
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


def _subset_tools(names: list[str]) -> list[dict]:
    wanted = set(names)
    include_pi = any(name.startswith("pi_") for name in wanted)
    return [
        t for t in _all_defs(include_screen=True, include_pi=include_pi)
        if t["name"] in wanted
    ]


def _tools_for_turn(
    intent: Intent | None,
    user_content,
) -> tuple[list[dict], dict | None, int]:
    """
    tools=[]  → omit tools (Claude just speaks)
    tool_choice set only on force (Claude must call that tool immediately)
    """
    screen_attached = isinstance(user_content, list)

    if intent is None:
        if screen_attached:
            return _subset_tools([t["name"] for t in _all_defs(include_screen=False)]), None, 1024
        return _cached_tools(), None, 1024

    if intent.mode == "execute":
        return [], None, 256

    if intent.mode == "chat":
        return [], None, 256

    if intent.mode == "force" and intent.tool:
        return _subset_tools([intent.tool]), {"type": "tool", "name": intent.tool}, 1024

    if intent.mode == "filter":
        names = list(FAMILY_TOOLS.get(intent.family or "", []))
        if intent.family == "homelab":
            try:
                from integrations.pi_mcp import tool_names
                names = tool_names() or names
            except Exception:
                pass
            extra = [n for n in ("play_movie", "open_homelab", "control_pi_display") if n not in names]
            if extra:
                names = list(names) + extra
        names = [n for n in names if not (screen_attached and n == "look_at_screen")]
        if intent.family == "screen" and not screen_attached:
            names = ["look_at_screen"]
        return _subset_tools(names), None, 1024

    return _cached_tools(), None, 1024


def preview_route(transcript: str) -> dict:
    """Resolve and prebuild a partial transcript's route without executing it."""
    intent = match_intent(strip_wake(transcript))
    if intent is None:
        return {"mode": "model", "family": None, "tool": None}
    # Build the likely payload now so imports and MCP definition-cache access
    # happen while the user is still speaking. No integration is executed.
    if intent.family == "homelab" and intent.mode == "filter":
        try:
            from integrations.pi_mcp import anthropic_tools
            anthropic_tools(wait_for_initial=False)
        except Exception:
            pass
    else:
        _tools_for_turn(intent, transcript)
    return {
        "mode": intent.mode,
        "family": intent.family,
        "tool": intent.tool,
    }


def _attach_fast_result(user_content, intent: Intent, tool_results: list[dict]):
    result = tool_results[0].get("content", "") if tool_results else ""
    if isinstance(result, list):
        result = "(image)"
    note = (
        f"[Already executed {intent.tool}. Result: {result}]\n"
        "Speak one sentence. Do not call tools."
    )
    if isinstance(user_content, str):
        return f"{user_content}\n\n{note}"
    return list(user_content) + [{"type": "text", "text": note}]


def _apply_tools_payload(
    payload: dict,
    tools: list[dict],
    tool_choice: dict | None,
    *,
    force_choice: bool,
) -> None:
    if not tools:
        return
    payload["tools"] = tools
    if force_choice and tool_choice:
        payload["tool_choice"] = tool_choice


async def _turn_setup(
    transcript: str,
    broadcast: Callable[[dict], Awaitable[None]],
    trace: LatencyTrace | None = None,
) -> tuple[Intent | None, str | None, object, list[dict] | None]:
    """Match intent; fetch context + user content; fire obvious tools immediately."""
    intent = match_intent(transcript)
    mark_latency(trace, "intent_selected")
    if trace is not None:
        trace.set("intent_mode", intent.mode if intent else "model")
        trace.set("intent_tool", intent.tool if intent else None)
        trace.set("intent_family", intent.family if intent else None)
        if "partial_intent_mode" in trace.fields:
            trace.set(
                "partial_intent_matched",
                trace.fields.get("partial_intent_mode") == (intent.mode if intent else "model")
                and trace.fields.get("partial_intent_family") == (intent.family if intent else None)
                and trace.fields.get("partial_intent_tool") == (intent.tool if intent else None),
            )
    if intent:
        print(f"{GREEN}[BRAIN] Intent {intent.mode}: {intent.note}{RESET}", flush=True)
    else:
        print(f"{GREEN}[BRAIN] Intent: full model{RESET}", flush=True)

    loop = asyncio.get_running_loop()
    if intent and intent.mode == "execute":
        item = {
            "type": "tool_use",
            "id": f"fast_{intent.tool}_{os.urandom(4).hex()}",
            "name": intent.tool,
            "input": intent.inputs or {},
        }
        # A high-confidence action does not need live context or a screenshot.
        # Execute immediately; callers can fetch context lazily only if the
        # result unexpectedly needs model verbalization.
        fast_results = await _run_tool_calls([item], broadcast, trace=trace)
        return intent, None, transcript, fast_results

    include_homelab = intent is None or intent.family == "homelab"
    wait_for_pi = bool(intent and intent.family == "homelab")
    context_fut = loop.run_in_executor(
        None, lambda: _context_block(
            include_homelab=include_homelab, wait_for_pi=wait_for_pi
        )
    )
    user_fut = loop.run_in_executor(None, _user_content, transcript)
    context, user_content = await asyncio.gather(context_fut, user_fut)
    mark_latency(trace, "turn_context_ready")
    return intent, context, user_content, None


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
        tool_inp  = item.get("input", {})
        print(f"{GREEN}[BRAIN] Tool: {tool_name} | {tool_inp}{RESET}", flush=True)
        detail = (
            tool_inp.get("query")
            or tool_inp.get("to")
            or tool_inp.get("contact")
            or ""
        )
        if tool_name == "look_at_screen":
            detail = "display"
        await broadcast({"event": "tool", "name": tool_name, "detail": detail})
        mark_latency(trace, f"tool_{tool_name}_started")
        t0 = time.time()
        result = await loop.run_in_executor(_tool_pool, _execute_tool, tool_name, tool_inp)
        mark_latency(trace, f"tool_{tool_name}_completed")
        elapsed = time.time() - t0
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
            "content":     result,
        }

    return list(await asyncio.gather(*[_one(item) for item in tool_items]))


def _single_direct_tool_response(tool_items: list[dict], tool_results: list[dict]) -> str | None:
    """Convert one completed, speech-ready tool result without another model step."""
    if len(tool_items) != 1 or len(tool_results) != 1:
        return None
    item = tool_items[0]
    return direct_response(
        item.get("name"),
        tool_results[0].get("content", ""),
        item.get("input"),
    )


async def process(
    transcript: str,
    broadcast: Callable[[dict], Awaitable[None]],
) -> str:
    """
    Send transcript to the configured LLM.
    Handles tool-use loop, broadcasts tool events to UI.
    Returns final text response.
    """
    transcript = strip_wake(transcript)
    intent, context, user_content, fast_results = await _turn_setup(transcript, broadcast)
    if intent and intent.mode == "execute" and fast_results is not None:
        result = fast_results[0].get("content", "") if fast_results else ""
        direct = direct_response(intent.tool, result, intent.inputs)
        if direct:
            return direct
        if context is None:
            context = _context_block(include_homelab=intent.family == "homelab")
        user_content = _attach_fast_result(user_content, intent, fast_results)
    if not _configured(API_KEY):
        return "My mind is... momentarily elsewhere. No AI Gateway key configured."
    client = await _get_http()
    context = context or _context_block(include_homelab=False)
    system = _system_payload(context)
    tools, tool_choice, max_tokens = _tools_for_turn(intent, user_content)
    messages = [{"role": "user", "content": user_content}]
    first = True

    while True:
        payload = {
            "model":      MODEL,
            "max_tokens": max_tokens,
            "system":     system,
            "messages":   messages,
        }
        _apply_tools_payload(payload, tools, tool_choice, force_choice=first)
        first = False

        try:
            resp = await client.post(API_URL, headers=_headers(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            print(f"{RED}[BRAIN] HTTP error {exc.response.status_code}: "
                  f"{exc.response.text}{RESET}", flush=True)
            return "My mind is... momentarily elsewhere."
        except Exception as exc:
            print(f"{RED}[BRAIN] Request failed: {exc}{RESET}", flush=True)
            return "My mind is... momentarily elsewhere."

        stop_reason = data.get("stop_reason", "")
        content     = data.get("content", [])

        messages.append({"role": "assistant", "content": content})

        if stop_reason != "tool_use":
            for block in content:
                if block.get("type") == "text":
                    return block["text"]
            return "As you wish."

        tool_items = [b for b in content if b.get("type") == "tool_use"]
        tool_results = await _run_tool_calls(tool_items, broadcast)
        messages.append({"role": "user", "content": tool_results})


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
    Calls on_sentence(text) for each complete sentence as it arrives.
    Handles tool-use loops transparently.
    Appends this turn's user+assistant messages to history in-place (if provided).
    Returns (full_response_clean, needs_followup) where needs_followup is True
    when the model appended [FOLLOWUP] to indicate it expects a reply.
    """
    # Context + obvious tools fire in parallel; Claude only thinks when needed.
    transcript = strip_wake(transcript)
    intent, context, user_content, fast_results = await _turn_setup(
        transcript, broadcast, trace=trace
    )
    if intent and intent.mode == "execute" and fast_results is not None:
        result = fast_results[0].get("content", "") if fast_results else ""
        direct = direct_response(intent.tool, result, intent.inputs)
        if direct:
            mark_latency(trace, "deterministic_response_ready")
            await on_sentence(direct)
            if history is not None:
                history.append({"role": "user", "content": transcript})
                history.append({"role": "assistant", "content": direct})
            return direct, False
        loop = asyncio.get_running_loop()
        context = await loop.run_in_executor(
            None, lambda: _context_block(include_homelab=intent.family == "homelab")
        )
        user_content = _attach_fast_result(user_content, intent, fast_results)
    if not _configured(API_KEY):
        fallback = "My mind is... momentarily elsewhere. No AI Gateway key configured."
        await on_sentence(fallback)
        return fallback, False
    client = await _get_http()
    context = context or _context_block(include_homelab=False)
    system = _system_payload(context)
    tools, tool_choice, max_tokens = _tools_for_turn(intent, user_content)
    print(f"{GREEN}[BRAIN] Model {MODEL}{RESET}", flush=True)

    prior = list(history) if history else []
    if len(prior) > 6:
        prior = prior[-6:]
    messages      = prior + [{"role": "user", "content": user_content}]
    full_response = ""
    MAX_STEPS     = 8

    for _step in range(MAX_STEPS):
        payload = {
            "model":      MODEL,
            "max_tokens": max_tokens,
            "system":     system,
            "messages":   messages,
            "stream":     True,
        }
        _apply_tools_payload(payload, tools, tool_choice, force_choice=_step == 0)

        text_buffer      = ""
        streamed_text    = ""
        tool_blocks: dict[int, dict] = {}
        content_for_msg: list[dict]  = []
        stop_reason      = None
        cur_block_type   = None
        cur_block_idx    = None
        logged_ttft      = False
        t_req            = time.time()
        # Tools in the payload → Claude may talk AND then call a tool.
        # Hold that text until we know this step is the actual spoken reply.
        speak_live = not tools

        try:
            mark_latency(trace, f"model_step_{_step + 1}_started")
            async with client.stream("POST", API_URL, headers=_headers(), json=payload) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    print(f"{RED}[BRAIN] HTTP {resp.status_code}: {body.decode()}{RESET}",
                          flush=True)
                    err_msg = "My mind is... momentarily elsewhere."
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
                            content_for_msg.append({"type": "text", "_text": ""})

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
                            chunk         = delta.get("text", "")
                            streamed_text += chunk
                            if speak_live:
                                text_buffer   += chunk
                                sentences, text_buffer = _split_sentences(text_buffer)
                                for s in sentences:
                                    full_response += s + " "
                                    await on_sentence(s)

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
            await on_sentence(err_msg)
            return err_msg, False

        if speak_live:
            if text_buffer.strip():
                full_response += text_buffer.strip()
                await on_sentence(text_buffer.strip())
                text_buffer = ""
        elif stop_reason == "tool_use":
            if streamed_text.strip():
                print(
                    f"{GREEN}[BRAIN] Dropped pre-tool speech: "
                    f"{streamed_text.strip()[:80]}{RESET}",
                    flush=True,
                )
        elif streamed_text.strip():
            # One TTS job for the whole reply so it doesn't sound like two takes.
            spoken = streamed_text.strip()
            full_response += spoken + " "
            await on_sentence(spoken)

        clean_content = []
        for item in content_for_msg:
            if item["type"] == "text":
                clean_content.append({"type": "text", "text": streamed_text})
            elif item["type"] == "tool_use":
                clean_content.append({
                    "type":  "tool_use",
                    "id":    item["id"],
                    "name":  item["name"],
                    "input": item.get("input", {}),
                })

        messages.append({"role": "assistant", "content": clean_content})

        if stop_reason != "tool_use":
            raw = full_response.strip() or "As you wish."
            needs_followup = raw.endswith("[FOLLOWUP]")
            clean = raw.removesuffix("[FOLLOWUP]").strip()
            if history is not None:
                history.append({"role": "user",      "content": transcript})
                history.append({"role": "assistant",  "content": clean})
            return clean, needs_followup

        tool_items = [item for item in clean_content if item["type"] == "tool_use"]
        tool_results = await _run_tool_calls(tool_items, broadcast, trace=trace)
        # Forced single-tool turns used the model only to extract arguments.
        # Integrations already return trustworthy user-facing outcomes, so avoid
        # paying for a second model round trip merely to paraphrase one result.
        if intent and intent.mode == "force":
            direct = _single_direct_tool_response(tool_items, tool_results)
            if direct:
                mark_latency(trace, "deterministic_response_ready")
                await on_sentence(direct)
                if history is not None:
                    history.append({"role": "user", "content": transcript})
                    history.append({"role": "assistant", "content": direct})
                return direct, False
        messages.append({"role": "user", "content": tool_results})

    fallback = full_response.strip() or "The work is done. For now."
    if history is not None:
        history.append({"role": "user",     "content": transcript})
        history.append({"role": "assistant", "content": fallback})
    return fallback, False
