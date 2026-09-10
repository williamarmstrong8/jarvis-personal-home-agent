import os
import sys
import types
import unittest
from unittest.mock import patch

# Keep deterministic routing/display tests runnable without optional Spotify
# credentials or the production virtual environment.
try:
    import spotipy  # noqa: F401
except ImportError:
    spotipy = types.ModuleType("spotipy")
    spotipy.Spotify = object
    oauth2 = types.ModuleType("spotipy.oauth2")
    oauth2.SpotifyOAuth = object
    sys.modules["spotipy"] = spotipy
    sys.modules["spotipy.oauth2"] = oauth2

try:
    import dotenv  # noqa: F401
except ImportError:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = dotenv

from integrations import spotify as spotify_module
from integrations.spotify import (
    _ensure_playback_device,
    _local_spotify_enabled,
    _name_matches,
    _playback_matches_transition,
    _pick_device_id,
    _pick_local_device_id,
)


class PickDeviceTests(unittest.TestCase):
    def test_prefers_named_device(self):
        devices = [
            {"id": "phone", "name": "iPhone", "type": "Smartphone", "is_active": True},
            {"id": "mac", "name": "William’s MacBook", "type": "Computer", "is_active": False},
        ]
        self.assertEqual("mac", _pick_device_id(devices, preferred="macbook"))

    def test_falls_back_to_active_then_computer(self):
        devices = [
            {"id": "tv", "name": "Living Room", "type": "TV", "is_active": False},
            {"id": "mac", "name": "Mac", "type": "Computer", "is_active": False},
            {"id": "phone", "name": "iPhone", "type": "Smartphone", "is_active": True},
        ]
        self.assertEqual("phone", _pick_device_id(devices, preferred=""))
        devices[2]["is_active"] = False
        self.assertEqual("mac", _pick_device_id(devices, preferred=""))

    def test_empty_list(self):
        self.assertIsNone(_pick_device_id([], preferred="mac"))


class LocalDeviceTests(unittest.TestCase):
    def test_name_key_ignores_punctuation(self):
        self.assertTrue(
            _name_matches("William’s MacBook Pro", ["Williams-MacBook-Pro"])
        )
        self.assertTrue(
            _name_matches("William's MacBook Pro", ["William’s MacBook Pro.local"])
        )
        self.assertFalse(_name_matches("Office Desktop", ["Williams-MacBook-Pro"]))

    def test_picks_this_mac_over_active_other_computer(self):
        devices = [
            {"id": "pc", "name": "Office Desktop", "type": "Computer", "is_active": True},
            {"id": "mac", "name": "William’s MacBook Pro", "type": "Computer", "is_active": False},
        ]
        self.assertEqual(
            "mac",
            _pick_local_device_id(devices, aliases=["William's MacBook Pro"]),
        )
        self.assertEqual("pc", _pick_device_id(devices, preferred=""))


class LocalLaunchTests(unittest.TestCase):
    def test_launch_flag(self):
        with patch.dict(os.environ, {"SPOTIFY_LAUNCH_LOCAL": "0"}, clear=False):
            self.assertFalse(_local_spotify_enabled())
        with patch.dict(os.environ, {"SPOTIFY_LAUNCH_LOCAL": "1"}, clear=False):
            self.assertTrue(_local_spotify_enabled())

    def test_ensure_launches_when_list_empty(self):
        sp = object()
        with (
            patch("integrations.spotify._device_list", return_value=[]),
            patch("integrations.spotify._pick_local_device_id", return_value=None),
            patch("integrations.spotify._launch_local_spotify", return_value=True) as launch,
            patch("integrations.spotify._wait_for_local_device", return_value="mac-id") as wait,
        ):
            self.assertEqual("mac-id", _ensure_playback_device(sp))
        launch.assert_called_once()
        wait.assert_called_once_with(sp)

    def test_ensure_skips_launch_when_this_mac_online(self):
        devices = [{"id": "mac", "name": "This Mac", "type": "Computer", "is_active": False}]
        with (
            patch("integrations.spotify._device_list", return_value=devices),
            patch("integrations.spotify._pick_local_device_id", return_value="mac"),
            patch("integrations.spotify._launch_local_spotify") as launch,
        ):
            self.assertEqual("mac", _ensure_playback_device(object()))
        launch.assert_not_called()

    def test_ensure_launches_when_other_computer_is_active(self):
        other = [{"id": "pc", "name": "Office PC", "type": "Computer", "is_active": True}]
        sp = object()
        with (
            patch("integrations.spotify._device_list", return_value=other),
            patch("integrations.spotify._pick_local_device_id", return_value=None),
            patch("integrations.spotify._launch_local_spotify", return_value=True) as launch,
            patch("integrations.spotify._wait_for_local_device", return_value="mac-id") as wait,
        ):
            self.assertEqual("mac-id", _ensure_playback_device(sp))
        launch.assert_called_once()
        wait.assert_called_once_with(sp)


class DisplayTransitionTests(unittest.TestCase):
    def test_rejects_stale_track_after_skip(self):
        old = {"id": "old", "uri": "spotify:track:old"}
        current = {"is_playing": True, "item": old}
        self.assertFalse(
            _playback_matches_transition(
                current,
                expected_keys=set(),
                previous_keys={"old", "spotify:track:old"},
                expected_context_uri=None,
                require_playing=True,
            )
        )

    def test_playlist_waits_for_its_playback_context(self):
        current = {
            "is_playing": True,
            "item": {"id": "song"},
            "context": {"uri": "spotify:playlist:wanted"},
        }
        self.assertTrue(
            _playback_matches_transition(
                current,
                expected_keys=set(),
                previous_keys=set(),
                expected_context_uri="spotify:playlist:wanted",
                require_playing=True,
            )
        )
        current["context"]["uri"] = "spotify:playlist:old"
        self.assertFalse(
            _playback_matches_transition(
                current,
                expected_keys=set(),
                previous_keys=set(),
                expected_context_uri="spotify:playlist:wanted",
                require_playing=True,
            )
        )

    def test_playlist_goes_directly_to_playback_state(self):
        playlist = {
            "id": "mix",
            "uri": "spotify:playlist:mix",
            "name": "Daily Mix",
        }
        sp = object()
        with (
            patch("integrations.spotify._client", return_value=sp),
            patch("integrations.spotify._first_search_hit", return_value=playlist),
            patch("integrations.spotify._device_list", return_value=[]),
            patch("integrations.spotify._pick_local_device_id", return_value="mac"),
            patch("integrations.spotify._play_uri_local") as local_play,
            patch("integrations.spotify._start_playback") as start,
            patch("integrations.spotify._mirror_display") as mirror,
            patch("integrations.spotify._sync_display_after_transition") as sync,
        ):
            result = spotify_module.play("Daily Mix", "playlist")

        self.assertEqual("Playing Daily Mix, sir.", result)
        local_play.assert_not_called()
        start.assert_called_once_with(
            sp,
            "mac",
            context_uri="spotify:playlist:mix",
            offset={"position": 0},
        )
        mirror.assert_not_called()
        self.assertEqual(
            "spotify:playlist:mix",
            sync.call_args.kwargs["expected_context_uri"],
        )

    def test_voice_duck_freezes_the_pi_timeline(self):
        current = {
            "is_playing": True,
            "progress_ms": 42_000,
            "device": {"id": "mac"},
            "item": {"id": "song"},
        }
        client = types.SimpleNamespace(pause_playback=lambda **_kwargs: None)
        with (
            patch("integrations.spotify.playback_state", return_value=current),
            patch("integrations.spotify._client", return_value=client),
            patch("integrations.spotify._mirror_display") as mirror,
        ):
            self.assertTrue(spotify_module.pause_for_voice())

        mirror.assert_called_once_with(
            current["item"],
            playing=False,
            progress_ms=42_000,
        )


if __name__ == "__main__":
    unittest.main()
