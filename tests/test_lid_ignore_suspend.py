"""Continuous lid-ignore survives an unrelated suspend and a locked screen (2026-10-22 decision)."""
from __future__ import annotations

import unittest
from unittest import mock

from test_daemon_logic import daemon_module
from test_remediation_linux import daemon_fixture, device


class LidIgnoreAcrossSuspendTests(unittest.TestCase):
    def armed(self):
        daemon = daemon_fixture()
        daemon.ignore_lid_close = True
        daemon.lid_policy_healthy = True
        daemon.lid_inhibitor_fd = 41
        daemon._release_lid_inhibitor = mock.Mock()
        keyboard = device(daemon, "keyboard", "keyboard")
        lid = device(daemon, "lid", "lid-switch")
        return daemon, keyboard, lid

    def test_suspend_releases_input_but_keeps_lid_ignore(self):
        daemon, keyboard, lid = self.armed()
        generation = daemon.lid_generation
        daemon._release_inputs_keep_lid("system-sleep-hook")
        keyboard.device.ungrab.assert_called_once()
        lid.device.ungrab.assert_not_called()
        self.assertTrue(lid.grabbed)
        self.assertTrue(daemon.ignore_lid_close)
        self.assertEqual(daemon.lid_inhibitor_fd, 41)
        daemon._release_lid_inhibitor.assert_not_called()
        self.assertEqual(daemon.lid_generation, generation)
        self.assertFalse(daemon.locked_kinds)
        self.assertEqual(daemon.last_action, "system-sleep-hook-lid-ignore-kept")

    def test_without_lid_ignore_the_full_emergency_unlock_still_runs(self):
        daemon = daemon_fixture()
        keyboard = device(daemon, "keyboard", "keyboard")
        daemon._release_inputs_keep_lid("system-suspend")
        keyboard.device.ungrab.assert_called_once()
        self.assertFalse(daemon.locked_kinds)
        self.assertEqual(daemon.last_action, "system-suspend")

    def test_release_failure_falls_back_to_emergency_unlock(self):
        daemon, keyboard, lid = self.armed()
        daemon._set_locked_kinds = mock.Mock(side_effect=RuntimeError("release failed"))
        daemon._release_inputs_keep_lid("system-suspend")
        self.assertFalse(daemon.ignore_lid_close)
        lid.device.ungrab.assert_called_once()

    def test_protective_suspend_path_is_unchanged(self):
        # _start_protection releases lid-ignore itself before requesting sleep,
        # so the following prepare-sleep sees lid-ignore already off.
        daemon, keyboard, lid = self.armed()
        daemon._emergency_unlock("safety-protection", preserve_safety=True)
        self.assertFalse(daemon.ignore_lid_close)
        daemon._release_inputs_keep_lid("system-sleep-hook")
        self.assertFalse(daemon.ignore_lid_close)


class LidIgnoreAcrossScreenLockTests(unittest.TestCase):
    def armed(self, lid_closed):
        daemon, keyboard, lid = LidIgnoreAcrossSuspendTests.armed(self)
        daemon.lid_is_closed = lid_closed
        daemon._start_protection = mock.Mock()
        daemon.wakeup_read = mock.Mock()
        daemon.wakeup_read.recv.side_effect = BlockingIOError()
        daemon.control_queue = daemon_module.queue.SimpleQueue()
        return daemon, keyboard, lid

    def drain(self, daemon, *events):
        for event in events:
            daemon.control_queue.put(event)
        daemon._drain_wakeup()

    def test_screen_lock_with_lid_closed_keeps_lid_ignore_and_starts_no_protection(self):
        daemon, keyboard, lid = self.armed(lid_closed=True)
        self.drain(daemon, "session-locked")
        daemon._start_protection.assert_not_called()
        self.assertTrue(daemon.ignore_lid_close)
        self.assertTrue(lid.grabbed)
        keyboard.device.ungrab.assert_called_once()  # User can type the password.
        self.assertFalse(daemon.authorized_session_active)

    def test_suspend_then_gnome_lock_on_suspend_keeps_lid_ignore(self):
        # The exact order seen in the journal: prepare-sleep, then the lock.
        daemon, keyboard, lid = self.armed(lid_closed=True)
        self.drain(daemon, "prepare-sleep", "session-locked")
        daemon._start_protection.assert_not_called()
        self.assertTrue(daemon.ignore_lid_close)
        self.assertTrue(lid.grabbed)

    def test_session_really_gone_still_ends_lid_ignore_safely(self):
        daemon, keyboard, lid = self.armed(lid_closed=True)
        self.drain(daemon, "session-inactive")
        daemon._start_protection.assert_called_once_with("SUPERVISION_LOST")

    def test_monitor_classifies_locked_local_session(self):
        good = dict(Active=True, Remote=False, Class="user", Type="wayland", Seat=("seat0", "/seat"))
        locked = {**good, "LockedHint": True}
        self.assertFalse(daemon_module.session_is_eligible(locked))
        self.assertTrue(daemon_module.session_is_locked_local(locked))
        for change in ({"Class": "manager"}, {"Remote": True}, {"Active": False}, {"Type": "tty"}):
            self.assertFalse(daemon_module.session_is_locked_local({**locked, **change}), change)


if __name__ == "__main__":
    unittest.main()
