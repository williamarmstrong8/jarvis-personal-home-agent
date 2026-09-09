"""
MCP client for the Raspberry Pi homelab.

Talks streamable HTTP to pi-mcp over Tailscale (PI_MCP_URL in .env).
Tools are exposed to the model with a pi_ prefix so they never collide
with local Jarvis tools.
"""

from __future__ import annotations

import json
import os
import threading
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"

PREFIX = "pi_"
PROTOCOL = "2025-03-26"
TOOL_TTL = float(os.environ.get("PI_MCP_TOOL_TTL", "300"))
INIT_TIMEOUT = 20.0
CALL_TIMEOUT = 210.0

_lock = threading.Lock()
_http: httpx.Client | None = None
_session_id: str | None = None
_rpc_id = 0
_tools: list[dict] = []
_instructions = ""
_listed_at = 0.0
_last_error = ""
_refresh_thread: threading.Thread | None = None


def _url() -> str:
    return (os.environ.get("PI_MCP_URL") or "").strip()


def _token() -> str:
    return (os.environ.get("PI_MCP_TOKEN") or "").strip()


def configured() -> bool:
    url = _url()
    tok = _token()
    if not url or "your-pi" in url or "tailscale-name" in url:
        return False
    return bool(tok) and not tok.lower().startswith("your_")


def _client() -> httpx.Client:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.Client(timeout=httpx.Timeout(CALL_TIMEOUT, connect=8.0), follow_redirects=True)
    return _http


def _headers() -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _session_id:
        h["mcp-session-id"] = _session_id
    return h


def _parse_body(resp: httpx.Response) -> dict | None:
    text = resp.text.strip()
    if not text:
        return None
    ct = (resp.headers.get("content-type") or "").lower()
    if "text/event-stream" in ct or text.startswith("event:") or text.startswith("data:"):
        chunks: list[str] = []
        for line in text.splitlines():
            if line.startswith("data:"):
                chunks.append(line[5:].lstrip())
        if not chunks:
            return None
        return json.loads("\n".join(chunks))
    return json.loads(text)


def _next_id() -> int:
    global _rpc_id
    _rpc_id += 1
    return _rpc_id


def _post(payload: dict, *, timeout: float) -> tuple[httpx.Response, dict | None]:
    resp = _client().post(_url(), headers=_headers(), json=payload, timeout=timeout)
    sid = resp.headers.get("mcp-session-id")
    if sid:
        global _session_id
        _session_id = sid
    return resp, _parse_body(resp)


def _reset() -> None:
    global _session_id
    _session_id = None


def _rpc_error(body: dict | None, status: int) -> str:
    if not body:
        return f"HTTP {status}"
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err)
    if "result" in body:
        return ""
    return str(body)[:240]


def _initialize() -> None:
    global _instructions, _last_error
    _reset()
    resp, body = _post(
        {
            "jsonrpc": "2.0",
            "id": _next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "jarvis", "version": "2.0"},
            },
        },
        timeout=INIT_TIMEOUT,
    )
    if resp.status_code == 401:
        _last_error = "unauthorized — check PI_MCP_TOKEN"
        raise RuntimeError(_last_error)
    if resp.status_code >= 400:
        _last_error = f"initialize HTTP {resp.status_code}"
        raise RuntimeError(_last_error)
    err = _rpc_error(body, resp.status_code)
    if err:
        _last_error = err
        raise RuntimeError(err)
    result = (body or {}).get("result") or {}
    _instructions = (result.get("instructions") or "").strip()
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=INIT_TIMEOUT)
    _last_error = ""


def _ensure_session() -> None:
    if _session_id:
        return
    _initialize()


def _list_remote() -> list[dict]:
    resp, body = _post(
        {"jsonrpc": "2.0", "id": _next_id(), "method": "tools/list"},
        timeout=INIT_TIMEOUT,
    )
    if resp.status_code in (404, 400):
        _reset()
        _initialize()
        resp, body = _post(
            {"jsonrpc": "2.0", "id": _next_id(), "method": "tools/list"},
            timeout=INIT_TIMEOUT,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"tools/list HTTP {resp.status_code}")
    err = _rpc_error(body, resp.status_code)
    if err:
        raise RuntimeError(err)
    return ((body or {}).get("result") or {}).get("tools") or []


def _to_anthropic(remote: list[dict]) -> list[dict]:
    out = []
    for tool in remote:
        name = tool.get("name") or ""
        if not name:
            continue
        schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        schema.setdefault("properties", {})
        desc = " ".join((tool.get("description") or "").split())
        out.append({
            "name": PREFIX + name,
            "description": f"[Homelab / Raspberry Pi] {desc}",
            "input_schema": schema,
        })
    return out


def refresh() -> list[dict]:
    """Connect and cache remote tools. Empty list if the Pi is unreachable."""
    global _tools, _listed_at, _last_error
    if not configured():
        _last_error = "PI_MCP_TOKEN not set"
        return []
    with _lock:
        try:
            _ensure_session()
            remote = _list_remote()
            _tools = _to_anthropic(remote)
            _listed_at = time.time()
            print(
                f"{GREEN}[PI-MCP] Connected — {len(_tools)} tools "
                f"via Tailscale{RESET}",
                flush=True,
            )
            return list(_tools)
        except Exception as exc:
            _last_error = str(exc)
            _reset()
            print(f"{YELLOW}[PI-MCP] Unavailable: {exc}{RESET}", flush=True)
            return []


def anthropic_tools(*, wait_for_initial: bool = True) -> list[dict]:
    """Return cached definitions immediately and refresh stale data off-path."""
    if not configured():
        return []
    if _tools:
        if (time.time() - _listed_at) >= TOOL_TTL:
            _schedule_refresh()
        return list(_tools)
    if wait_for_initial:
        return refresh()
    _schedule_refresh()
    return []


def _schedule_refresh() -> None:
    global _refresh_thread
    if _refresh_thread and _refresh_thread.is_alive():
        return
    _refresh_thread = threading.Thread(
        target=refresh,
        daemon=True,
        name="jarvis-pi-mcp-refresh",
    )
    _refresh_thread.start()


def tool_names() -> list[str]:
    return [t["name"] for t in anthropic_tools(wait_for_initial=True)]


def is_pi_tool(name: str) -> bool:
    return name.startswith(PREFIX)


def live_instructions(*, wait_for_initial: bool = True) -> str:
    if not anthropic_tools(wait_for_initial=wait_for_initial):
        return ""
    extra = _instructions or "Homelab tools control the Raspberry Pi (Radarr, status, attached monitor)."
    return (
        "# Homelab\n"
        f"{extra}\n"
        "Call pi_* tools for this. Confirm before grabbing anything over 10 GB. "
        "To watch a movie or TV episode on this Mac, call play_movie with the title "
        "and season/episode numbers. Do not search Sonarr first. "
        "To open Radarr, Sonarr, Plex, Jellyfin, Home Assistant, or other dashboards "
        "in the Mac browser, call open_homelab. Pass app='all' for the main set. "
        "The Pi monitor is not this Mac's screen — do not call look_at_screen for it."
    )


def call_tool(name: str, inputs: dict | None = None) -> str:
    """Run a pi_* tool on the homelab MCP. Returns a string for the model."""
    if not configured():
        return "Homelab MCP is not configured, sir."
    remote_name = name[len(PREFIX):] if name.startswith(PREFIX) else name
    args = inputs or {}
    with _lock:
        try:
            _ensure_session()
            resp, body = _post(
                {
                    "jsonrpc": "2.0",
                    "id": _next_id(),
                    "method": "tools/call",
                    "params": {"name": remote_name, "arguments": args},
                },
                timeout=CALL_TIMEOUT,
            )
            if resp.status_code in (404, 400):
                _reset()
                _initialize()
                resp, body = _post(
                    {
                        "jsonrpc": "2.0",
                        "id": _next_id(),
                        "method": "tools/call",
                        "params": {"name": remote_name, "arguments": args},
                    },
                    timeout=CALL_TIMEOUT,
                )
            if resp.status_code >= 400:
                return f"Homelab tool '{remote_name}' failed: HTTP {resp.status_code}"
            err = _rpc_error(body, resp.status_code)
            if err:
                return f"Homelab tool '{remote_name}' failed: {err}"
            result = (body or {}).get("result") or {}
            if result.get("isError"):
                return _stringify(result)
            return _stringify(result)
        except Exception as exc:
            _reset()
            print(f"{RED}[PI-MCP] {name} failed: {exc}{RESET}", flush=True)
            return f"Couldn't reach the Raspberry Pi, sir: {exc}"


def _stringify(result: dict) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            else:
                parts.append(json.dumps(block, default=str))
        text = "\n".join(p for p in parts if p).strip()
        if text:
            return text
    if content not in (None, []):
        return json.dumps(content, default=str)
    structured = result.get("structuredContent")
    if structured is not None:
        return json.dumps(structured, default=str)
    return json.dumps(result, default=str)
