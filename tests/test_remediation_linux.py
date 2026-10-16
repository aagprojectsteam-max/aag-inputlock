"""Regression tests: no real devices, inhibitors, sessions, or system writes."""
from __future__ import annotations

import errno
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_daemon_logic import daemon_module  # loads fallback stubs where needed
import input_lock_gnome as gnome
from input_lock_common import Config
from input_lock_daemon import ClientState, DeviceRecord, InputLockDaemon
from input_lock_lid_policy import LidNoSuspendPolicy, PolicyError


def daemon_fixture():
    daemon = InputLockDaemon.__new__(InputLockDaemon)
    from input_lock_safety import SafetyPolicy
    daemon.safety = SafetyPolicy()
    daemon._write_safety = mock.Mock()
    daemon._saved_lid_ignore_close = False
    daemon.safety_sample = None
    daemon.protective_result = None
    daemon.running = True
    daemon.lid_is_closed = False
    daemon.lid_state_from_evdev = False
    daemon.last_lid_event_at = 0.0
    daemon.accept_retry_at = None
    daemon.next_safety_tick = float("inf")
    daemon.config = Config(authorized_uid=1000, authorized_user="test")
    daemon.devices = {}
    daemon.clients = {}
    daemon.subscribers = set()
    daemon.selector = mock.Mock()
    daemon.locked_kinds = {"keyboard"}
    daemon.default_locked_kinds = {"keyboard", "mouse"}
    daemon.ignore_lid_close = False
    daemon.lid_policy_healthy = False
    daemon.lid_inhibitor_fd = None
    daemon.sleep_inhibitor_fd = None
    daemon.controller_fd = None
    daemon.lid_generation = 0
    daemon.lid_lease_deadline = None
    daemon.release_errors = []
    daemon.allowed_key_codes = set()
    daemon.allowed_keys = ()
    daemon.allowed_uinput = None
    daemon.allowed_key_down_counts = {}
    daemon.auto_unlock_deadline = None
    daemon.pending_hotkey = None
    daemon.authorized_session_active = True
    daemon.idle_lock_enabled = False
    daemon.idle_lock_seconds = 60
    daemon.last_input_activity = 0.0
    daemon._write_state = mock.Mock()
    daemon._emit_state_event = mock.Mock()
    daemon._persist_user_settings = mock.Mock()
    daemon._ensure_allowed_uinput = mock.Mock()
    return daemon


def device(daemon, name, kind, *, grabbed=True):
    native = mock.Mock()
    native.active_keys.return_value = []
    record = DeviceRecord(name, native, name, frozenset({kind}), "", (), False, "", grabbed=grabbed)
    daemon.devices[name] = record
    return record


class ReleaseRegressionTests(unittest.TestCase):
    def test_emergency_never_acquires_lid(self):
        daemon = daemon_fixture()
        keyboard = device(daemon, "keyboard", "keyboard")
        lid = device(daemon, "lid", "lid-switch", grabbed=False)
        lid.device.grab.side_effect = OSError("busy")
        daemon.ignore_lid_close = True
        daemon._emergency_unlock("test")
        keyboard.device.ungrab.assert_called_once()
        lid.device.grab.assert_not_called()
        daemon._ensure_allowed_uinput.assert_not_called()
        self.assertFalse(daemon.ignore_lid_close)
        self.assertFalse(daemon.locked_kinds)

    def test_release_continues_after_ungrab_failure_without_reopening(self):
        daemon = daemon_fixture()
        broken = device(daemon, "broken", "keyboard")
        other = device(daemon, "other", "mouse")
        broken.device.ungrab.side_effect = OSError("failed")
        daemon._attach_device = mock.Mock()
        daemon._emergency_unlock("test")
        broken.device.close.assert_called_once()
        other.device.ungrab.assert_called_once()
        daemon._attach_device.assert_not_called()
        self.assertTrue(daemon.release_errors)

    def test_partial_unlock_preserves_deadline(self):
        daemon = daemon_fixture()
        daemon.locked_kinds = {"keyboard", "mouse"}
        daemon.auto_unlock_deadline = 123.0
        device(daemon, "keyboard", "keyboard")
        device(daemon, "mouse", "mouse")
        daemon._set_locked_kinds({"keyboard"}, reason="test", timeout=None)
        self.assertEqual(daemon.auto_unlock_deadline, 123.0)

    def test_repeated_lock_explicitly_updates_deadline(self):
        daemon = daemon_fixture()
        with mock.patch.object(daemon_module.time, "monotonic", return_value=100):
            daemon._set_locked_kinds({"keyboard"}, reason="test", timeout=10)
        self.assertEqual(daemon.auto_unlock_deadline, 110)


class LegacyPolicyRegressionTests(unittest.TestCase):
    def test_failed_disable_without_backup_never_enables(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = mock.Mock(side_effect=PolicyError("transient failure"))
            policy = LidNoSuspendPolicy(mock.Mock(), backup_path=Path(directory)/"missing.json", helper=helper)
            with self.assertRaises(PolicyError):
                policy.disable()
            self.assertNotIn(mock.call("enable", True), helper.call_args_list)


class SessionRegressionTests(unittest.TestCase):
    def test_only_local_interactive_graphical_seat_is_eligible(self):
        good = dict(Active=True, Remote=False, Class="user", Type="wayland", Seat=("seat0", "/seat"))
        self.assertTrue(daemon_module.session_is_eligible(good))
        for change in ({"LockedHint":True}, {"Class":"manager"}, {"Remote":True}, {"Seat":("", "/")}, {"Type":"tty"}, {"Active":False}):
            self.assertFalse(daemon_module.session_is_eligible({**good, **change}), change)

    def test_reload_rejects_restart_required_identity(self):
        daemon = daemon_fixture()
        daemon.config_path = Path("unused")
        daemon._emergency_unlock = mock.Mock()
        daemon._rescan_devices = mock.Mock()
        old = daemon.config
        with mock.patch.object(daemon_module, "load_config", return_value=replace(old, authorized_uid=1001)):
            daemon._reload_config()
        self.assertIs(daemon.config, old)
        daemon._rescan_devices.assert_not_called()


class GnomeRegressionTests(unittest.TestCase):
    def test_empty_gvariant_array(self):
        with mock.patch.object(gnome, "run", return_value="@as []"):
            self.assertEqual(gnome.paths(), [])


if __name__ == "__main__":
    unittest.main()

class LinuxLifecycleRegressionTests(unittest.TestCase):
    def test_protection_releases_grabs_and_inhibitor_before_dispatch(self):
        daemon = daemon_fixture()
        daemon.ignore_lid_close = True
        daemon.lid_inhibitor_fd = 123
        lid = device(daemon, 'lid', 'lid-switch')
        order = []
        lid.device.ungrab.side_effect = lambda: order.append('ungrab')
        def request():
            self.assertFalse(daemon.ignore_lid_close)
            self.assertFalse(lid.grabbed)
            self.assertIsNone(daemon.lid_inhibitor_fd)
            order.append('request')
        def thread(**kwargs):
            return mock.Mock(start=kwargs['target'])
        with (mock.patch.object(daemon_module.os, 'close', side_effect=lambda _fd: order.append('close')),
              mock.patch.object(daemon_module, 'request_service', side_effect=request),
              mock.patch.object(daemon_module.threading, 'Thread', side_effect=thread)):
            daemon._post_control = mock.Mock()
            daemon._start_protection('CLOSED_LID_DEADLINE')
        self.assertEqual(order, ['ungrab', 'close', 'request'])

    def test_failed_release_terminates_without_requesting_sleep(self):
        daemon = daemon_fixture()
        lid = device(daemon, 'lid', 'lid-switch')
        lid.device.ungrab.side_effect = OSError('ungrab failed')
        lid.device.close.side_effect = OSError('close failed')
        with mock.patch.object(daemon_module, 'request_service') as request:
            with self.assertRaises(daemon_module.TransitionError):
                daemon._start_protection('CLOSED_LID_DEADLINE')
            request.assert_not_called()

    def armable(self):
        from test_safety import sample
        daemon = daemon_fixture()
        daemon.safety.clock = lambda: daemon_module.time.monotonic()
        daemon.controller_fd = 3
        daemon.clients[3] = ClientState(mock.Mock(), 1000, deadline=135)
        daemon.last_power_at = daemon.last_logind_at = 100
        daemon.safety_sample = sample(100)
        daemon._acquire_lid_inhibitor = mock.Mock()
        device(daemon, 'lid', 'lid-switch', grabbed=False)
        return daemon

    def test_stale_worker_cannot_rearm_after_disable(self):
        daemon = self.armable()
        with mock.patch.object(daemon_module.time, 'monotonic', return_value=100):
            daemon._set_ignore_lid_close(True, generation=0)
            self.assertTrue(daemon.ignore_lid_close)
            old_generation = daemon.lid_generation
            daemon._set_ignore_lid_close(False)
            with self.assertRaises(daemon_module.TransitionError):
                daemon._set_ignore_lid_close(True, generation=old_generation)
        self.assertFalse(daemon.ignore_lid_close)
        self.assertFalse(daemon.safety.armed)
        daemon._acquire_lid_inhibitor.assert_called_once()

    def test_controller_loss_releases_lid_on_open_lid(self):
        daemon = self.armable()
        with mock.patch.object(daemon_module.time, 'monotonic', return_value=100):
            daemon._set_ignore_lid_close(True, generation=0)
            daemon._close_client(3)
        self.assertFalse(daemon.ignore_lid_close)
        self.assertIsNone(daemon.controller_fd)
        daemon.devices['lid'].device.ungrab.assert_called_once()

    def test_controller_loss_with_closed_lid_requests_protection(self):
        daemon = self.armable()
        daemon.ignore_lid_close = True
        daemon.safety_sample.lid_closed = True
        daemon._start_protection = mock.Mock()
        daemon._close_client(3)
        daemon._start_protection.assert_called_once_with('CONTROLLER_LOST')

    def test_owner_heartbeat_expiry_releases(self):
        daemon = self.armable()
        with mock.patch.object(daemon_module.time, 'monotonic', return_value=100):
            daemon._set_ignore_lid_close(True, generation=0)
        with mock.patch.object(daemon_module.time, 'monotonic', return_value=136):
            daemon._handle_timers()
        self.assertFalse(daemon.ignore_lid_close)
        self.assertFalse(daemon.clients)

    def test_failed_lid_grab_rolls_back_arm_without_touching_keyboard(self):
        daemon = self.armable()
        keyboard = device(daemon, 'keyboard', 'keyboard')
        daemon.devices['lid'].device.grab.side_effect = OSError('busy')
        with mock.patch.object(daemon_module.time, 'monotonic', return_value=100):
            with self.assertRaises(OSError):
                daemon._set_ignore_lid_close(True, generation=0)
        self.assertFalse(daemon.ignore_lid_close)
        self.assertFalse(daemon.safety.armed)
        keyboard.device.ungrab.assert_not_called()

    def test_abnormal_previous_run_preserves_evidence_and_starts_disarmed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'safety.json'
            path.write_text(json.dumps({'effective':True,'clean_shutdown':False,'boot_id':'previous',
                                        'safety':{'state':'LID_CLOSED_HIGH_RISK'}}))
            daemon = daemon_fixture()
            daemon._saved_lid_ignore_close = True
            with mock.patch.object(daemon_module, 'BREADCRUMB', path):
                daemon._reset_lid_ignore_on_startup()
            self.assertEqual(daemon.safety.state, 'RECOVERY')
            self.assertFalse(daemon.ignore_lid_close)
            self.assertTrue(daemon._saved_lid_ignore_close)
            self.assertTrue(daemon.safety_previous_run['effective'])

    def test_sync_dropped_discards_until_report_and_resynchronizes_forwarded_keys(self):
        from evdev import ecodes
        daemon = daemon_fixture()
        record = device(daemon, 'keyboard', 'keyboard')
        daemon.allowed_key_codes = {30}
        daemon.allowed_uinput = mock.Mock()
        daemon.allowed_key_down_counts = {30:1}
        record.forwarded_allowed = {30}
        record.device.active_keys.return_value = [30]
        def event(kind, code, value=0): return SimpleNamespace(type=kind, code=code, value=value)
        record.device.read.return_value = [event(ecodes.EV_SYN, ecodes.SYN_DROPPED),
            event(ecodes.EV_KEY,48,1), event(ecodes.EV_KEY,30,0),
            event(ecodes.EV_SYN,ecodes.SYN_REPORT), event(ecodes.EV_KEY,30,0)]
        daemon._write_virtual_allowed = mock.Mock()
        daemon._evaluate_hotkeys = mock.Mock()
        daemon._maybe_fire_pending_hotkey = mock.Mock()
        daemon._read_device('keyboard')
        self.assertEqual(daemon._write_virtual_allowed.call_args_list,
                         [mock.call(30,0),mock.call(30,1),mock.call(30,0)])
        self.assertNotIn(48, record.pressed)
        self.assertFalse(record.synchronizing)

    def test_inactive_graphical_session_cannot_acquire_input(self):
        daemon = daemon_fixture()
        daemon.authorized_session_active = False
        with self.assertRaises(daemon_module.TransitionError):
            daemon._set_locked_kinds({'keyboard','mouse'}, reason='fixture', timeout=None)
        daemon._ensure_allowed_uinput.assert_not_called()

    def test_evdev_close_starts_timer_before_next_poll_and_stale_poll_cannot_reopen(self):
        from test_safety import sample
        from dataclasses import asdict
        daemon = self.armable()
        with mock.patch.object(daemon_module.time, 'monotonic', return_value=100):
            daemon._set_ignore_lid_close(True, generation=0)
        with mock.patch.object(daemon_module.time, 'monotonic', return_value=101):
            daemon._handle_lid_switch_event(True)
        self.assertEqual(daemon.safety.closed_since, 101)
        self.assertEqual(daemon.safety.deadline, 341)
        daemon.wakeup_read = mock.Mock()
        daemon.wakeup_read.recv.side_effect = BlockingIOError()
        daemon.control_queue = daemon_module.queue.SimpleQueue()
        daemon.control_queue.put('safety-sample:' + json.dumps(asdict(sample(100, lid_closed=False))))
        daemon._drain_wakeup()
        self.assertTrue(daemon.safety_sample.lid_closed)

    def test_old_protective_worker_cannot_replace_a_new_episode(self):
        daemon = daemon_fixture()
        daemon.protective_result = {'generation': 42, 'result': 'pending'}
        daemon.wakeup_read = mock.Mock()
        daemon.wakeup_read.recv.side_effect = BlockingIOError()
        daemon.control_queue = daemon_module.queue.SimpleQueue()
        daemon.control_queue.put('protective-result:' + json.dumps(
            {'generation': 41, 'result': 'request-failed', 'error': 'old request'}))
        daemon._drain_wakeup()
        self.assertEqual(daemon.protective_result, {'generation': 42, 'result': 'pending'})
        daemon._write_safety.assert_not_called()


class AdmissionRegressionTests(unittest.TestCase):
    def client(self, uid=1000, fd=3):
        sock = mock.Mock()
        sock.fileno.return_value = fd
        sock.getsockopt.return_value = daemon_module.struct.pack(daemon_module.PEERCRED_FORMAT,123,uid,uid)
        return sock

    def test_unauthorized_peer_is_closed_before_registration(self):
        daemon = daemon_fixture()
        sock = self.client(999)
        daemon.server = mock.Mock()
        daemon.server.accept.side_effect = [(sock,None),BlockingIOError()]
        daemon._accept_clients()
        sock.close.assert_called_once()
        self.assertFalse(daemon.clients)
        daemon.selector.register.assert_not_called()

    def test_maximum_clients_enforced(self):
        daemon = daemon_fixture()
        daemon.clients = {n:object() for n in range(daemon_module.MAX_CLIENTS)}
        sock = self.client(fd=200)
        daemon.server = mock.Mock()
        daemon.server.accept.side_effect = [(sock,None),BlockingIOError()]
        daemon._accept_clients()
        self.assertEqual(len(daemon.clients),daemon_module.MAX_CLIENTS)
        sock.close.assert_called_once()

    def test_incomplete_request_expires(self):
        daemon = daemon_fixture()
        sock = self.client()
        daemon.clients[3] = ClientState(sock,1000,buffer=bytearray(b'{'),deadline=99)
        with mock.patch.object(daemon_module.time,'monotonic',return_value=100):
            daemon._handle_timers()
        self.assertFalse(daemon.clients)
        sock.close.assert_called_once()

    def test_resource_exhaustion_pauses_accept_instead_of_crashing_or_spinning(self):
        daemon = daemon_fixture()
        daemon.server = mock.Mock()
        daemon.server.accept.side_effect = OSError(errno.EMFILE,'full')
        with mock.patch.object(daemon_module.time,'monotonic',return_value=100):
            daemon._accept_clients()
        self.assertEqual(daemon.accept_retry_at,101)
        daemon.selector.unregister.assert_called_once_with(daemon.server)

    def test_subscriber_limit(self):
        daemon = daemon_fixture()
        daemon.subscribers = set(range(daemon_module.MAX_SUBSCRIBERS))
        daemon._send_and_close = mock.Mock()
        daemon._handle_request(100,{'cmd':'subscribe'})
        self.assertIn('subscriber limit',daemon._send_and_close.call_args.args[1]['error'])
