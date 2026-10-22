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
        # A disconnect while lid-ignore is on now raises a critical
        # notification (A8); never pop real desktop notifications in tests.
        tray._notify = mock.Mock()
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


class ContinuousLidDisplayTests(unittest.TestCase):
    def test_checkbox_explicitly_advertises_continuous_mode(self):
        self.assertIn("continuous", notifier_module.CONTINUOUS_LID_LABEL)
        self.assertIn("safety-monitored", notifier_module.CONTINUOUS_LID_LABEL)
        self.assertNotIn("2–4", notifier_module.CONTINUOUS_LID_LABEL)

    def test_closed_lid_has_no_countdown_in_tray_status(self):
        tray = DisconnectTests().tray()
        tray.current_state.update({
            "safety": {"state": "LID_CLOSED_MONITORED", "thermal": "NORMAL",
                       "thermal_protection": "ARMED", "deadline": None},
            "ignore_lid_close": True,
            "lid_policy_healthy": True,
        })
        tray.updating_lid_item = False
        tray._update_menu(frozenset(), "CTRL+ALT+Z")
        self.assertIn("continuous", tray.safety_item.set_label.call_args.args[0])
        self.assertNotIn("remaining", tray.safety_item.set_label.call_args.args[0])
        tray.lid_item.set_label.assert_called_with(notifier_module.CONTINUOUS_LID_LABEL)


class GnomeIdleGuardTests(unittest.TestCase):
    def fake(self):
        proxy = mock.Mock()
        proxy.get_name_owner.return_value = ":1.321"
        proxy.call_sync.return_value.unpack.return_value = (47,)
        return proxy

    def test_idle_only_guard_uses_flag_eight_once_and_releases(self):
        proxy = self.fake()
        guard = notifier_module.GnomeIdleGuard(proxy_factory=lambda: proxy)
        guard.acquire()
        guard.acquire()
        self.assertEqual(proxy.call_sync.call_count, 1)
        call = proxy.call_sync.call_args
        self.assertEqual(call.args[0], "Inhibit")
        self.assertEqual(call.args[1].get_type_string(), "(susu)")
        self.assertEqual(call.args[1].unpack()[3], 8)
        self.assertNotEqual(call.args[1].unpack()[3], 4)
        self.assertEqual(guard.cookie, 47)
        guard.release()
        self.assertEqual(proxy.call_sync.call_count, 2)
        call = proxy.call_sync.call_args
        self.assertEqual(call.args[0], "Uninhibit")
        self.assertEqual(call.args[1].unpack(), (47,))
        guard.release()
        self.assertEqual(proxy.call_sync.call_count, 2)

    def test_uninhibit_timeout_closes_private_bus_and_discards_cookie(self):
        proxy = self.fake()
        guard = notifier_module.GnomeIdleGuard(proxy_factory=lambda: proxy)
        guard.acquire()
        proxy.call_sync.side_effect = OSError("timeout")
        guard.release()
        self.assertIsNone(guard.cookie)
        self.assertIsNone(guard.proxy)
        proxy.get_connection.return_value.close_sync.assert_called_once_with(None)
        guard.release()
        self.assertEqual(proxy.call_sync.call_count, 2)

    def test_lost_inhibit_reply_closes_bus_before_retry(self):
        first, second = self.fake(), self.fake()
        first.call_sync.side_effect = TimeoutError("lost reply")
        factory = mock.Mock(side_effect=[first, second])
        guard = notifier_module.GnomeIdleGuard(proxy_factory=factory)
        with self.assertRaises(TimeoutError):
            guard.acquire()
        first.get_connection.return_value.close_sync.assert_called_once_with(None)
        self.assertIsNone(guard.cookie)
        self.assertIsNone(guard.proxy)
        guard.acquire()
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(guard.cookie, 47)
        guard.release()
        second.get_connection.return_value.close_sync.assert_called_once_with(None)

    def test_session_manager_restart_reacquires_on_new_owner(self):
        proxy = self.fake()
        proxy.get_name_owner.side_effect = [":1.321", ":1.322"]
        guard = notifier_module.GnomeIdleGuard(proxy_factory=lambda: proxy)
        guard.acquire()
        guard.acquire()
        self.assertEqual(proxy.call_sync.call_count, 2)
        self.assertEqual(guard.owner, ":1.322")

    def test_absent_gnome_session_fails_closed_without_cookie(self):
        proxy = self.fake()
        proxy.get_name_owner.return_value = None
        guard = notifier_module.GnomeIdleGuard(proxy_factory=lambda: proxy)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            guard.acquire()
        self.assertIsNone(guard.cookie)
        proxy.call_sync.assert_not_called()
        guard.release()

    def test_failed_acquisition_disables_lid_override_once_per_generation(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        tray.idle_guard = mock.Mock()
        tray.idle_guard.acquire.side_effect = RuntimeError("session bus unavailable")
        tray.idle_guard_failed_generation = None
        tray._notify = mock.Mock()
        tray._lid_toggle_worker = mock.Mock()
        state = {"ignore_lid_close": True, "lid_generation": 55, "locked_kinds": []}
        with mock.patch.object(notifier_module.threading, "Thread") as worker:
            tray._reconcile_idle_guard(state)
            tray._reconcile_idle_guard(state)
        worker.assert_called_once()
        worker.return_value.start.assert_called_once()
        self.assertEqual(worker.call_args.kwargs["args"], (55,))
        self.assertEqual(worker.call_args.kwargs["target"], tray._idle_guard_safety_disable)
        tray._notify.assert_called_once()
        tray._reconcile_idle_guard({"ignore_lid_close": False})
        # Two failed lease attempts and an explicit off transition each
        # release the GNOME cookie idempotently.
        self.assertEqual(tray.idle_guard.release.call_count, 3)

    def test_notification_failure_does_not_prevent_immediate_idle_safety_disable(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        tray.idle_guard = mock.Mock()
        tray.idle_guard.acquire.side_effect = RuntimeError("session bus unavailable")
        tray.idle_guard_failed_generation = None
        tray._notify = mock.Mock(side_effect=RuntimeError("notification broken"))
        tray._idle_guard_safety_disable = mock.Mock()
        with mock.patch.object(notifier_module.threading, "Thread") as worker:
            tray._reconcile_idle_guard({
                "ignore_lid_close": True, "lid_generation": 59, "locked_kinds": []})
        worker.assert_called_once()
        worker.return_value.start.assert_called_once()
        self.assertEqual(worker.call_args.kwargs["args"], (59,))

    def test_failed_guarded_disable_releases_controller_for_native_failsafe(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        tray.config = mock.Mock()
        tray.subscription_socket = mock.Mock()
        tray._action_error = mock.Mock(return_value=False)
        with (mock.patch.object(notifier_module, "send_request",
                                side_effect=RuntimeError("daemon disconnected")),
              mock.patch.object(notifier_module.GLib, "idle_add") as queue):
            tray._idle_guard_safety_disable(99)
        tray.subscription_socket.shutdown.assert_called_once_with(notifier_module.socket.SHUT_RDWR)
        queue.assert_called_once()
        self.assertEqual(queue.call_args.args[1], "Lid Safety")

    def test_guarded_disable_sends_only_off_and_keeps_controller_on_success(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        tray.config = mock.Mock()
        tray.subscription_socket = mock.Mock()
        with mock.patch.object(notifier_module, "send_request", return_value={"ok": True}) as send:
            tray._idle_guard_safety_disable(57)
        self.assertEqual(send.call_args.args[0],
                         {"cmd": "set-lid-ignore", "enabled": False, "generation": 57})
        tray.subscription_socket.shutdown.assert_not_called()

    def test_successful_reconcile_keeps_manual_system_suspend_unblocked(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        tray.idle_guard = mock.Mock()
        tray.idle_guard_failed_generation = None
        tray._notify = mock.Mock()
        tray._reconcile_idle_guard({"ignore_lid_close": True, "lid_generation": 1})
        tray.idle_guard.acquire.assert_called_once()
        tray._notify.assert_not_called()


class CoolingArmAdmissionTests(unittest.TestCase):
    def fake(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        tray.updating_lid_item = False
        tray.current_state = {"lid_generation": 19, "ignore_lid_close": False}
        tray.power_guard = mock.Mock()
        tray._show_error = mock.Mock(return_value=False)
        return tray

    # U4: the cooling hold and the enable request now run in order on the
    # lease worker (synchronously here, because lease_worker is None).
    def test_arm_preacquires_cooling_before_starting_daemon_request(self):
        tray = self.fake()
        tray.config = mock.Mock()
        tray.lid_item = mock.Mock()
        item = mock.Mock()
        item.get_active.return_value = True
        actions = []
        tray.power_guard.acquire.side_effect = lambda: actions.append("power-saver")
        dialog = mock.Mock()
        dialog.run.return_value = notifier_module.Gtk.ResponseType.YES
        with (mock.patch.object(notifier_module.Gtk, "MessageDialog", return_value=dialog),
              mock.patch.object(notifier_module, "send_request",
                                side_effect=lambda *a, **k: actions.append("daemon-request") or {"ok": True}),
              mock.patch.object(notifier_module.GLib, "timeout_add_seconds")):
            tray._on_lid_toggled(item)
        self.assertEqual(actions, ["power-saver", "daemon-request"])
        tray._show_error.assert_not_called()
        self.assertFalse(tray.enable_in_flight)

    def test_cooling_failure_refuses_arm_before_root_lid_inhibitor(self):
        tray = self.fake()
        tray.config = mock.Mock()
        tray.lid_item = mock.Mock()
        tray.connected = True
        tray.current_state = {"lid_generation": 19, "ignore_lid_close": False, "lid_switch_devices": 1}
        tray.power_guard.acquire.side_effect = RuntimeError("power daemon absent")
        item = mock.Mock()
        item.get_active.return_value = True
        dialog = mock.Mock()
        dialog.run.return_value = notifier_module.Gtk.ResponseType.YES
        with (mock.patch.object(notifier_module.Gtk, "MessageDialog", return_value=dialog),
              mock.patch.object(notifier_module, "send_request") as send):
            tray._on_lid_toggled(item)
        send.assert_not_called()
        tray.lid_item.set_active.assert_called_with(False)
        tray._show_error.assert_called_once()
        self.assertIn("power-saver", tray._show_error.call_args.args[1])

class PowerSaverGuardTests(unittest.TestCase):
    def fake(self):
        proxy = mock.Mock()
        proxy.get_name_owner.return_value = ":1.456"
        proxy.call_sync.return_value.unpack.return_value = (77,)
        return proxy

    def test_hold_is_power_saver_only_and_releases_on_user_disable(self):
        proxy = self.fake()
        guard = notifier_module.PowerSaverGuard(proxy_factory=lambda: proxy)
        with mock.patch.object(guard, "_active_profile", return_value="power-saver"):
            guard.acquire()
            guard.acquire()
        proxy.call_sync.assert_called_once()
        call = proxy.call_sync.call_args
        self.assertEqual(call.args[0], "HoldProfile")
        self.assertEqual(call.args[1].get_type_string(), "(sss)")
        self.assertEqual(call.args[1].unpack()[0], "power-saver")
        self.assertEqual(guard.cookie, 77)
        guard.release()
        self.assertEqual(proxy.call_sync.call_count, 2)
        self.assertEqual(proxy.call_sync.call_args.args[0], "ReleaseProfile")
        self.assertEqual(proxy.call_sync.call_args.args[1].unpack(), (77,))
        self.assertIsNone(guard.proxy)
        proxy.get_connection.return_value.close_sync.assert_called_once_with(None)
        guard.release()
        self.assertEqual(proxy.call_sync.call_count, 2)

    def test_ineffective_profile_fails_closed_and_drops_system_bus(self):
        # A7: one reconnect + fresh hold is tried before failing closed.
        proxy = self.fake()
        guard = notifier_module.PowerSaverGuard(proxy_factory=lambda: proxy, sleep=lambda _s: None)
        with mock.patch.object(guard, "_active_profile", return_value="balanced"):
            with self.assertRaisesRegex(RuntimeError, "did not become effective.*balanced"):
                guard.acquire()
        self.assertEqual(proxy.get_connection.return_value.close_sync.call_args_list,
                         [mock.call(None), mock.call(None)])
        self.assertIsNone(guard.cookie)
        self.assertIsNone(guard.proxy)

    def test_lost_reply_closes_private_bus_and_retry_acquires_clean_cookie(self):
        first, second = self.fake(), self.fake()
        first.call_sync.side_effect = TimeoutError("unknown acceptance")
        factory = mock.Mock(side_effect=[first, second])
        guard = notifier_module.PowerSaverGuard(proxy_factory=factory)
        with self.assertRaises(TimeoutError):
            guard.acquire()
        first.get_connection.return_value.close_sync.assert_called_once_with(None)
        with mock.patch.object(guard, "_active_profile", return_value="power-saver"):
            guard.acquire()
        self.assertEqual(guard.cookie, 77)
        self.assertEqual(factory.call_count, 2)
        guard.release()
        second.get_connection.return_value.close_sync.assert_called_once_with(None)

    def test_restarted_power_daemon_reacquires_cookie_on_fresh_connection(self):
        first, second = self.fake(), self.fake()
        first.get_name_owner.side_effect = [":1.old", ":1.new"]
        second.get_name_owner.return_value = ":1.new"
        factory = mock.Mock(side_effect=[first, second])
        guard = notifier_module.PowerSaverGuard(proxy_factory=factory)
        with mock.patch.object(guard, "_active_profile", return_value="power-saver"):
            guard.acquire()
            guard.acquire()
        first.get_connection.return_value.close_sync.assert_called_once_with(None)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(guard.owner, ":1.new")
        guard.release()

    def test_missing_power_daemon_refuses_guard_before_any_hold(self):
        proxy = self.fake()
        proxy.get_name_owner.return_value = None
        guard = notifier_module.PowerSaverGuard(proxy_factory=lambda: proxy)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            guard.acquire()
        proxy.call_sync.assert_not_called()
        proxy.get_connection.return_value.close_sync.assert_called_once_with(None)

    def test_reconcile_acquires_cooling_first_then_idle_and_releases_both(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        calls = []
        tray.power_guard = mock.Mock()
        tray.idle_guard = mock.Mock()
        tray.power_guard.acquire.side_effect = lambda: calls.append("power")
        tray.idle_guard.acquire.side_effect = lambda: calls.append("idle")
        tray.idle_guard_failed_generation = None
        tray._reconcile_idle_guard({"ignore_lid_close": True, "lid_generation": 7})
        self.assertEqual(calls, ["power", "idle"])
        tray._reconcile_idle_guard({"ignore_lid_close": False})
        tray.power_guard.release.assert_called_once()
        tray.idle_guard.release.assert_called_once()

    def test_cooling_hold_failure_prevents_idle_and_disables_lid_override(self):
        tray = TrayNotifier.__new__(TrayNotifier)
        tray.power_guard = mock.Mock()
        tray.power_guard.acquire.side_effect = RuntimeError("power policy unavailable")
        tray.idle_guard = mock.Mock()
        tray.idle_guard_failed_generation = None
        tray._notify = mock.Mock()
        tray._idle_guard_safety_disable = mock.Mock()
        with mock.patch.object(notifier_module.threading, "Thread") as worker:
            tray._reconcile_idle_guard({
                "ignore_lid_close": True, "lid_generation": 45, "locked_kinds": []})
        tray.power_guard.release.assert_called_once()
        tray.idle_guard.release.assert_called_once()
        tray.idle_guard.acquire.assert_not_called()
        worker.assert_called_once()
        self.assertEqual(worker.call_args.kwargs["args"], (45,))
        worker.return_value.start.assert_called_once()


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
