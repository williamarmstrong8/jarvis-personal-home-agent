"""
J.A.R.V.I.S. — WebSocket Server
Bridges the Python backend to the Electron UI.
"""

import asyncio
import json
import os

import websockets
from dotenv import load_dotenv

load_dotenv()

WS_PORT = int(os.environ.get("WS_PORT", 8765))

CYAN  = "\033[96m"
RESET = "\033[0m"

_clients: set = set()


async def _handler(websocket):
    _clients.add(websocket)
    print(f"{CYAN}[WS] Client connected ({len(_clients)} total){RESET}", flush=True)
    try:
        async for _ in websocket:
            pass  # we don't expect messages from the UI
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _clients.discard(websocket)
        print(f"{CYAN}[WS] Client disconnected ({len(_clients)} remaining){RESET}",
              flush=True)


async def broadcast(message: dict):
    """Send a JSON message to every connected WebSocket client."""
    if not _clients:
        print(f"{CYAN}[WS] No clients connected — skipping broadcast: {message}{RESET}",
              flush=True)
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
