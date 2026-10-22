"""Daemon overhaul 2026-10-22 (fix agent B): sleep window, watchdog, logind
tolerance, IPC output queue, journal failures, lock-out hardening.

Simulations only: fake clocks, socketpairs, temporary directories and fake
D-Bus modules. Nothing touches real devices, logind or systemd.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import selectors  # noqa: E402
import socket  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from dataclasses import asdict  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from test_daemon_logic import daemon_module  # noqa: E402  (installs evdev/pyudev stubs if needed)
from test_remediation_linux import daemon_fixture, device  # noqa: E402
from input_lock_common import ConfigError  # noqa: E402
from input_lock_daemon import ClientState, InputLockDaemon, LogindMonitor, TransitionError, parse_hotkey  # noqa: E402
from input_lock_safety import Sample, Sensor  # noqa: E402

LOGGER = "input-lock-daemon"
_ENVIRONMENT = mock.patch.dict(os.environ)


def setUpModule():
    # Never let a test talk to a real service manager's notify socket.
    _ENVIRONMENT.start()
    os.environ.pop("NOTIFY_SOCKET", None)


def tearDownModule():
    _ENVIRONMENT.stop()


def make_sample(at: float, lid_closed: bool | None = False, cpu: float = 60.0) -> Sample:
    return Sample(at, lid_closed, [Sensor("cpu", "cpu", cpu, 85, 95, 100),
                                   Sensor("chassis", "chassis", 40, 65, 70, 73)],
                  True, 80.0, False, 1, 0.1, True, True)


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def base_daemon(clock: Clock | None = None):
    daemon = daemon_fixture()
    if clock is not None:
        daemon.safety.clock = clock
    daemon.primary_hotkey = parse_hotkey("CTRL+ALT+Z")
    daemon.emergency_hotkey = parse_hotkey("CTRL+ALT+SHIFT+F12")
    daemon.on_battery = False
    daemon.battery_percentage = 80.0
    daemon.last_action = "startup"
    daemon.last_power_at = daemon.last_logind_at = 0.0
    daemon.upower_warning = None
    daemon.last_safety_emit = 0.0
    daemon.last_hotkey_at = 0.0
    daemon.wakeup_read = mock.Mock()
    daemon.wakeup_read.recv.side_effect = BlockingIOError()
    daemon.control_queue = daemon_module.queue.SimpleQueue()
    return daemon


def armed_daemon(clock: Clock):
    """A daemon armed at clock.now with a subscribed tray controller on fd 3."""
    daemon = base_daemon(clock)
    daemon.locked_kinds = set()
    daemon.controller_fd = 3
    daemon.clients[3] = ClientState(mock.Mock(), 1000, subscribed=True, deadline=clock.now + 35, pid=4242)
    daemon.subscribers.add(3)
    daemon.last_power_at = daemon.last_logind_at = clock.now
    daemon.safety_sample = make_sample(clock.now)
    daemon._acquire_lid_inhibitor = mock.Mock()
    device(daemon, "lid", "lid-switch", grabbed=False)
    with mock.patch.object(daemon_module.time, "monotonic", clock):
        daemon._set_ignore_lid_close(True, generation=0)
    assert daemon.ignore_lid_close
    return daemon


def drain(daemon, *events) -> None:
    for event in events:
        daemon.control_queue.put(event)
    daemon._drain_wakeup()


class NotifySocket:
    """A fake systemd notify socket (filesystem or abstract namespace)."""

    def __init__(self, abstract: bool = False) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.tmp = tempfile.TemporaryDirectory()
        if abstract:
            name = f"input-lock-test-{os.getpid()}-{id(self)}"
            self.sock.bind("\0" + name)
            self.env = "@" + name
        else:
            path = os.path.join(self.tmp.name, "notify")
            self.sock.bind(path)
            self.env = path
        self.sock.setblocking(False)

    def messages(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.sock.recv(4096).decode())
            except BlockingIOError:
                return out

    def close(self) -> None:
        self.sock.close()
        self.tmp.cleanup()


class SdNotifyTests(unittest.TestCase):
    def test_filesystem_and_abstract_sockets(self):
        for abstract in (False, True):
            fake = NotifySocket(abstract=abstract)
            try:
                self.assertTrue(daemon_module.sd_notify("READY=1", {"NOTIFY_SOCKET": fake.env}))
                self.assertEqual(fake.messages(), ["READY=1"])
            finally:
                fake.close()

    def test_unsupervised_or_unreachable_is_quietly_false(self):
        self.assertFalse(daemon_module.sd_notify("READY=1", {}))
        self.assertFalse(daemon_module.sd_notify("READY=1", {"NOTIFY_SOCKET": "vsock:2:1"}))
        self.assertFalse(daemon_module.sd_notify("READY=1", {"NOTIFY_SOCKET": "/nonexistent/notify"}))


class WatchdogSleepWindowTests(unittest.TestCase):
    """A3: extend the watchdog for the sleep window and restore it afterwards."""

    def setUp(self):
        self.fake = NotifySocket(abstract=True)
        self.env = mock.patch.dict(os.environ, {"NOTIFY_SOCKET": self.fake.env})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.fake.close()

    def test_logind_signal_extends_then_restores(self):
        daemon = base_daemon()
        drain(daemon, "prepare-sleep")
        self.assertEqual(self.fake.messages(), ["WATCHDOG=1", "WATCHDOG_USEC=300000000"])
        self.assertIsNotNone(daemon.sleep_window_since)
        drain(daemon, "resume-sleep")
        self.assertEqual(self.fake.messages(), ["WATCHDOG_USEC=20000000", "WATCHDOG=1"])
        self.assertIsNone(daemon.sleep_window_since)

    def test_sleep_hook_ipc_pre_and_post(self):
        daemon = base_daemon()
        daemon._send_and_close = mock.Mock()
        daemon.clients[9] = daemon_module.ClientState(mock.Mock(), 0, pid=1)  # root sleep hook (S-R1)
        daemon._handle_request(9, {"cmd": "prepare-sleep"})
        self.assertTrue(daemon._send_and_close.call_args.args[1]["ok"])
        self.assertEqual(self.fake.messages(), ["WATCHDOG=1", "WATCHDOG_USEC=300000000"])
        # The logind signal arriving as well must not reopen or duplicate state.
        drain(daemon, "prepare-sleep")
        self.fake.messages()
        daemon._handle_request(9, {"cmd": "resume-sleep"})
        self.assertTrue(daemon._send_and_close.call_args.args[1]["ok"])
        self.assertEqual(self.fake.messages(), ["WATCHDOG_USEC=20000000", "WATCHDOG=1"])
        drain(daemon, "resume-sleep")  # Second close is a no-op.
        self.assertEqual(self.fake.messages(), [])

    def test_monitor_thread_extends_watchdog_before_the_main_loop(self):
        events = []
        monitor = LogindMonitor(1000, events.append)
        monitor.prepare_for_sleep(True)
        self.assertEqual(self.fake.messages(), ["WATCHDOG=1", "WATCHDOG_USEC=300000000"])
        monitor.prepare_for_sleep(False)
        self.assertEqual(events, ["prepare-sleep", "resume-sleep"])

    def test_safety_tick_pings_the_watchdog(self):
        daemon = base_daemon()
        daemon.next_safety_tick = 0
        daemon._safety_tick()
        self.assertIn("WATCHDOG=1", self.fake.messages())

    def test_missing_resume_signal_closes_the_window_eventually(self):
        clock = Clock(100.0)
        daemon = base_daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "prepare-sleep")
            clock.now = 100 + daemon_module.SLEEP_WINDOW_MAX_SECONDS
            daemon._handle_timers()
        self.assertIsNone(daemon.sleep_window_since)
        self.assertEqual(self.fake.messages()[-2:], ["WATCHDOG_USEC=20000000", "WATCHDOG=1"])


class SleepWindowLeaseTests(unittest.TestCase):
    """A4: controller and lease deadlines pause across a stalled suspend."""

    def test_lease_survives_a_long_freeze_and_gets_one_more_timeout(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "prepare-sleep")
            clock.now = 400.0        # user.slice frozen far beyond CONTROLLER_TIMEOUT
            daemon._handle_timers()
            self.assertIn(3, daemon.clients)
            self.assertTrue(daemon.ignore_lid_close)
            self.assertGreater(daemon._next_timeout(), 0)  # no busy loop on paused deadlines
            drain(daemon, "resume-sleep")
            self.assertEqual(daemon.clients[3].deadline, 435.0)
            self.assertEqual(daemon.lid_lease_deadline, 435.0)
            clock.now = 434.0
            daemon._handle_timers()
            self.assertTrue(daemon.ignore_lid_close)
            clock.now = 436.0        # no heartbeat after resume: normal expiry
            daemon._handle_timers()
        self.assertNotIn(3, daemon.clients)
        self.assertFalse(daemon.ignore_lid_close)
        # U-R1: a missed heartbeat lease has its own key.
        self.assertEqual(daemon.last_lid_disable["reason"], "controller-lease-expired")

    def test_heartbeat_after_resume_keeps_lid_ignore(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon._send = mock.Mock()
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "prepare-sleep")
            clock.now = 300.0
            drain(daemon, "resume-sleep")
            clock.now = 310.0
            daemon._handle_request(3, {"cmd": "heartbeat"})
            clock.now = 340.0
            daemon._handle_timers()
        self.assertTrue(daemon.ignore_lid_close)
        self.assertEqual(daemon.lid_lease_deadline, 345.0)

    def test_unsubscribed_request_clients_still_time_out(self):
        clock = Clock(100.0)
        daemon = base_daemon(clock)
        sock = mock.Mock()
        daemon.clients[5] = ClientState(sock, 1000, deadline=105.0)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "prepare-sleep")
            clock.now = 106.0
            daemon._handle_timers()
        self.assertNotIn(5, daemon.clients)


class SleepInProgressProtectionTests(unittest.TestCase):
    """A10 (daemon side): no protective suspend while logind prepares sleep."""

    def closed_lid(self, clock):
        daemon = armed_daemon(clock)
        daemon.lid_is_closed = True
        daemon.safety_sample.lid_closed = True
        return daemon

    def test_controller_loss_in_window_is_deferred_then_acted_on(self):
        clock = Clock(100.0)
        daemon = self.closed_lid(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "request_service") as request:
            drain(daemon, "prepare-sleep")
            daemon._close_client(3)
            request.assert_not_called()
            self.assertTrue(daemon.ignore_lid_close)
            self.assertEqual(daemon.deferred_protection, "CONTROLLER_LOST")
            clock.now = 200.0
            drain(daemon, "resume-sleep")
            daemon._start_protection = mock.Mock()
            clock.now = 234.0
            daemon._handle_timers()
            daemon._start_protection.assert_not_called()   # still in the grace period
            clock.now = 236.0
            daemon._handle_timers()
        daemon._start_protection.assert_called_once_with("CONTROLLER_LOST")

    def test_deferred_reason_is_dropped_when_the_tray_reconnects(self):
        clock = Clock(100.0)
        daemon = self.closed_lid(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "prepare-sleep")
            daemon._close_client(3)
            drain(daemon, "resume-sleep")
            daemon.clients[8] = ClientState(mock.Mock(), 1000, subscribed=True, deadline=200.0)
            daemon.controller_fd = 8
            daemon._start_protection = mock.Mock()
            clock.now = 136.0
            daemon._handle_timers()
        daemon._start_protection.assert_not_called()
        self.assertTrue(daemon.ignore_lid_close)
        self.assertIsNone(daemon.deferred_protection)

    def test_safety_policy_keeps_gathering_evidence_in_the_window(self):
        # S-R1 (review 2): the policy is updated in the window; any decision
        # is deferred by _start_protection until the window closes.
        clock = Clock(100.0)
        daemon = self.closed_lid(clock)
        daemon.safety.update = mock.Mock(return_value=None)
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "request_service") as request:
            drain(daemon, "prepare-sleep")
            daemon._safety_tick()
            daemon._handle_lid_switch_event(True)
            self.assertEqual(daemon.safety.update.call_count, 2)
            daemon.safety.update.return_value = "THERMAL_EMERGENCY"
            daemon._safety_tick()
            request.assert_not_called()
            self.assertTrue(daemon.ignore_lid_close)
            self.assertEqual(daemon.deferred_protection, "THERMAL_EMERGENCY")
            drain(daemon, "resume-sleep")
            daemon._start_protection = mock.Mock()
            daemon._safety_tick()
        daemon._start_protection.assert_called_once_with("THERMAL_EMERGENCY")


GOOD_SESSION = dict(Id="2", Active=True, Remote=False, Class="user", Type="wayland", Seat=("seat0", "/seat"))


def run_refresh(monitor, sessions):
    rows = [(values.get("Id", "x"), 1000, "user", "seat0", f"/session/{index}")
            for index, values in enumerate(sessions)]
    by_path = {row[4]: values for row, values in zip(rows, sessions)}
    monitor.refresh_sessions_with(lambda: rows, lambda path: by_path[path])


class LogindMonitorTests(unittest.TestCase):
    def test_transient_dbus_failure_is_only_an_error_event(self):
        events = []
        monitor = LogindMonitor(1000, events.append)

        def broken():
            raise RuntimeError("org.freedesktop.DBus.Error.NoReply")
        monitor.refresh_sessions_with(broken, lambda path: {})
        self.assertEqual(events, ["logind-error"])
        self.assertNotIn("session-inactive", events)

    def test_locked_local_session_counts_as_healthy_supervision(self):
        events = []
        monitor = LogindMonitor(1000, events.append)
        run_refresh(monitor, [{**GOOD_SESSION, "LockedHint": True}])
        self.assertEqual(events, ["logind-healthy", "session-locked"])
        self.assertEqual(monitor.graphical_session_ids, frozenset({"2"}))

    def test_no_local_session_is_not_healthy(self):
        events = []
        monitor = LogindMonitor(1000, events.append)
        run_refresh(monitor, [{**GOOD_SESSION, "Remote": True, "Id": "7"}])
        self.assertEqual(events, ["session-inactive"])
        self.assertEqual(monitor.graphical_session_ids, frozenset())


class LogindErrorToleranceTests(unittest.TestCase):
    """A6: act only after ~15 s of consecutive logind failures."""

    def daemon(self, clock):
        daemon = armed_daemon(clock)
        daemon.lid_is_closed = True
        daemon.safety_sample.lid_closed = True
        daemon._start_protection = mock.Mock()
        return daemon

    def test_short_outage_is_tolerated(self):
        clock = Clock(100.0)
        daemon = self.daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            for now in (100.0, 105.0, 110.0):
                clock.now = now
                drain(daemon, "logind-error")
                daemon._handle_timers()
            clock.now = 113.0
            drain(daemon, "logind-healthy", "session-locked")
            clock.now = 130.0
            daemon._handle_timers()
        daemon._start_protection.assert_not_called()
        self.assertTrue(daemon.ignore_lid_close)
        self.assertIsNone(daemon.logind_error_since)

    def test_sustained_outage_is_session_loss(self):
        clock = Clock(100.0)
        daemon = self.daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "logind-error")
            clock.now = 114.0
            drain(daemon, "logind-error")
            daemon._handle_timers()
            daemon._start_protection.assert_not_called()
            self.assertAlmostEqual(daemon._next_timeout(), 1.0)
            clock.now = 115.0
            with self.assertLogs(LOGGER, "WARNING") as logs:
                daemon._handle_timers()
            clock.now = 130.0
            drain(daemon, "logind-error")
            daemon._handle_timers()
        daemon._start_protection.assert_called_once_with("SUPERVISION_LOST")
        self.assertIn("LOGIND_ERROR_PERSISTENT", "\n".join(logs.output))
        self.assertFalse(daemon.authorized_session_active)

    def test_real_session_loss_still_acts_at_once(self):
        clock = Clock(100.0)
        daemon = self.daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "session-inactive")
        daemon._start_protection.assert_called_once_with("SUPERVISION_LOST")

    def test_dead_monitor_thread_is_restarted_with_backoff(self):
        clock = Clock(100.0)
        daemon = base_daemon(clock)
        daemon._start_logind_monitor = mock.Mock()
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "logind-monitor-stopped")
            self.assertEqual(daemon.logind_restart_at, 102.0)
            clock.now = 101.0
            daemon._handle_timers()
            daemon._start_logind_monitor.assert_not_called()
            clock.now = 102.0
            daemon._handle_timers()
            daemon._start_logind_monitor.assert_called_once_with()
            drain(daemon, "logind-monitor-stopped")
            self.assertEqual(daemon.logind_restart_at, 107.0)
            drain(daemon, "logind-healthy")
        self.assertEqual(daemon.logind_restart_attempts, 0)
        self.assertIsNone(daemon.logind_error_since)


class LockedSessionSupervisionTests(unittest.TestCase):
    """A5: a screen lock with the lid closed keeps supervision healthy over time."""

    def test_locked_session_over_advancing_time(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon._start_protection = mock.Mock()
        monitor = LogindMonitor(1000, daemon._post_control)
        daemon._wake = mock.Mock()
        locked = {**GOOD_SESSION, "LockedHint": True}
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            daemon.lid_is_closed = True
            for step in range(0, 121):
                clock.now = 100.0 + step
                if step % 2 == 0:
                    daemon.control_queue.put("safety-sample:" + json.dumps(asdict(make_sample(clock.now, True))))
                    daemon.last_power_at = clock.now
                if step % 5 == 0:
                    run_refresh(monitor, [locked])
                daemon._drain_wakeup()
                daemon._safety_tick()
        daemon._start_protection.assert_not_called()
        self.assertTrue(daemon.safety_sample.dependencies_ok)
        self.assertTrue(daemon.ignore_lid_close)
        self.assertTrue(daemon.authorized_session_locked)
        self.assertFalse(daemon.authorized_session_active)
        self.assertNotEqual(daemon.safety.state, "PROTECTIVE_ACTION_PENDING")
        self.assertTrue(daemon.safety.armed)


class OutputQueueTests(unittest.TestCase):
    """B2: backpressure is never treated as controller loss."""

    def setUp(self):
        self.server_side, self.peer = socket.socketpair()
        for sock, option in ((self.server_side, socket.SO_SNDBUF), (self.peer, socket.SO_RCVBUF)):
            sock.setsockopt(socket.SOL_SOCKET, option, 4096)
        self.server_side.setblocking(False)
        self.peer.setblocking(False)
        self.daemon = base_daemon()
        self.daemon.selector = selectors.DefaultSelector()
        self.fd = self.server_side.fileno()
        self.daemon.selector.register(self.server_side, selectors.EVENT_READ, ("client", self.fd))
        self.daemon.clients[self.fd] = ClientState(self.server_side, 1000, subscribed=True, deadline=1e12)
        self.daemon.subscribers.add(self.fd)
        self.daemon.controller_fd = self.fd
        del self.daemon._emit_state_event  # use the real method
        self.counter = 0

        def payload():
            self.counter += 1
            return {"n": self.counter, "padding": "x" * 2000}
        self.daemon._state_payload = payload

    def tearDown(self):
        self.daemon.selector.close()
        self.server_side.close()
        self.peer.close()

    def read_messages(self):
        """Act as a slow reader: read what is there, let the daemon flush more."""
        data = b""
        for _ in range(100_000):
            if self.fd in self.daemon.clients:
                self.daemon._flush_client(self.fd)
            try:
                chunk = self.peer.recv(65536)
            except BlockingIOError:
                state = self.daemon.clients.get(self.fd)
                if state is None or not (state.outbuf or state.pending_state):
                    break
                continue
            if not chunk:
                break
            data += chunk
        return [json.loads(line) for line in data.decode().splitlines()]

    def test_slow_controller_is_kept_and_state_events_coalesce(self):
        for _ in range(300):
            self.daemon._emit_state_event("safety-status")
        state = self.daemon.clients.get(self.fd)
        self.assertIsNotNone(state, "controller was dropped on backpressure")
        self.assertEqual(self.daemon.controller_fd, self.fd)
        self.assertTrue(state.outbuf or state.pending_state)
        key = self.daemon.selector.get_key(self.server_side)
        self.assertTrue(key.events & selectors.EVENT_WRITE)
        self.assertLess(len(state.outbuf), 64 * 1024)
        messages = self.read_messages()
        self.assertEqual(messages[-1]["state"]["n"], 300)  # newest state always arrives
        self.assertLess(len(messages), 300)
        numbers = [m["state"]["n"] for m in messages]
        self.assertEqual(numbers, sorted(numbers))
        self.assertFalse(self.daemon.selector.get_key(self.server_side).events & selectors.EVENT_WRITE)

    def test_heartbeat_reply_is_queued_not_fatal(self):
        for _ in range(50):
            self.daemon._emit_state_event("safety-status")
        self.daemon._handle_request(self.fd, {"cmd": "heartbeat"})
        self.assertIn(self.fd, self.daemon.clients)
        messages = self.read_messages()
        self.assertIn({"ok": True, "heartbeat": True}, messages)

    def test_closed_peer_is_a_real_loss(self):
        self.peer.close()
        self.daemon._emit_state_event("safety-status")
        self.assertNotIn(self.fd, self.daemon.clients)
        self.assertIsNone(self.daemon.controller_fd)

    def test_response_is_flushed_before_close(self):
        self.daemon.controller_fd = None
        self.daemon.subscribers.clear()
        self.daemon.clients[self.fd].subscribed = False
        self.daemon._send_and_close(self.fd, {"ok": True, "big": "y" * 50000})
        self.assertIn(self.fd, self.daemon.clients)  # still sending
        key = self.daemon.selector.get_key(self.server_side)
        self.assertEqual(key.events, selectors.EVENT_WRITE)  # no further requests read
        messages = self.read_messages()
        self.assertEqual(messages, [{"ok": True, "big": "y" * 50000}])
        self.assertNotIn(self.fd, self.daemon.clients)


def journal_daemon(clock=None):
    daemon = base_daemon(clock)
    del daemon._write_safety  # use the real journal writer
    daemon.boot_id = "fixture"
    daemon.daemon_started_at = 1.0
    daemon.clean_shutdown = False
    daemon.safety_previous_run = None
    daemon.safety_signature = None
    daemon.safety_last_write = 0.0
    return daemon


class SafetyJournalFailureTests(unittest.TestCase):
    """B3: a journal write failure is not fatal; disarm only if armed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.unwritable = Path(self.tmp.name) / "missing" / "safety.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_failure_while_armed_disarms_and_keeps_running(self):
        daemon = journal_daemon()
        daemon.ignore_lid_close = True
        daemon.safety.armed = True
        lid = device(daemon, "lid", "lid-switch")
        with mock.patch.object(daemon_module, "BREADCRUMB", self.unwritable), \
                self.assertLogs(LOGGER, "INFO") as logs:
            daemon._write_safety(force=True)
            daemon._write_safety(force=True)
        self.assertFalse(daemon.ignore_lid_close)
        self.assertFalse(daemon.safety.armed)
        self.assertFalse(lid.grabbed)
        self.assertEqual(daemon.last_lid_disable["reason"], "supervision-journal-unwritable")
        self.assertFalse(daemon.last_lid_disable["requested_by_user"])
        text = "\n".join(logs.output)
        self.assertEqual(text.count("SAFETY_JOURNAL_WRITE_FAILED"), 1)
        # Writable again: recovery is logged.
        with mock.patch.object(daemon_module, "BREADCRUMB", Path(self.tmp.name) / "safety.json"), \
                self.assertLogs(LOGGER, "INFO") as logs:
            daemon._write_safety(force=True)
        self.assertIn("SAFETY_JOURNAL_RESTORED", "\n".join(logs.output))

    def test_failure_while_disarmed_changes_nothing(self):
        daemon = journal_daemon()
        daemon._disable_lid = mock.Mock()
        with mock.patch.object(daemon_module, "BREADCRUMB", self.unwritable):
            daemon._write_safety(force=True)
            with self.assertRaises(OSError):
                daemon._write_safety(force=True, raise_errors=True)
        daemon._disable_lid.assert_not_called()

    def test_arming_refuses_when_the_journal_is_not_writable(self):
        clock = Clock(100.0)
        daemon = journal_daemon(clock)
        daemon.locked_kinds = set()
        daemon.controller_fd = 3
        daemon.clients[3] = ClientState(mock.Mock(), 1000, subscribed=True, deadline=135.0)
        daemon.last_power_at = daemon.last_logind_at = 100.0
        daemon.safety_sample = make_sample(100.0)
        daemon._acquire_lid_inhibitor = mock.Mock()
        lid = device(daemon, "lid", "lid-switch", grabbed=False)
        with mock.patch.object(daemon_module, "BREADCRUMB", self.unwritable), \
                mock.patch.object(daemon_module.time, "monotonic", clock):
            with self.assertRaisesRegex(TransitionError, "safety journal"):
                daemon._set_ignore_lid_close(True, generation=0)
        self.assertFalse(daemon.ignore_lid_close)
        self.assertFalse(daemon.safety.armed)
        lid.device.grab.assert_not_called()
        daemon._acquire_lid_inhibitor.assert_not_called()
        daemon._persist_user_settings.assert_not_called()

    def test_thermal_flapping_does_not_force_journal_writes(self):
        daemon = journal_daemon()
        with mock.patch.object(daemon_module, "atomic_json") as write:
            daemon.safety.update(make_sample(daemon_module.time.monotonic(), cpu=60))
            daemon._write_safety()
            daemon.safety.update(make_sample(daemon_module.time.monotonic(), cpu=96))
            daemon._write_safety()
            daemon.safety.update(make_sample(daemon_module.time.monotonic(), cpu=60))
            daemon._write_safety()
        self.assertEqual(write.call_count, 1)


class ThermalMeasurementLogTests(unittest.TestCase):
    """G1: transitions only, persisting >= 2 samples, DEBUG while disarmed."""

    def snapshot(self, state):
        return {"thermal_measurement": {"state": state, "fresh": True, "complete": True},
                "thermal_protection": "ARMED" if self.daemon.safety.armed else "DISARMED"}

    def note(self, state, at):
        self.daemon.safety.sample = make_sample(at)
        InputLockDaemon._note_thermal_measurement(self.daemon, self.snapshot(state))

    def setUp(self):
        self.daemon = base_daemon()

    def test_single_sample_spike_is_not_logged(self):
        with mock.patch.object(daemon_module.LOG, "log") as log:
            self.note("NORMAL", 1)          # first observation: logged
            self.note("HIGH_RISK", 3)       # one spike sample
            self.note("HIGH_RISK", 3)       # same sample, later tick
            self.note("NORMAL", 5)          # back: candidate dropped
            self.note("WARNING", 7)
            self.note("WARNING", 9)         # second distinct sample: logged
        self.assertEqual([c.args[2] for c in log.call_args_list], ["NORMAL", "WARNING"])
        self.assertTrue(all(c.args[0] == logging.DEBUG for c in log.call_args_list))

    def test_armed_transitions_are_info(self):
        with mock.patch.object(daemon_module.LOG, "log") as log:
            self.note("NORMAL", 1)
            self.daemon.safety.armed = True
            self.note("NORMAL", 3)          # arming change: logged at once
        self.assertEqual(log.call_args_list[-1].args[0], logging.INFO)


class PersistAfterArmTests(unittest.TestCase):
    """B5: ignore_lid_close=True is saved only after arming succeeded."""

    def daemon(self, clock):
        daemon = base_daemon(clock)
        daemon.locked_kinds = set()
        daemon.controller_fd = 3
        daemon.clients[3] = ClientState(mock.Mock(), 1000, subscribed=True, deadline=135.0)
        daemon.last_power_at = daemon.last_logind_at = 100.0
        daemon.safety_sample = make_sample(100.0)
        daemon._acquire_lid_inhibitor = mock.Mock()
        return daemon

    def test_persist_is_the_last_step(self):
        clock = Clock(100.0)
        daemon = self.daemon(clock)
        order = []
        lid = device(daemon, "lid", "lid-switch", grabbed=False)
        daemon._acquire_lid_inhibitor.side_effect = lambda: order.append("inhibit")
        lid.device.grab.side_effect = lambda: order.append("grab")
        daemon._persist_user_settings.side_effect = lambda **kw: order.append(("persist", kw))
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            daemon._set_ignore_lid_close(True, generation=0)
        self.assertEqual(order, ["inhibit", "grab", ("persist", {"ignore_lid_close": True})])
        self.assertTrue(daemon._saved_lid_ignore_close)

    def test_failed_grab_never_persists(self):
        clock = Clock(100.0)
        daemon = self.daemon(clock)
        lid = device(daemon, "lid", "lid-switch", grabbed=False)
        lid.device.grab.side_effect = OSError("busy")
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            with self.assertRaises(OSError):
                daemon._set_ignore_lid_close(True, generation=0)
        daemon._persist_user_settings.assert_not_called()
        self.assertFalse(daemon._saved_lid_ignore_close)

    def test_failed_persist_rolls_back(self):
        clock = Clock(100.0)
        daemon = self.daemon(clock)
        lid = device(daemon, "lid", "lid-switch", grabbed=False)
        daemon._persist_user_settings.side_effect = TransitionError("cannot save user settings: disk full")
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            with self.assertRaises(TransitionError):
                daemon._set_ignore_lid_close(True, generation=0)
        self.assertFalse(daemon.ignore_lid_close)
        self.assertFalse(daemon.safety.armed)
        self.assertFalse(lid.grabbed)
        self.assertFalse(daemon._saved_lid_ignore_close)

    def test_journal_failure_after_arm_reverts_the_saved_preference(self):
        clock = Clock(100.0)
        daemon = self.daemon(clock)
        device(daemon, "lid", "lid-switch", grabbed=False)
        calls = []

        def journal(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise OSError("read-only file system")
        daemon._write_safety = mock.Mock(side_effect=journal)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            with self.assertRaisesRegex(TransitionError, "safety journal"):
                daemon._set_ignore_lid_close(True, generation=0)
        self.assertFalse(daemon.ignore_lid_close)
        self.assertEqual(daemon._persist_user_settings.call_args_list,
                         [mock.call(ignore_lid_close=True), mock.call(ignore_lid_close=False)])
        self.assertFalse(daemon._saved_lid_ignore_close)


class LidInhibitorTests(unittest.TestCase):
    """B4: bounded Inhibit call and the private bus is closed."""

    def fake_dbus(self, fail=False):
        record = {}

        class Bus:
            def __init__(self, kind):
                record["bus"] = self
                self.closed = False

            def get_object(self, name, path, introspect=True):
                record["introspect"] = introspect
                return object()

            def close(self):
                self.closed = True

        class Manager:
            def __init__(self, obj, interface):
                pass

            def Inhibit(self, *args, timeout=None):
                record["args"], record["timeout"] = args, timeout
                if fail:
                    raise RuntimeError("org.freedesktop.DBus.Error.NoReply")
                return SimpleNamespace(take=lambda: 42)

        module = types.ModuleType("dbus")
        module.bus = SimpleNamespace(BusConnection=Bus, BUS_SYSTEM=1)
        module.Interface = Manager
        return module, record

    def test_inhibit_has_a_timeout_and_closes_the_bus(self):
        daemon = base_daemon()
        module, record = self.fake_dbus()
        with mock.patch.dict(sys.modules, {"dbus": module}):
            daemon._acquire_lid_inhibitor()
        self.assertEqual(daemon.lid_inhibitor_fd, 42)
        self.assertEqual(record["timeout"], 5)
        self.assertEqual(record["args"][0], "handle-lid-switch")
        self.assertEqual(record["args"][3], "block")
        self.assertFalse(record["introspect"])
        self.assertTrue(record["bus"].closed)

    def test_failure_also_closes_the_bus(self):
        daemon = base_daemon()
        module, record = self.fake_dbus(fail=True)
        with mock.patch.dict(sys.modules, {"dbus": module}):
            with self.assertRaises(TransitionError):
                daemon._acquire_lid_inhibitor()
        self.assertIsNone(daemon.lid_inhibitor_fd)
        self.assertTrue(record["bus"].closed)


class EmergencyHotkeyTests(unittest.TestCase):
    """L2 (no debounce for the emergency unlock) and L3 (lock cooldown)."""

    def daemon(self, clock):
        daemon = base_daemon(clock)
        keyboard = device(daemon, "keyboard", "keyboard")
        daemon.locked_kinds = {"keyboard"}
        daemon._send_and_close = mock.Mock()
        daemon._state_payload = mock.Mock(return_value={})
        daemon.clients[6] = ClientState(mock.Mock(), 1000, pid=31337)
        return daemon, keyboard

    def test_emergency_is_not_debounced(self):
        clock = Clock(100.0)
        daemon, keyboard = self.daemon(clock)
        daemon.last_hotkey_at = 99.9   # primary hotkey 100 ms ago
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            self.assertEqual(daemon._schedule_hotkey("toggle-default", "all-input", frozenset(), "primary-hotkey"),
                             "ignored-debounce")
            self.assertEqual(daemon._schedule_hotkey("emergency-unlock", "all-input", frozenset(), "emergency-hotkey"),
                             "scheduled")
        keyboard.device.ungrab.assert_called_once()
        self.assertFalse(daemon.locked_kinds)

    def test_ipc_locking_is_refused_for_a_minute_after_a_physical_emergency(self):
        clock = Clock(100.0)
        daemon, _keyboard = self.daemon(clock)
        daemon._apply_action = mock.Mock()
        daemon._set_locked_kinds = mock.Mock()
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            daemon._schedule_hotkey("emergency-unlock", "all-input", frozenset(), "emergency-hotkey")
            self.assertFalse(daemon.locked_kinds)
            clock.now = 130.0
            for request in ({"cmd": "lock", "mode": "keyboard"}, {"cmd": "lock-default"},
                            {"cmd": "toggle", "mode": "all"}, {"cmd": "toggle-default"}, {"cmd": "gnome-hotkey"}):
                with self.assertLogs(LOGGER, "WARNING") as logs:
                    daemon._handle_request(6, request)
                response = daemon._send_and_close.call_args.args[1]
                self.assertFalse(response["ok"], request)
                self.assertIn("paused", response["error"])
                self.assertIn("pid=31337", logs.output[0])
            daemon._apply_action.assert_not_called()
            daemon._set_locked_kinds.assert_not_called()
            daemon._handle_request(6, {"cmd": "unlock", "mode": "all"})   # unlocking is never refused
            self.assertTrue(daemon._send_and_close.call_args.args[1]["ok"])
            clock.now = 161.0
            daemon._apply_action.reset_mock()
            daemon._handle_request(6, {"cmd": "lock", "mode": "keyboard"})
        daemon._apply_action.assert_called_once()
        self.assertTrue(daemon._send_and_close.call_args.args[1]["ok"])

    def test_tty_recovery_also_starts_the_cooldown_but_ipc_emergency_does_not(self):
        clock = Clock(100.0)
        daemon, _keyboard = self.daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            daemon._handle_request(6, {"cmd": "emergency-unlock"})
            self.assertEqual(daemon.lock_cooldown_until, 0.0)
            daemon.last_hotkey_at = 0.0
            daemon._schedule_hotkey("emergency-unlock", "all-input", frozenset(), "tty-recovery-hotkey")
        self.assertEqual(daemon.lock_cooldown_until, 160.0)


class SetHotkeyValidationTests(unittest.TestCase):
    """L4: IPC set-hotkey uses input_lock_common.canonical_hotkey (interface 2)."""

    def setUp(self):
        self.daemon = base_daemon()
        self.daemon._send_and_close = mock.Mock()
        self.daemon._set_primary_hotkey = mock.Mock()
        self.daemon._state_payload = mock.Mock(return_value={})

    def test_canonical_text_is_applied(self):
        with mock.patch("input_lock_common.canonical_hotkey", create=True, return_value="CTRL+ALT+K") as canon:
            self.daemon._handle_request(4, {"cmd": "set-hotkey", "hotkey": "alt+ctrl+k"})
        canon.assert_called_once_with("alt+ctrl+k")
        self.daemon._set_primary_hotkey.assert_called_once_with("CTRL+ALT+K")
        self.assertTrue(self.daemon._send_and_close.call_args.args[1]["ok"])

    def test_weak_hotkey_is_rejected_with_its_reason(self):
        with mock.patch("input_lock_common.canonical_hotkey", create=True,
                        side_effect=ConfigError("hotkey needs at least two modifiers: SHIFT+A")):
            self.daemon._handle_request(4, {"cmd": "set-hotkey", "hotkey": "SHIFT+A"})
        self.daemon._set_primary_hotkey.assert_not_called()
        self.assertEqual(self.daemon._send_and_close.call_args.args[1],
                         {"ok": False, "error": "hotkey needs at least two modifiers: SHIFT+A"})


class ControllerClaimTests(unittest.TestCase):
    """L6: the controller must come from the authorized user's graphical session."""

    def setUp(self):
        self.daemon = base_daemon()
        self.daemon.logind_monitor = SimpleNamespace(graphical_session_ids=frozenset({"2"}))
        self.state = ClientState(mock.Mock(), 1000, pid=777)

    def scope(self, kind, session=None):
        return mock.patch.object(InputLockDaemon, "_peer_session_scope", return_value=(kind, session))

    def test_cgroup_classification(self):
        cases = {
            "0::/user.slice/user-1000.slice/session-2.scope\n": ("session", "2"),
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/input-lock-notifier.service\n": ("user-manager", None),
            "0::/system.slice/ssh.service\n": (None, None),
        }
        for text, expected in cases.items():
            with mock.patch.object(daemon_module.Path, "read_text", return_value=text):
                self.assertEqual(InputLockDaemon._peer_session_scope(777, 1000), expected, text)
        self.assertEqual(InputLockDaemon._peer_session_scope(0, 1000), (None, None))

    def test_graphical_session_peer_is_accepted(self):
        with self.scope("session", "2"):
            self.assertIsNone(self.daemon._controller_claim_problem(self.state))

    def test_other_session_peer_is_refused(self):
        self.daemon._send_and_close = mock.Mock()
        self.daemon.clients[5] = self.state
        with self.scope("session", "9"), self.assertLogs(LOGGER, "WARNING") as logs:
            self.daemon._handle_request(5, {"cmd": "subscribe", "controller": True})
        self.assertIsNone(self.daemon.controller_fd)
        self.assertFalse(self.daemon._send_and_close.call_args.args[1]["ok"])
        self.assertIn("pid=777", "\n".join(logs.output))

    def test_user_service_needs_an_active_or_locked_session(self):
        with self.scope("user-manager"):
            self.daemon.authorized_session_active = True
            self.assertIsNone(self.daemon._controller_claim_problem(self.state))
            self.daemon.authorized_session_active = False
            self.daemon.authorized_session_locked = True
            self.assertIsNone(self.daemon._controller_claim_problem(self.state))

    def test_unverifiable_peer_keeps_first_come_and_is_logged(self):
        self.daemon.authorized_session_active = False
        with self.scope(None), self.assertLogs(LOGGER, "WARNING") as logs:
            self.assertIsNone(self.daemon._controller_claim_problem(self.state))
        self.assertIn("CONTROLLER_CLAIM_UNVERIFIED", logs.output[0])

    def test_root_cannot_be_the_controller(self):
        self.assertIsNotNone(self.daemon._controller_claim_problem(ClientState(mock.Mock(), 0, pid=1)))


class ProtocolAndLidDisableTests(unittest.TestCase):
    """Interface 1 (protocol field) and interface 3 (last_lid_disable)."""

    def test_status_and_snapshot_carry_protocol_3(self):
        daemon = base_daemon()
        daemon._send_and_close = mock.Mock()
        self.assertEqual(daemon._state_payload()["protocol"], 3)
        daemon._handle_request(4, {"cmd": "status"})
        response = daemon._send_and_close.call_args.args[1]
        self.assertEqual(response["protocol"], 3)
        self.assertEqual(response["state"]["protocol"], 3)
        daemon._handle_request(4, {"cmd": "ping"})
        self.assertEqual(daemon._send_and_close.call_args.args[1]["protocol"], 3)

    def test_user_request_is_marked_and_logged(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        self.assertIsNone(daemon.last_lid_disable)
        with self.assertLogs(LOGGER, "INFO") as logs:
            daemon._set_ignore_lid_close(False)
        self.assertEqual(daemon.last_lid_disable["reason"], "lid-ignore-disabled")
        self.assertTrue(daemon.last_lid_disable["requested_by_user"])
        self.assertIsInstance(daemon.last_lid_disable["at"], float)
        self.assertIn("event=LID_IGNORE_DISABLED reason=lid-ignore-disabled requested_by_user=True",
                      "\n".join(logs.output))
        self.assertEqual(daemon._state_payload()["last_lid_disable"], daemon.last_lid_disable)

    def test_controller_loss_is_not_user_requested(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon._close_client(3)
        self.assertEqual(daemon.last_lid_disable["reason"], "controller-disconnected")
        self.assertFalse(daemon.last_lid_disable["requested_by_user"])

    def test_emergency_and_protection_paths_record_the_reason(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon._emergency_unlock("emergency-hotkey")
        self.assertEqual(daemon.last_lid_disable["reason"], "emergency-unlock")
        self.assertFalse(daemon.last_lid_disable["requested_by_user"])
        daemon = armed_daemon(clock)
        with mock.patch.object(daemon_module, "request_service"), \
                mock.patch.object(daemon_module.threading, "Thread"):
            daemon._start_protection("THERMAL_EMERGENCY")
        self.assertEqual(daemon.last_lid_disable["reason"], "thermal-emergency")
        self.assertFalse(daemon.last_lid_disable["requested_by_user"])

    def test_every_daemon_reason_maps_to_a_tray_key(self):
        """The tray understands these keys, or reasons naming thermal,
        battery, telemetry, supervision or sleep (coordinator, interface 3)."""
        known = {"controller-lease-expired", "controller-disconnected", "session-inactive",
                 "daemon-shutdown", "emergency-unlock", "settings-reset"}
        words = ("thermal", "battery", "telemetry", "supervision", "sleep")
        internal = [
            "controller-disconnected", "session-inactive", "logind-unavailable", "safety-journal-failed",
            "emergency-hotkey", "gnome-emergency-hotkey", "tty-recovery-hotkey", "ipc-emergency-unlock",
            "daemon-shutdown", "authorized-session-inactive", "user-session-ended", "hotkey-change",
            "lock-kinds-change", "allowed-keys-change", "configuration-reload", "invalid-config-reload",
            "system-suspend", "system-sleep-hook", "CONTROLLER_LOST", "SUPERVISION_LOST", "LID_DEVICE_LOST",
        ] + ["protective-suspend:" + code for code in (
            "CONTROLLER_LOST", "SUPERVISION_LOST", "TELEMETRY_LOST", "LID_TELEMETRY_LOST", "LID_DEVICE_LOST",
            "BATTERY_CRITICAL", "THERMAL_HIGH_RISK", "THERMAL_EMERGENCY", "DAEMON_FAILED")]
        for reason in internal:
            key = daemon_module.lid_disable_key(reason)
            self.assertTrue(key in known or any(word in key for word in words), (reason, key))

    def test_session_and_logind_losses_use_tray_keys(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        drain(daemon, "session-inactive")
        self.assertEqual(daemon.last_lid_disable["reason"], "session-inactive")
        daemon = armed_daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "logind-error")
            clock.now = 116.0
            daemon._handle_timers()
        self.assertIn("supervision", daemon.last_lid_disable["reason"])
        self.assertFalse(daemon.last_lid_disable["requested_by_user"])

    def test_disable_when_already_off_records_nothing(self):
        daemon = base_daemon()
        daemon._set_ignore_lid_close(False)
        self.assertIsNone(daemon.last_lid_disable)


class ReadinessNotificationTests(unittest.TestCase):
    """B1: READY=1 only after the socket, the device scan and the first state file."""

    def test_ready_after_server_devices_and_state(self):
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        order = []

        def step(name):
            return mock.Mock(side_effect=lambda *a, **k: order.append(name))
        for name in ("_install_signal_handlers", "_reset_lid_ignore_on_startup", "_setup_server",
                     "_setup_udev", "_rescan_devices", "_write_state", "_start_logind_monitor", "_cleanup"):
            setattr(daemon, name, step(name))
        daemon.selector = mock.Mock()
        daemon.wakeup_read = mock.Mock()
        daemon.allowed_key_codes = set()
        daemon.ignore_lid_close = False
        daemon.config = SimpleNamespace(authorized_uid=1000)
        daemon.devices = {}
        daemon.running = False
        daemon.reload_requested = False
        with mock.patch.object(daemon_module, "sd_notify", side_effect=lambda m: order.append(m)), \
                mock.patch.object(daemon_module.threading, "Thread"):
            self.assertEqual(daemon.run(), 0)
        ready = order.index("READY=1")
        for name in ("_setup_server", "_rescan_devices", "_write_state"):
            self.assertLess(order.index(name), ready, name)
        self.assertLess(ready, order.index("_start_logind_monitor"))

    def test_unit_is_notify_and_keeps_the_20s_watchdog(self):
        unit = (Path(__file__).resolve().parents[1] / "systemd" / "input-lock.service").read_text()
        lines = {line.strip() for line in unit.splitlines()}
        self.assertIn("Type=notify", lines)
        self.assertIn("WatchdogSec=20s", lines)
        self.assertIn("Restart=no", lines)

    def test_sleep_hook_has_a_post_branch(self):
        hook = (Path(__file__).resolve().parents[1] / "integration" / "input-lock-system-sleep").read_text()
        self.assertIn("prepare-sleep", hook)
        self.assertIn("post)", hook)
        self.assertIn("input-lock resume-sleep", hook)


if __name__ == "__main__":
    unittest.main()
