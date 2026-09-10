import asyncio
import sys
import types
import unittest
from unittest.mock import patch


# Keep this unit test runnable before the project's optional environment is
# installed. The deterministic path never instantiates an HTTP client.
try:
    import httpx  # noqa: F401
except ImportError:
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = object
    httpx.Client = object
    httpx.Response = object
    httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
    httpx.TimeoutException = type("TimeoutException", (Exception,), {})
    httpx.Timeout = lambda *a, **k: None
    httpx.Limits = lambda *a, **k: None
    sys.modules["httpx"] = httpx

try:
    import dotenv  # noqa: F401
except ImportError:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

from core import brain


class BrainFastPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_command_needs_no_gateway_or_context(self):
        events = []
        spoken = []

        async def broadcast(event):
            events.append(event)

        async def on_sentence(text):
            spoken.append(text)

        with (
            patch.object(brain, "API_KEY", ""),
            patch.object(brain, "_execute_tool", return_value="Playback paused, sir."),
            patch.object(brain, "_context_block", side_effect=AssertionError("context fetched")),
        ):
            result, followup = await brain.process_streaming(
                "pause the music", broadcast, on_sentence
            )

        self.assertEqual("Playback paused, sir.", result)
        self.assertFalse(followup)
        self.assertEqual(["Playback paused, sir."], spoken)
        self.assertEqual("control_pi_display", events[0]["name"])

    async def test_context_question_needs_no_gateway(self):
        events = []
        spoken = []

        async def broadcast(event):
            events.append(event)

        async def on_sentence(text):
            spoken.append(text)

        with (
            patch.object(brain, "API_KEY", ""),
            patch.object(brain, "_execute_tool", return_value="It is 9:41 AM, sir."),
            patch.object(brain, "_context_block", side_effect=AssertionError("context fetched")),
        ):
            result, followup = await brain.process_streaming(
                "what time is it", broadcast, on_sentence
            )

        self.assertEqual("It is 9:41 AM, sir.", result)
        self.assertFalse(followup)
        self.assertEqual(["It is 9:41 AM, sir."], spoken)
        self.assertEqual("get_context_value", events[0]["name"])

    def test_small_talk_has_no_tool_payload(self):
        intent = brain.Intent("chat", family="chat", note="conversation")
        tools, choice, max_tokens = brain._tools_for_turn(intent, "good morning")
        self.assertEqual([], tools)
        self.assertIsNone(choice)
        self.assertEqual(256, max_tokens)

    def test_single_forced_tool_result_can_skip_verbalization_model(self):
        direct = brain._single_direct_tool_response(
            [{
                "name": "create_calendar_event",
                "input": {"title": "Design review"},
            }],
            [{"content": "Done, sir. 'Design review' added to your calendar."}],
        )
        self.assertEqual(
            "Done, sir. 'Design review' added to your calendar.",
            direct,
        )

    def test_partial_route_preview_does_not_execute(self):
        with patch.object(
            brain, "_execute_tool", side_effect=AssertionError("tool executed")
        ):
            route = brain.preview_route("pause the movie on the pi")
        self.assertEqual("execute", route["mode"])
        self.assertEqual("homelab", route["family"])


if __name__ == "__main__":
    unittest.main()
