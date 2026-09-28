"""
J.A.R.V.I.S. — WebSocket Server
Bridges the Python backend to the Electron notch UI.

Outbound: state/transcript/tool/card events (see ui/island.js).
Inbound:  {"type": "text" | "listen" | "stop" | "media", ...} from the island.
"""

import json
import os
from typing import Callable

import websockets
from dotenv import load_dotenv

load_dotenv()

WS_PORT = int(os.environ.get("WS_PORT", 8765))

CYAN  = "\033[96m"
RED   = "\033[91m"
RESET = "\033[0m"

_clients: set = set()
_command_handler: Callable[[dict], None] | None = None

# Replayed to a UI that (re)connects mid-turn so the island never shows stale state.
_STATE_EVENTS = {"idle", "wake", "listening", "followup_listening", "thinking", "speaking"}
_last_state: dict | None = None


def set_command_handler(handler: Callable[[dict], None]) -> None:
    """Register the callback for commands the UI sends (runs on the WS loop)."""
    global _command_handler
    _command_handler = handler


async def _handler(websocket):
    _clients.add(websocket)
    print(f"{CYAN}[WS] Client connected ({len(_clients)} total){RESET}", flush=True)
    try:
        if _last_state is not None:
            await websocket.send(json.dumps(_last_state))
        async for raw in websocket:
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict) or not message.get("type"):
                continue
            if _command_handler is None:
                continue
            try:
                _command_handler(message)
            except Exception as exc:
                print(f"{RED}[WS] Command {message.get('type')} failed: {exc}{RESET}",
                      flush=True)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _clients.discard(websocket)
        print(f"{CYAN}[WS] Client disconnected ({len(_clients)} remaining){RESET}",
              flush=True)


async def broadcast(message: dict):
    """Send a JSON message to every connected WebSocket client."""
    global _last_state
    if message.get("event") in _STATE_EVENTS:
        _last_state = message
    if not _clients:
        return

    payload = json.dumps(message)
    dead = set()
    for ws in list(_clients):
        try:
            await ws.send(payload)
        except websockets.exceptions.ConnectionClosed:
            dead.add(ws)

    _clients.difference_update(dead)


async def start_server():
    """Bind the WebSocket server. Returns after it is listening."""
    print(f"{CYAN}[WS] Starting WebSocket server on ws://127.0.0.1:{WS_PORT}{RESET}",
          flush=True)
    # IPv4 only — "localhost" also tries ::1 and fails if another Jarvis
    # already bound IPv6 even when IPv4 looks free.
    return await websockets.serve(_handler, "127.0.0.1", WS_PORT)
