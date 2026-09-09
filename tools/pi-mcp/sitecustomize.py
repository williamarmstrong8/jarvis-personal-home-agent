"""Load the user-writable pi-mcp server instead of /opt/pi-mcp/server.py."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

OVERRIDE = Path(os.environ.get("PI_MCP_SERVER", str(Path.home() / "pi-mcp" / "server.py")))


class _PiMcpServerFinder:
    def find_spec(self, name, path, target=None):
        if name != "server" or not OVERRIDE.is_file():
            return None
        return importlib.util.spec_from_file_location("server", OVERRIDE)


sys.meta_path.insert(0, _PiMcpServerFinder())
