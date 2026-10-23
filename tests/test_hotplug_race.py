#!/usr/bin/python3
"""Regression: a device node vanishing during rescan must not kill the daemon (2026-10-21 incident)."""
from __future__ import annotations

import unittest
from unittest import mock

import test_daemon_logic  # noqa: F401  (installs evdev/pyudev stubs when missing)
import input_lock_daemon as daemon_module
from input_lock_daemon import InputLockDaemon


def _vanished(*_args, **_kwargs):
    raise FileNotFoundError(2, "No such file or directory", "/dev/input/event20")


class HotplugRaceTests(unittest.TestCase):
    def test_list_devices_race_falls_back_and_skips_vanished_node(self):
        real_stat = daemon_module.os.stat

        def fake_stat(path, *a, **k):
            if path == "/dev/input/event20":
                raise FileNotFoundError(path)
            return real_stat(path, *a, **k)

        with mock.patch.object(daemon_module.evdev, "list_devices", side_effect=_vanished, create=True), \
             mock.patch.object(daemon_module.os, "listdir", return_value=["event20", "mouse0"]), \
             mock.patch.object(daemon_module.os, "stat", side_effect=fake_stat):
            self.assertEqual(InputLockDaemon._list_event_devices(), set())

    def test_list_devices_normal_path_unchanged(self):
        with mock.patch.object(daemon_module.evdev, "list_devices", return_value=["/dev/input/event3"], create=True):
            self.assertEqual(InputLockDaemon._list_event_devices(), {"/dev/input/event3"})

    def test_handle_udev_survives_transient_rescan_error(self):
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        monitor = mock.Mock()
        monitor.poll.side_effect = [object(), None]
        daemon.udev_monitor = monitor
        daemon._rescan_devices = mock.Mock(side_effect=FileNotFoundError("/dev/input/event20"))
        daemon._handle_udev()  # must not raise
        daemon._rescan_devices.assert_called_once()


if __name__ == "__main__":
    unittest.main()
