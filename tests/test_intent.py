import unittest

from core.intent import match_intent


class IntentRoutingTests(unittest.TestCase):
    def assert_intent(self, phrase, mode, tool=None, family=None):
        intent = match_intent(phrase)
        self.assertIsNotNone(intent, phrase)
        self.assertEqual(mode, intent.mode, phrase)
        if tool is not None:
            self.assertEqual(tool, intent.tool, phrase)
        if family is not None:
            self.assertEqual(family, intent.family, phrase)
        return intent

    def test_fast_spotify_controls(self):
        pause = self.assert_intent(
            "Hey Jarvis, pause the music", "execute", "control_pi_display"
        )
        self.assertTrue(pause.inputs["fallback_spotify"])
        self.assert_intent("skip this track", "execute", "skip_spotify")
        self.assert_intent("what's playing?", "execute", "get_currently_playing")

    def test_fast_spotify_play_extracts_query(self):
        intent = self.assert_intent(
            "Jarvis, play Radiohead on Spotify", "execute", "play_spotify"
        )
        self.assertEqual({"query": "radiohead", "type": "track"}, intent.inputs)

    def test_movie_does_not_route_to_spotify(self):
        intent = self.assert_intent(
            "play Inception on Jellyfin", "execute", "play_movie"
        )
        self.assertEqual("inception", intent.inputs["title"])

    def test_episode_arguments_are_deterministic(self):
        intent = self.assert_intent(
            "watch season two episode three of Severance", "execute", "play_movie"
        )
        self.assertEqual(2, intent.inputs["season"])
        self.assertEqual(3, intent.inputs["episode"])

    def test_homelab_status_and_libraries(self):
        self.assert_intent("how is the Raspberry Pi doing", "execute", "pi_get_status")
        self.assert_intent("list my movies", "execute", "pi_list_movies")
        self.assert_intent("what shows do I have", "execute", "pi_list_series")

    def test_complete_message_can_execute(self):
        intent = self.assert_intent(
            "text Alice saying I will be there at eight", "execute", "send_imessage"
        )
        self.assertEqual("alice", intent.inputs["to"])
        self.assertEqual("i will be there at eight", intent.inputs["message"])

    def test_incomplete_mutations_force_argument_extraction(self):
        self.assert_intent("email jane@example.com about tomorrow", "force", "send_gmail")
        self.assert_intent("schedule a meeting with Alex tomorrow", "force", "create_calendar_event")

    def test_structured_email_bypasses_argument_model(self):
        intent = self.assert_intent(
            "send email to alex@example.com subject hello body checking in",
            "execute",
            "send_gmail",
        )
        self.assertEqual(
            {
                "to": "alex@example.com",
                "subject": "hello",
                "body": "checking in",
            },
            intent.inputs,
        )

    def test_domain_requests_limit_tools(self):
        self.assert_intent("search Notion for project notes", "filter", family="notion")
        self.assert_intent("restart the Plex container", "filter", family="homelab")

    def test_small_talk_omits_tool_schemas(self):
        self.assert_intent("good morning", "chat", family="chat")
        self.assert_intent("how are you", "chat", family="chat")

    def test_negation_and_compound_commands_use_full_model(self):
        self.assertIsNone(match_intent("do not play that"))
        self.assertIsNone(match_intent("play Radiohead and then email Alex"))

    def test_context_questions_use_local_snapshot(self):
        intent = self.assert_intent(
            "what time is it", "execute", "get_context_value", "context"
        )
        self.assertEqual({"kind": "time"}, intent.inputs)
        self.assertEqual(
            "weather",
            self.assert_intent(
                "what's the weather", "execute", "get_context_value"
            ).inputs["kind"],
        )
        self.assertEqual(
            "next_event",
            self.assert_intent(
                "when is my next meeting", "execute", "get_context_value"
            ).inputs["kind"],
        )


if __name__ == "__main__":
    unittest.main()
