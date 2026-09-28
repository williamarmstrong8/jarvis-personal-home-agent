import copy
import json
import sys
import types
import unittest
from unittest.mock import patch


# Keep this unit test runnable before the project's optional environment is
# installed. The HTTP client is replaced by a scripted fake below.
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


def sse(*events):
    return [f"data: {json.dumps(e)}" for e in events]


def text_step(text):
    return sse(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    )


def tool_step(*calls, narration=None):
    events = []
    index = 0
    if narration:
        events += [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": narration}},
            {"type": "content_block_stop", "index": 0},
        ]
        index = 1
    for call_id, name, inputs in calls:
        events += [
            {"type": "content_block_start", "index": index,
             "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}},
            {"type": "content_block_delta", "index": index,
             "delta": {"type": "input_json_delta", "partial_json": json.dumps(inputs)}},
            {"type": "content_block_stop", "index": index},
        ]
        index += 1
    events.append({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
    return sse(*events)


class FakeResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class FakeStream:
    def __init__(self, lines):
        self._response = FakeResponse(lines)

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    def __init__(self, steps):
        self.steps = list(steps)
        self.payloads = []

    def stream(self, method, url, headers=None, json=None):
        self.payloads.append(copy.deepcopy(json))
        return FakeStream(self.steps.pop(0))


class ModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def run_turn(self, transcript, steps, tool_results=None):
        client = FakeClient(steps)
        events, spoken, calls = [], [], []

        async def broadcast(event):
            events.append(event)

        async def on_sentence(text):
            spoken.append(text)

        async def get_http():
            return client

        def execute(name, inputs):
            calls.append((name, inputs))
            return (tool_results or {}).get(name, "Done, sir.")

        with (
            patch.object(brain, "API_KEY", "test-key"),
            patch.object(brain, "_get_http", get_http),
            patch.object(brain, "_context_block", return_value="TIME: 9:41"),
            patch.object(brain, "_all_defs", return_value=list(brain.TOOLS)),
            patch.object(brain, "_execute_tool", side_effect=execute),
            patch.object(brain, "spotify_card_event", return_value=None),
        ):
            result = await brain.process_streaming(transcript, broadcast, on_sentence)
        return result, client, events, spoken, calls

    async def test_every_turn_offers_every_tool_without_forcing(self):
        _, client, *_ = await self.run_turn(
            "pause the music", [text_step("Paused, sir.")]
        )
        payload = client.payloads[0]
        names = {t["name"] for t in payload["tools"]}
        self.assertEqual({t["name"] for t in brain.TOOLS}, names)
        self.assertNotIn("tool_choice", payload)
        self.assertEqual("pause the music", payload["messages"][-1]["content"])

    async def test_parallel_tool_calls_then_spoken_outcome(self):
        (reply, followup), client, events, spoken, calls = await self.run_turn(
            "play Radiohead and text Alex I'm late",
            [
                tool_step(
                    ("t1", "play_spotify", {"query": "Radiohead", "type": "artist"}),
                    ("t2", "send_imessage", {"to": "Alex", "message": "I'm late"}),
                    narration="On it, sir. ",
                ),
                text_step("Radiohead is on and Alex has been warned, sir."),
            ],
        )
        self.assertEqual(
            [("play_spotify", {"query": "Radiohead", "type": "artist"}),
             ("send_imessage", {"to": "Alex", "message": "I'm late"})],
            calls,
        )
        self.assertEqual(["On it, sir.", "Radiohead is on and Alex has been warned, sir."], spoken)
        self.assertFalse(followup)
        kinds = [e["event"] for e in events]
        self.assertEqual("reply_start", kinds[0])
        self.assertIn("tool_pending", kinds)
        self.assertEqual(2, kinds.count("tool_done"))
        # Tool results are sent back for the model to verbalize.
        second = client.payloads[1]["messages"]
        self.assertEqual("tool_result", second[-1]["content"][0]["type"])

    async def test_followup_marker_is_stripped(self):
        (reply, followup), *_ , spoken, _ = await self.run_turn(
            "email Jane", [text_step("What should it say, sir? [FOLLOWUP]")]
        )
        self.assertTrue(followup)
        self.assertEqual("What should it say, sir?", reply)
        self.assertEqual(["What should it say, sir?"], spoken)

    async def test_suit_up_is_a_model_tool(self):
        brain.consume_suit_up_request()
        brain._execute_tool("suit_up", {})
        self.assertTrue(brain.consume_suit_up_request())
        self.assertFalse(brain.consume_suit_up_request())

    async def test_missing_key_speaks_fallback(self):
        spoken = []

        async def broadcast(_event):
            pass

        async def on_sentence(text):
            spoken.append(text)

        with patch.object(brain, "API_KEY", ""):
            reply, _ = await brain.process_streaming("hello", broadcast, on_sentence)
        self.assertIn("No AI Gateway key", reply)
        self.assertEqual([reply], spoken)


class StripWakeTests(unittest.TestCase):
    def test_strips_leading_wake_phrase(self):
        self.assertEqual("play some music", brain.strip_wake("Hey Jarvis, play some music"))
        self.assertEqual("what's up", brain.strip_wake("jarvis what's up"))

    def test_keeps_bare_wake_word(self):
        self.assertEqual("Jarvis", brain.strip_wake("Jarvis"))


if __name__ == "__main__":
    unittest.main()
