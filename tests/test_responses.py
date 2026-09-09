import json
import unittest

from core.responses import direct_response, supports_direct_response


class DirectResponseTests(unittest.TestCase):
    def test_existing_spoken_result_passes_through(self):
        self.assertEqual(
            "Playback paused, sir.",
            direct_response("pause_spotify", "Playback paused, sir."),
        )

    def test_pi_status_is_compact(self):
        result = json.dumps({
            "services_down": [],
            "disk": {"used_pct": 41.2},
            "cpu_temp_c": 53.1,
            "internet": True,
        })
        spoken = direct_response("pi_get_status", result)
        self.assertIn("all reported services are up", spoken)
        self.assertIn("41.2 percent", spoken)
        self.assertNotIn("{", spoken)

    def test_pi_library_is_summarized(self):
        result = json.dumps({
            "total": 3,
            "movies": [{"title": "Arrival"}, {"title": "Heat"}, {"title": "Alien"}],
        })
        self.assertEqual(
            "The Pi library has 3 movies, sir. The first few are Arrival, Heat, Alien.",
            direct_response("pi_list_movies", result),
        )

    def test_pi_error_is_not_reported_as_healthy(self):
        spoken = direct_response("pi_get_status", json.dumps({"error": "timeout"}))
        self.assertIn("couldn't retrieve", spoken)
        self.assertNotIn("services are up", spoken)

    def test_search_uses_tavily_summary_only(self):
        result = "Summary: The answer is forty-two.\n\nTop 3 results for 'question':\n1. Example"
        self.assertEqual(
            "The answer is forty-two.", direct_response("web_search", result)
        )

    def test_calendar_speaks_count_and_next_event(self):
        result = (
            "Calendar events for today (2 found):\n\n"
            "• Design review — Wed Sep 9, 10:00 AM – 10:30 AM\n"
            "• Customer call — Wed Sep 9, 2:00 PM – 3:00 PM"
        )
        spoken = direct_response("list_calendar_events", result)
        self.assertEqual(
            "You have 2 events; next is Design review, Wed Sep 9, 10:00 AM – 10:30 AM, sir.",
            spoken,
        )

    def test_gmail_speaks_subjects_without_message_bodies(self):
        result = (
            "From: A <a@example.com>\nSubject: Launch plan\nDate: Today\nSnippet: secret body\n\n---\n\n"
            "From: B <b@example.com>\nSubject: Follow up\nDate: Today\nSnippet: another body"
        )
        spoken = direct_response("search_gmail", result)
        self.assertEqual(
            "I found 2 unread messages; the latest subjects are Launch plan, Follow up, sir.",
            spoken,
        )
        self.assertNotIn("secret body", spoken)

    def test_mutation_confirmation_skips_second_model_call(self):
        self.assertTrue(supports_direct_response("create_calendar_event"))
        self.assertEqual(
            "Done, sir. Meeting added to your calendar.",
            direct_response(
                "create_calendar_event",
                "Done, sir. Meeting added to your calendar.",
            ),
        )

    def test_unknown_tool_keeps_model_path(self):
        self.assertFalse(supports_direct_response("unknown_tool"))
        self.assertIsNone(direct_response("unknown_tool", "Done"))


if __name__ == "__main__":
    unittest.main()
