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

    def test_handle_udev_survives_evdev_systemerror(self):
        # 2026-10-28: input_props() on a vanished node raised SystemError.
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        monitor = mock.Mock()
        monitor.poll.side_effect = [object(), None]
        daemon.udev_monitor = monitor
        daemon._rescan_devices = mock.Mock(side_effect=SystemError(
            "<built-in function ioctl_EVIOCGPROP> returned NULL without setting an exception"))
        daemon._handle_udev()  # must not raise
        daemon._rescan_devices.assert_called_once()

    def _attach_with(self, device):
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        daemon.devices = {}
        daemon.selector = mock.Mock()
        daemon.udev_context = None
        with mock.patch.object(daemon_module.evdev, "InputDevice", return_value=device, create=True), \
             mock.patch.object(daemon_module.pyudev.Devices, "from_device_file",
                               side_effect=OSError("gone"), create=True):
            daemon._attach_device("/dev/input/event20")
        return daemon

    def test_attach_skips_and_closes_device_that_vanishes_during_classify(self):
        device = mock.Mock()
        device.capabilities.return_value = {}
        device.input_props.side_effect = SystemError(
            "<built-in function ioctl_EVIOCGPROP> returned NULL without setting an exception")
        daemon = self._attach_with(device)  # must not raise
        self.assertEqual(daemon.devices, {})
        device.close.assert_called()
        daemon.selector.register.assert_not_called()

    def test_attach_closes_device_when_open_query_fails_with_oserror(self):
        device = mock.Mock()
        device.capabilities.side_effect = OSError(19, "No such device")
        device.input_props.return_value = []
        daemon = self._attach_with(device)
        self.assertEqual(daemon.devices, {})
        device.close.assert_called()


if __name__ == "__main__":
    unittest.main()
