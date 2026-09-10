import os
import unittest
from unittest.mock import patch

from integrations.spotify import (
    _ensure_playback_device,
    _local_spotify_enabled,
    _name_matches,
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


if __name__ == "__main__":
    unittest.main()
