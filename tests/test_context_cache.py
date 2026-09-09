import sys
import time
import types
import unittest
from unittest.mock import patch

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = types.ModuleType("requests")

from core import context


class ContextSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.old_cache = context._block_cache

    def tearDown(self):
        context._block_cache = self.old_cache

    def test_stale_snapshot_returns_immediately_and_schedules_refresh(self):
        context._block_cache = ("cached context", time.time() - 60)
        with patch.object(context, "_schedule_refresh") as schedule:
            self.assertEqual("cached context", context.get_context_block())
        schedule.assert_called_once_with()

    def test_cold_start_returns_local_context_without_waiting(self):
        context._block_cache = None
        with patch.object(context, "_schedule_refresh") as schedule:
            block = context.get_context_block()
        self.assertIn("CURRENT CONTEXT", block)
        self.assertIn("warming in background", block)
        schedule.assert_called_once_with()

    def test_direct_weather_answer_uses_hot_snapshot(self):
        context._block_cache = (
            "--- CURRENT CONTEXT ---\nWEATHER: 21°C, clear, 5 km/h wind\n"
            "BATTERY: 80%\nNEXT EVENT: none today\nUNREAD EMAILS: 2\n"
            "-----------------------",
            time.time(),
        )
        self.assertEqual(
            "It is 21°C, clear, 5 km/h wind, sir.",
            context.direct_context_response("weather"),
        )
        self.assertEqual(
            "You have 2 unread messages, sir.",
            context.direct_context_response("unread_count"),
        )


if __name__ == "__main__":
    unittest.main()
