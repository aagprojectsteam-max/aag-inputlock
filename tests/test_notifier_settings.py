#!/usr/bin/python3
from __future__ import annotations

import errno
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))

import input_lock_notifier as notifier_module  # noqa: E402
from input_lock_notifier import TrayNotifier  # noqa: E402


class DisconnectTests(unittest.TestCase):
    def tray(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        tray.connected = True
        tray.current_state = {'lid_switch_devices': 1, 'ignore_lid_close': True, 'lid_policy_healthy': True}
        tray.previous_locked = None
        for name in ('status_item', 'safety_item', 'toggle_item', 'lid_item', 'indicator', 'shortcut_item', 'config'):
            setattr(tray, name, mock.Mock())
        tray._show_error = mock.Mock(return_value=False)
        return tray

    def test_disconnect_disables_both_and_restore_cannot_reenable_stale_state(self):
        tray = self.tray()
        tray._show_disconnected()
        tray._restore_lid_item()
        self.assertFalse(tray.connected)
        tray.toggle_item.set_sensitive.assert_called_with(False)
        tray.lid_item.set_sensitive.assert_called_with(False)
        self.assertIn('unknown', tray.safety_item.set_label.call_args.args[0])
        tray._handle_message({'subscribed': True, 'state': tray.current_state})
        self.assertTrue(tray.connected)
        tray.toggle_item.set_sensitive.assert_called_with(True)
        tray.lid_item.set_sensitive.assert_called_with(True)

    def test_missing_socket_and_refused_races_are_actionable_and_disabled(self):
        for error in (FileNotFoundError(errno.ENOENT, 'missing'), ConnectionRefusedError(errno.ECONNREFUSED, 'refused')):
            for action in ('lid', 'input'):
                tray = self.tray()
                with (mock.patch.object(notifier_module, 'send_request', side_effect=error),
                      mock.patch.object(notifier_module.GLib, 'idle_add', side_effect=lambda fn, *args: fn(*args))):
                    if action == 'lid':
                        tray._lid_toggle_worker(True, 1)
                    else:
                        tray._toggle_worker(False)
                self.assertFalse(tray.connected)
                tray.lid_item.set_sensitive.assert_called_with(False)
                self.assertIn('InputLock background service unavailable', tray._show_error.call_args.args[1])
                self.assertNotIn('[Errno', tray._show_error.call_args.args[1])

    def test_policy_refusal_remains_visible_and_lid_toggle_reversible(self):
        tray = self.tray()
        with (mock.patch.object(notifier_module, 'send_request', side_effect=ValueError('Open the lid')),
              mock.patch.object(notifier_module.GLib, 'idle_add', side_effect=lambda fn, *args: fn(*args))):
            tray._lid_toggle_worker(True, 1)
        tray._show_error.assert_called_with('Lid Setting Failed', 'Open the lid')
        tray.lid_item.set_sensitive.assert_called_with(True)


class LidEvidenceDisplayTests(unittest.TestCase):
    def test_deadline_shortener_uses_structured_evidence_and_previous_decision(self):
        self.assertEqual(notifier_module.lid_deadline_cause({
            "deadline_shortened_by": ["SUSTAINED_HIGH_CPU", "ON_BATTERY", "ON_BATTERY"]}),
            "battery power + sustained CPU load")
        self.assertEqual(notifier_module.lid_deadline_cause({
            "last_decision": {"deadline_shortened_by": ["ON_BATTERY"]}}),
            "battery power")
        self.assertEqual(notifier_module.lid_deadline_cause({"deadline_shortened_by": None}), "")
        self.assertEqual(notifier_module.lid_deadline_cause({}), "")

    def test_timed_toggle_description_is_explicit_about_auto_suspend(self):
        self.assertIn("2–4 min", notifier_module.TIMED_LID_LABEL)
        self.assertIn("auto-suspend", notifier_module.TIMED_LID_LABEL)


class CursorSettingsTests(unittest.TestCase):
    def test_defaults_when_file_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor-settings.json"
            with mock.patch.object(notifier_module, "CURSOR_SETTINGS_PATH", path):
                self.assertEqual(
                    TrayNotifier._load_cursor_settings(),
                    {"enabled": False, "seconds": 60},
                )

    def test_settings_are_saved_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested/cursor-settings.json"
            with mock.patch.object(notifier_module, "CURSOR_SETTINGS_PATH", path):
                TrayNotifier._save_cursor_settings(True, 125)
                self.assertEqual(
                    TrayNotifier._load_cursor_settings(),
                    {"enabled": True, "seconds": 125},
                )
                self.assertEqual(json.loads(path.read_text())["seconds"], 125)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_settings_fall_back_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor-settings.json"
            path.write_text('{"enabled": true, "seconds": 0}')
            with mock.patch.object(notifier_module, "CURSOR_SETTINGS_PATH", path):
                self.assertEqual(
                    TrayNotifier._load_cursor_settings(),
                    {"enabled": False, "seconds": 60},
                )


class StartupTests(unittest.TestCase):
    def test_missing_display_exits_cleanly_for_systemd_retry(self) -> None:
        with mock.patch.object(
            notifier_module.Gtk, "init_check", return_value=(False, [])
        ):
            with mock.patch.object(notifier_module, "TrayNotifier") as tray:
                self.assertEqual(notifier_module.main(), 75)
                tray.assert_not_called()

    def test_ready_display_starts_tray(self) -> None:
        with mock.patch.object(
            notifier_module.Gtk, "init_check", return_value=(True, [])
        ):
            with mock.patch.object(notifier_module, "TrayNotifier") as tray:
                tray.return_value.run.return_value = 0
                self.assertEqual(notifier_module.main(), 0)
                tray.return_value.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

class ExplicitActionTests(unittest.TestCase):
    def test_queued_unlock_does_not_change_into_lock_after_state_change(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        tray.current_state = {'locked_kinds': []}
        tray.config = mock.Mock()
        with mock.patch.object(notifier_module, 'send_request', return_value={'ok':True}) as send:
            tray._toggle_worker(True)
        self.assertEqual(send.call_args.args[0], {'cmd':'emergency-unlock'})

    def test_lid_worker_uses_captured_generation(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        tray.current_state = {'lid_generation': 999}
        tray.config = mock.Mock()
        with mock.patch.object(notifier_module, 'send_request', return_value={'ok':True}) as send:
            tray._lid_toggle_worker(True, 123)
        self.assertEqual(send.call_args.args[0]['generation'],123)
