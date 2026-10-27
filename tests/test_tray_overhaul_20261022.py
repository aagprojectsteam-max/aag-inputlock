#!/usr/bin/python3
"""Tray overhaul 2026-10-22: A7, A8, U1-U6, U8, G1 (agent C)."""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import input_lock_notifier as notifier  # noqa: E402
from input_lock_notifier import PowerSaverGuard, TrayNotifier  # noqa: E402
from gtk_fakes import FakeLabel  # noqa: E402


_DIALOG_GUARD = None


def setUpModule():
    # Never show (and block on) a real GTK dialog on the developer's desktop:
    # tests must patch the dialogs they exercise.
    global _DIALOG_GUARD

    def refuse(*_args, **_kwargs):
        raise AssertionError("a real GTK dialog was run during a test")

    _DIALOG_GUARD = mock.patch.object(notifier.Gtk.Dialog, "run", refuse)
    _DIALOG_GUARD.start()
    # The lid confirmation builds real labels; CI has no display (GTK aborts).
    global _LABEL_GUARD
    _LABEL_GUARD = mock.patch.object(notifier.Gtk, "Label", FakeLabel)
    _LABEL_GUARD.start()


def tearDownModule():
    _LABEL_GUARD.stop()
    _DIALOG_GUARD.stop()


def fake_proxy(cookies=(77, 78, 79)):
    proxy = mock.Mock()
    proxy.get_name_owner.return_value = ":1.456"
    proxy.call_sync.return_value.unpack.side_effect = [(cookie,) for cookie in cookies]
    return proxy


def hold_calls(proxy):
    return [call for call in proxy.call_sync.call_args_list if call.args[0] == "HoldProfile"]


class PowerSaverGuardRetryTests(unittest.TestCase):
    def test_single_mismatch_reconnects_reholds_and_rechecks_after_a_second(self):
        first, second = fake_proxy((77,)), fake_proxy((88,))
        factory = mock.Mock(side_effect=[first, second])
        sleeps = []
        guard = PowerSaverGuard(proxy_factory=factory, sleep=sleeps.append)
        with mock.patch.object(guard, "_active_profile", side_effect=["performance", "power-saver"]):
            restored = guard.acquire()
        self.assertTrue(restored)
        self.assertEqual(sleeps, [PowerSaverGuard.RETRY_SETTLE_SECONDS])
        self.assertEqual(len(hold_calls(first)), 1)
        self.assertEqual(len(hold_calls(second)), 1)
        first.get_connection.return_value.close_sync.assert_called_once_with(None)
        self.assertEqual(guard.cookie, 88)
        self.assertIs(guard.proxy, second)

    def test_second_mismatch_fails_closed_naming_the_observed_profile(self):
        guard = PowerSaverGuard(proxy_factory=lambda: fake_proxy(), sleep=lambda _s: None)
        with mock.patch.object(guard, "_active_profile", side_effect=["balanced", "performance"]):
            with self.assertRaisesRegex(RuntimeError, r"after one retry \(active profile: performance\)"):
                guard.acquire()
        self.assertIsNone(guard.cookie)
        self.assertIsNone(guard.proxy)

    def test_profile_released_marks_cookie_and_next_acquire_takes_a_fresh_hold(self):
        proxy = fake_proxy((77, 78))
        guard = PowerSaverGuard(proxy_factory=lambda: proxy, sleep=lambda _s: None)
        callback = mock.Mock()
        guard.on_released = callback
        with mock.patch.object(guard, "_active_profile", return_value="power-saver"):
            self.assertFalse(guard.acquire())
            self.assertFalse(guard.profile_released(12345))  # not ours
            callback.assert_not_called()
            self.assertTrue(guard.profile_released(77))
            callback.assert_called_once_with()
            self.assertTrue(guard.acquire())  # restored
            self.assertFalse(guard.acquire())  # steady state again
        self.assertEqual(len(hold_calls(proxy)), 2)
        self.assertEqual(guard.cookie, 78)
        # The released cookie must never be "released" again.
        releases = [c for c in proxy.call_sync.call_args_list if c.args[0] == "ReleaseProfile"]
        self.assertEqual(releases, [])

    def test_release_skips_cookie_already_dropped_by_ppd(self):
        proxy = fake_proxy((77,))
        guard = PowerSaverGuard(proxy_factory=lambda: proxy)
        with mock.patch.object(guard, "_active_profile", return_value="power-saver"):
            guard.acquire()
        guard.profile_released(77)
        guard.release()
        releases = [c for c in proxy.call_sync.call_args_list if c.args[0] == "ReleaseProfile"]
        self.assertEqual(releases, [])
        proxy.get_connection.return_value.close_sync.assert_called_once_with(None)

    def test_subscribes_to_profile_released_and_unsubscribes_on_release(self):
        proxy = fake_proxy((77,))
        connection = proxy.get_connection.return_value
        connection.signal_subscribe.return_value = 5
        guard = PowerSaverGuard(proxy_factory=lambda: proxy)
        with mock.patch.object(guard, "_active_profile", return_value="power-saver"):
            guard.acquire()
        args = connection.signal_subscribe.call_args.args
        self.assertEqual(args[2], "ProfileReleased")
        self.assertEqual(args[3], "/net/hadess/PowerProfiles")
        signal_callback = args[6]
        guard.on_released = mock.Mock()
        signal_callback(connection, ":1.9", "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles",
                        "ProfileReleased", notifier.GLib.Variant("(u)", (77,)))
        guard.on_released.assert_called_once_with()
        guard.release()
        connection.signal_unsubscribe.assert_called_once_with(5)


def bare_tray(**state):
    tray = TrayNotifier.__new__(TrayNotifier)
    tray.config = mock.Mock()
    tray.current_state = dict(state)
    tray.previous_locked = None
    for name in ("status_item", "safety_item", "safety_detail_item", "cooling_item",
                 "toggle_item", "lid_item", "indicator", "shortcut_item"):
        setattr(tray, name, mock.Mock())
    tray._notify = mock.Mock()
    tray._show_error = mock.Mock(return_value=False)
    tray.idle_guard = mock.Mock()
    tray.power_guard = mock.Mock()
    tray.power_guard.acquire.return_value = False
    return tray


ARMED = {"ignore_lid_close": True, "lid_generation": 4, "lid_switch_devices": 1,
         "lid_policy_healthy": True, "locked_kinds": [], "lid_is_closed": False}


def disarmed(**extra):
    state = dict(ARMED, ignore_lid_close=False, lid_generation=5, lid_policy_healthy=False)
    state.update(extra)
    return state


def event(state, reason="state-changed"):
    return {"type": "event", "event": "state-changed", "reason": reason, "state": state}


class PowerModeChangeTests(unittest.TestCase):
    def test_manual_power_mode_change_reholds_and_notifies_keeping_lid_ignore(self):
        tray = bare_tray(**ARMED)
        tray.connected = True
        tray.power_guard.acquire.return_value = True  # had to re-take the hold
        with mock.patch.object(notifier, "send_request") as send:
            tray._on_power_hold_released()
        tray.power_guard.acquire.assert_called_once_with()
        tray.idle_guard.acquire.assert_called_once_with()
        send.assert_not_called()  # lid-ignore kept
        tray._notify.assert_called_once()
        self.assertEqual(tray._notify.call_args.args[1], notifier.POWER_RESTORED_BODY)
        self.assertTrue(tray.cooling_verified)

    def test_release_signal_ignored_when_lid_ignore_off(self):
        tray = bare_tray(**disarmed())
        tray._on_power_hold_released()
        tray.power_guard.acquire.assert_not_called()

    def test_ineffective_fresh_hold_fails_closed_with_reason_and_keeps_idle_for_grace(self):
        tray = bare_tray(**ARMED)
        tray.connected = True
        tray.idle_held = True
        tray.power_guard.acquire.side_effect = RuntimeError(
            "Power-saver hold did not become effective after one retry (active profile: balanced)")
        with (mock.patch.object(notifier.threading, "Thread") as thread,
              mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=91) as timer):
            tray._reconcile_idle_guard(tray.current_state)
        tray.power_guard.release.assert_called_once_with()
        tray.idle_guard.release.assert_not_called()  # U3 grace
        timer.assert_called_once()
        self.assertEqual(timer.call_args.args[0], notifier.IDLE_GRACE_SECONDS)
        self.assertEqual(thread.call_args.kwargs["target"], tray._idle_guard_safety_disable)
        self.assertEqual(thread.call_args.kwargs["args"], (4,))
        kwargs = tray._notify.call_args.kwargs
        self.assertTrue(kwargs["critical"])
        self.assertTrue(kwargs["reenable"])
        self.assertIn("balanced", tray._notify.call_args.args[1])
        self.assertEqual(tray.own_disable_reason, "power-saver-ineffective")
        # The daemon then reports the tray's own "off": no duplicate notice.
        tray._handle_message(event(disarmed(last_lid_disable={
            "reason": "lid-ignore-disabled", "requested_by_user": True, "at": 1.0})))
        self.assertEqual(tray._notify.call_count, 1)
        tray.idle_guard.release.assert_not_called()  # still within grace
        tray._idle_grace_expired()
        tray.idle_guard.release.assert_called_once_with()


class LidLossNotificationTests(unittest.TestCase):
    def armed_tray(self):
        tray = bare_tray(**ARMED)
        tray.connected = True
        tray.cooling_verified = True
        return tray

    def test_unrequested_loss_is_critical_persistent_with_reason_and_never_rearms(self):
        tray = self.armed_tray()
        state = disarmed(last_lid_disable={"reason": "controller-lease-expired",
                                           "requested_by_user": False, "at": 1.0})
        with (mock.patch.object(notifier, "send_request") as send,
              mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=7)):
            tray._handle_message(event(state, "controller-lease-expired"))
        send.assert_not_called()
        tray._notify.assert_called_once()
        title, body = tray._notify.call_args.args[:2]
        self.assertEqual(title, notifier.LID_LOST_TITLE)
        self.assertIn("did not check in", body)
        self.assertIn("Re-enable", body)
        self.assertTrue(tray._notify.call_args.kwargs["critical"])
        self.assertTrue(tray._notify.call_args.kwargs["reenable"])
        tray.lid_item.set_active.assert_called_with(False)
        self.assertFalse(tray.cooling_verified)
        # Grace: cooling released, idle inhibitor kept for now.
        tray.power_guard.release.assert_called_once_with()
        tray.idle_guard.release.assert_not_called()

    def test_user_requested_disable_is_silent_and_releases_both(self):
        tray = self.armed_tray()
        tray._handle_message(event(disarmed(last_lid_disable={
            "reason": "lid-ignore-disabled", "requested_by_user": True, "at": 1.0})))
        tray._notify.assert_not_called()
        tray.power_guard.release.assert_called_once_with()
        tray.idle_guard.release.assert_called_once_with()

    def test_older_daemon_without_last_lid_disable_uses_event_reason(self):
        tray = self.armed_tray()
        tray._handle_message(event(disarmed(), "lid-ignore-disabled"))
        tray._notify.assert_not_called()
        tray = self.armed_tray()
        with mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=7):
            tray._handle_message(event(disarmed(), "session-inactive"))
        self.assertIn("session", tray._notify.call_args.args[1])

    def test_closed_lid_loss_releases_idle_at_once(self):
        tray = self.armed_tray()
        with mock.patch.object(notifier.GLib, "timeout_add_seconds") as timer:
            tray._handle_message(event(disarmed(lid_is_closed=True), "safety-protection"))
        timer.assert_not_called()
        tray.idle_guard.release.assert_called_once_with()
        tray._notify.assert_called_once()

    def test_safety_pending_does_not_add_a_second_generic_notice(self):
        tray = self.armed_tray()
        tray.current_state = dict(ARMED, safety={"state": "LID_IGNORE_ARMED"})
        state = disarmed(safety={"state": "PROTECTIVE_ACTION_PENDING",
                                 "last_decision": {"reason": "THERMAL_EMERGENCY"}})
        with mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=7):
            tray._handle_message(event(state, "safety-protection"))
        tray._notify.assert_called_once()
        self.assertIn("dangerous temperature", tray._notify.call_args.args[1])

    def test_tray_unlock_action_is_informational_not_critical(self):
        tray = self.armed_tray()
        tray._mark_tray_unlock()
        tray._handle_message(event(disarmed(last_lid_disable={
            "reason": "ipc-emergency-unlock", "requested_by_user": False}), "ipc-emergency-unlock"))
        tray._notify.assert_called_once()
        self.assertNotIn("critical", tray._notify.call_args.kwargs)
        self.assertEqual(tray._notify.call_args.args[1], notifier.TRAY_UNLOCK_LID_NOTE)
        tray.idle_guard.release.assert_called_once_with()
        # A later emergency unlock from elsewhere is critical again.
        tray = self.armed_tray()
        with mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=7):
            tray._handle_message(event(disarmed(), "ipc-emergency-unlock"))
        self.assertTrue(tray._notify.call_args.kwargs["critical"])

    def test_resume_loss_is_covered_after_reconnect_free_event(self):
        # A4: after thaw the daemon reports lease expiry; same notification.
        tray = self.armed_tray()
        with mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=7):
            tray._handle_message(event(disarmed(last_lid_disable={
                "reason": "controller-lease-expired", "requested_by_user": False})))
        tray._notify.assert_called_once()


class ReasonMappingTests(unittest.TestCase):
    def test_known_reasons_have_plain_words(self):
        cases = {
            "controller-lease-expired": "did not check in",
            "controller-disconnected": "connection",
            "session-inactive": "session",
            "authorized-session-inactive": "session",
            "daemon-shutdown": "stopped or restarted",
            "daemon-restarted": "restarted",
            "daemon-unavailable": "connection",
            "ipc-emergency-unlock": "emergency",
            "user-session-ended": "tray was stopped",
        }
        for reason, words in cases.items():
            with self.subTest(reason=reason):
                text = notifier.describe_lid_disable(reason)
                self.assertIn(words, text)
                self.assertNotIn(reason, text)

    def test_safety_reasons_from_decision_and_from_reason_text(self):
        state = {"safety": {"last_decision": {"reason": "BATTERY_CRITICAL"}}}
        self.assertIn("battery is critically low",
                      notifier.describe_lid_disable("safety-protection", state))
        self.assertIn("dangerous temperature", notifier.describe_lid_disable("THERMAL_EMERGENCY"))
        self.assertIn("readings", notifier.describe_lid_disable("supervision:TELEMETRY_LOST"))
        self.assertIn("heat", notifier.describe_lid_disable("thermal-supervision-high"))
        self.assertIn("battery", notifier.describe_lid_disable("battery-low-supervision"))
        self.assertIn("sleep", notifier.describe_lid_disable("prepare-sleep-timeout"))

    def test_unknown_reason_is_named_and_detail_appended(self):
        text = notifier.describe_lid_disable("something-new", detail="x=1")
        self.assertIn("something-new", text)
        self.assertTrue(text.endswith("Details: x=1"))
        self.assertIn("without giving a reason", notifier.describe_lid_disable(""))


class ReenableActionTests(unittest.TestCase):
    def test_reenable_goes_through_the_lid_item_and_its_dialog(self):
        tray = bare_tray(**disarmed())
        tray.connected = True
        tray._offer_reenable()
        tray.lid_item.set_active.assert_called_once_with(True)
        tray._show_error.assert_not_called()

    def test_reenable_requires_an_open_lid(self):
        tray = bare_tray(**disarmed(lid_is_closed=True))
        tray.connected = True
        tray._offer_reenable()
        tray.lid_item.set_active.assert_not_called()
        self.assertIn("Open the lid", tray._show_error.call_args.args[1])

    def test_reenable_while_disconnected_or_already_on_does_nothing_risky(self):
        tray = bare_tray(**disarmed())
        tray.connected = False
        tray._offer_reenable()
        tray.lid_item.set_active.assert_not_called()
        tray._show_error.assert_called_once()
        tray = bare_tray(**ARMED)
        tray.connected = True
        tray._offer_reenable()
        tray.lid_item.set_active.assert_not_called()

    def test_closed_lid_refuses_enable_after_dialog(self):
        tray = bare_tray(**disarmed(lid_is_closed=True))
        tray.updating_lid_item = False
        item = mock.Mock()
        item.get_active.return_value = True
        dialog = mock.Mock()
        dialog.run.return_value = notifier.Gtk.ResponseType.YES
        with (mock.patch.object(notifier.Gtk, "MessageDialog", return_value=dialog),
              mock.patch.object(notifier, "send_request") as send):
            tray._on_lid_toggled(item)
        send.assert_not_called()
        tray.power_guard.acquire.assert_not_called()
        item.set_active.assert_called_with(False)

    def test_confirmation_dialog_documents_that_stopping_the_tray_ends_lid_ignore(self):
        tray = bare_tray(**disarmed())
        item = mock.Mock()
        item.get_active.return_value = True
        dialog = mock.Mock()
        dialog.run.return_value = notifier.Gtk.ResponseType.NO
        with mock.patch.object(notifier.Gtk, "MessageDialog", return_value=dialog):
            tray._on_lid_toggled(item)
        packed = [call.args[0] for call in dialog.get_message_area().pack_start.call_args_list]
        texts = [label.get_text() for label in packed]
        self.assertTrue(any(text.endswith(notifier.TRAY_STOP_NOTE) for text in texts), texts)
        self.assertTrue(any(notifier.LID_CONFIRM_WARNING in text for text in texts), texts)
        for label in packed:
            # An explicit width stops GTK from shaping a narrow column.
            self.assertGreaterEqual(label.get_width_chars(), 40)
            self.assertTrue(label.get_line_wrap())
        dialog.add_button.assert_any_call("Turn On", notifier.Gtk.ResponseType.YES)
        self.assertIn("tray", notifier.LID_TOOLTIP)

    def test_notification_command_is_critical_persistent_with_action(self):
        command = TrayNotifier._notify_command("/usr/bin/notify-send", "T", "B", False, True, True)
        self.assertIn("--urgency=critical", command)
        self.assertIn("--expire-time=0", command)
        self.assertIn("--action=reenable=" + notifier.REENABLE_ACTION_LABEL, command)
        normal = TrayNotifier._notify_command("/usr/bin/notify-send", "T", "B", False, False, False)
        self.assertIn("--urgency=normal", normal)

    def test_action_reply_schedules_reenable_on_main_thread(self):
        tray = bare_tray()
        result = mock.Mock(stdout="reenable\n")
        with (mock.patch.object(notifier.shutil, "which", side_effect=lambda name: "/usr/bin/" + name),
              mock.patch.object(notifier.subprocess, "run", return_value=result),
              mock.patch.object(notifier.GLib, "idle_add") as idle):
            tray.config.play_sound = False
            TrayNotifier._notify_blocking(tray, "T", "B", False, True, True)
        idle.assert_called_once_with(tray._offer_reenable)

    def test_notify_never_blocks_the_caller(self):
        tray = bare_tray()
        del tray._notify
        with mock.patch.object(notifier.threading, "Thread") as thread:
            tray._notify("T", "B", locked=False)
        self.assertEqual(thread.call_args.kwargs["target"], tray._notify_blocking)
        thread.return_value.start.assert_called_once_with()


class DisconnectedStateTests(unittest.TestCase):
    def test_disconnect_while_armed_unchecks_marks_stale_and_notifies_once(self):
        tray = bare_tray(**ARMED)
        tray.connected = True
        flags = []
        tray.lid_item.set_active.side_effect = lambda value: flags.append((value, tray.updating_lid_item))
        with mock.patch.object(notifier.GLib, "timeout_add_seconds", return_value=3):
            tray._show_disconnected()
            tray._show_disconnected()
        self.assertEqual(flags[-1], (False, True))
        self.assertFalse(tray.connected)
        self.assertTrue(tray.state_stale)
        self.assertIs(tray.current_state["ignore_lid_close"], False)
        self.assertTrue(tray.current_state["stale"])
        tray.indicator.set_icon_full.assert_called_with(notifier.DISCONNECTED_ICON,
                                                        "AAG - InputLock service unavailable")
        tray.lid_item.set_label.assert_called_with(notifier.LID_UNAVAILABLE_LABEL)
        tray._notify.assert_called_once()
        self.assertIn("connection", tray._notify.call_args.args[1])
        # Reconnecting with lid-ignore off does not notify again.
        tray._handle_message({"subscribed": True, "state": disarmed()})
        tray._notify.assert_called_once()

    def test_second_tray_instance_has_its_own_status(self):
        tray = bare_tray()
        tray._show_disconnected("another-tray")
        self.assertIn("Another tray", tray.status_item.set_label.call_args.args[0])

    def test_wording_is_consistent(self):
        for label in (notifier.CONTINUOUS_LID_LABEL, notifier.LID_UNAVAILABLE_LABEL,
                      notifier.LID_SAFETY_UNAVAILABLE_LABEL):
            self.assertTrue(label.startswith(notifier.LID_LABEL))


class CoolingLabelTests(unittest.TestCase):
    def test_cooling_suffix_only_after_verified_hold(self):
        tray = bare_tray(**ARMED, safety={"state": "LID_IGNORE_ARMED"})
        tray.connected = True
        self.assertIn("Cooling: not confirmed", tray._safety_label())
        tray._lease_ok(4, False, False)
        self.assertIn("Cooling: active", tray._safety_label())
        tray.cooling_item.set_label.assert_called_with("Cooling: active")
        tray.cooling_item.show.assert_called()


class EnableInFlightTests(unittest.TestCase):
    def test_reconcile_never_releases_the_pre_enable_hold(self):
        tray = bare_tray(**disarmed())
        tray.connected = True
        tray.enable_in_flight = True
        tray._reconcile_idle_guard(tray.current_state)
        tray.power_guard.release.assert_not_called()
        tray.idle_guard.release.assert_not_called()
        tray._show_disconnected()
        tray.power_guard.release.assert_not_called()

    def test_failed_enable_releases_the_hold_and_clears_in_flight(self):
        tray = bare_tray(**disarmed())
        tray.connected = True
        tray.enable_in_flight = True
        with mock.patch.object(notifier, "send_request", side_effect=ValueError("stale lid request")):
            tray._enable_job(5)
        tray.power_guard.acquire.assert_called_once_with()
        # Released in the job and again by the U-R6 reconcile (idempotent).
        tray.power_guard.release.assert_called_with()
        tray.idle_guard.release.assert_called_once_with()
        self.assertFalse(tray.enable_in_flight)
        tray._show_error.assert_called_once()
        self.assertEqual(tray._show_error.call_args.args[1], "stale lid request")

    def test_enable_runs_on_the_lease_worker_not_the_gtk_thread(self):
        tray = bare_tray(**disarmed())
        tray.lease_worker = mock.Mock()
        tray.updating_lid_item = False
        item = mock.Mock()
        item.get_active.return_value = True
        dialog = mock.Mock()
        dialog.run.return_value = notifier.Gtk.ResponseType.YES
        with (mock.patch.object(notifier.Gtk, "MessageDialog", return_value=dialog),
              mock.patch.object(notifier, "send_request") as send):
            tray._on_lid_toggled(item)
            send.assert_not_called()
            tray.power_guard.acquire.assert_not_called()
            tray.lease_worker.submit.assert_called_once_with(tray._enable_job, 5)
            self.assertTrue(tray.enable_in_flight)
            item.set_sensitive.assert_called_with(False)
            # A second click while in flight is ignored.
            tray._on_lid_toggled(item)
            tray.lease_worker.submit.assert_called_once()

    def test_successful_enable_then_state_event_acquires(self):
        tray = bare_tray(**disarmed())
        tray.connected = True
        tray.enable_in_flight = True
        with (mock.patch.object(notifier, "send_request", return_value={"ok": True}),
              mock.patch.object(notifier.GLib, "timeout_add_seconds") as later):
            tray._enable_job(5)
        later.assert_called_once()
        self.assertFalse(tray.enable_in_flight)
        tray.power_guard.release.assert_not_called()
        tray._handle_message(event(dict(ARMED, lid_generation=6), "lid-ignore-enabled"))
        self.assertEqual(tray.power_guard.acquire.call_count, 2)
        tray.idle_guard.acquire.assert_called_once_with()

    def test_lease_checks_are_coalesced_on_the_worker(self):
        tray = bare_tray(**ARMED)
        tray.lease_worker = mock.Mock()
        tray._reconcile_idle_guard(tray.current_state)
        tray._reconcile_idle_guard(tray.current_state)
        tray.lease_worker.submit.assert_called_once()
        tray.power_guard.acquire.assert_not_called()


class SubscriptionWorkerTests(unittest.TestCase):
    def run_worker(self, side_effects):
        tray = bare_tray()
        tray.running = True
        waits = []
        effects = list(side_effects)

        def subscribe():
            effect = effects.pop(0)
            if not effects:
                tray.running = False
            if isinstance(effect, BaseException):
                raise effect

        tray._subscribe_once = subscribe
        tray._wait_before_retry = waits.append
        with (mock.patch.object(notifier.GLib, "idle_add") as idle,
              self.assertLogs("input-lock-tray", level="INFO") as logs):
            tray._subscription_worker()
        return tray, waits, idle, logs.output

    def test_unexpected_errors_never_kill_the_heartbeat_thread(self):
        tray, waits, idle, logs = self.run_worker([KeyError("bug"), OSError("refused"), None])
        self.assertEqual(len(waits), 2)
        self.assertEqual(idle.call_count, 1)  # disconnected UI once per outage
        self.assertTrue(any("unexpectedly" in line for line in logs))

    def test_down_state_is_logged_once_not_every_retry(self):
        tray, waits, idle, logs = self.run_worker([OSError("refused")] * 6 + [None])
        unavailable = [line for line in logs if "subscription unavailable" in line]
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(waits[:4], [1.0, 2.0, 4.0, 8.0])

    def test_second_instance_backs_off_and_logs_once(self):
        error = ValueError("a tray controller is already connected")
        tray, waits, idle, logs = self.run_worker([error, error, None])
        self.assertEqual(waits, [notifier.SECOND_INSTANCE_RETRY_SECONDS] * 2)
        self.assertEqual(len([line for line in logs if "Another AAG - InputLock tray" in line]), 1)
        self.assertEqual(idle.call_args.args[1], "another-tray")

    def test_reconnect_is_logged(self):
        tray = bare_tray()
        tray.down_since = notifier.time.monotonic() - 30
        with self.assertLogs("input-lock-tray", level="INFO") as logs:
            tray._subscription_up()
        self.assertIn("reconnected", logs.output[0])
        self.assertIsNone(tray.down_since)


class MiscTests(unittest.TestCase):
    def test_serial_worker_runs_jobs_in_order_and_survives_errors(self):
        worker = notifier.SerialWorker("test-worker")
        seen = []
        done = threading.Event()
        worker.submit(seen.append, 1)
        worker.submit(lambda: 1 / 0)
        worker.submit(seen.append, 2)
        worker.submit(done.set)
        self.assertTrue(done.wait(5))
        worker.stop()
        self.assertEqual(seen, [1, 2])

    def test_cursor_extension_status(self):
        uuid = notifier.CURSOR_EXTENSION_UUID

        def settings(enabled, disabled=(), all_off=False):
            fake = mock.Mock()
            fake.get_boolean.return_value = all_off
            fake.get_strv.side_effect = lambda key: list(enabled if key == "enabled-extensions" else disabled)
            return fake

        self.assertTrue(notifier.cursor_extension_enabled(settings([uuid])))
        self.assertFalse(notifier.cursor_extension_enabled(settings([])))
        self.assertFalse(notifier.cursor_extension_enabled(settings([uuid], [uuid])))
        self.assertFalse(notifier.cursor_extension_enabled(settings([uuid], all_off=True)))
        broken = mock.Mock()
        broken.get_boolean.side_effect = RuntimeError("no schema")
        self.assertIsNone(notifier.cursor_extension_enabled(broken))
        self.assertIn("disabled", TrayNotifier._cursor_extension_text(False))

    def test_enable_cursor_extension_uses_gnome_extensions_enable(self):
        result = mock.Mock(returncode=0, stdout="", stderr="")
        with (mock.patch.object(notifier.shutil, "which", return_value="/usr/bin/gnome-extensions"),
              mock.patch.object(notifier.subprocess, "run", return_value=result) as run):
            ok, _message = TrayNotifier._enable_cursor_extension()
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args[0],
                         ["/usr/bin/gnome-extensions", "enable", notifier.CURSOR_EXTENSION_UUID])

    def test_autostart_state_is_applied_without_retoggling(self):
        tray = bare_tray()
        tray.autostart_item = mock.Mock()
        flags = []
        tray.autostart_item.set_active.side_effect = lambda v: flags.append(tray.updating_autostart_item)
        tray._apply_autostart_state(True)
        self.assertEqual(flags, [True])
        self.assertTrue(tray.autostart_cached)
        tray.autostart_item.set_sensitive.assert_called_with(True)


if __name__ == "__main__":
    unittest.main()
