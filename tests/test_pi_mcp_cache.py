import sys
import time
import types
import unittest
from unittest.mock import patch


try:
    import httpx  # noqa: F401
except ImportError:
    httpx = types.ModuleType("httpx")
    httpx.Client = object
    httpx.Response = object
    httpx.Timeout = lambda *a, **k: None
    sys.modules["httpx"] = httpx

try:
    import dotenv  # noqa: F401
except ImportError:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

from integrations import pi_mcp


class PiMcpCacheTests(unittest.TestCase):
    def setUp(self):
        self.old_tools = pi_mcp._tools
        self.old_listed = pi_mcp._listed_at

    def tearDown(self):
        pi_mcp._tools = self.old_tools
        pi_mcp._listed_at = self.old_listed

    def test_stale_definitions_return_without_waiting(self):
        pi_mcp._tools = [{"name": "pi_get_status"}]
        pi_mcp._listed_at = time.time() - pi_mcp.TOOL_TTL - 1
        with (
            patch.object(pi_mcp, "configured", return_value=True),
            patch.object(pi_mcp, "_schedule_refresh") as schedule,
        ):
            self.assertEqual(
                [{"name": "pi_get_status"}],
                pi_mcp.anthropic_tools(wait_for_initial=False),
            )
        schedule.assert_called_once_with()

    def test_unrelated_cold_turn_schedules_but_does_not_block(self):
        pi_mcp._tools = []
        with (
            patch.object(pi_mcp, "configured", return_value=True),
            patch.object(pi_mcp, "_schedule_refresh") as schedule,
            patch.object(pi_mcp, "refresh", side_effect=AssertionError("blocked")),
        ):
            self.assertEqual([], pi_mcp.anthropic_tools(wait_for_initial=False))
        schedule.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
