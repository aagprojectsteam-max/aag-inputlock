"""Second review round, root and safety side (S-R1..S-R8) and the daemon side
of U-R1 and U-R5.

Simulations only: fake clocks, socketpairs, temporary directories and fake
D-Bus/GLib modules. Nothing here talks to a real bus, a notify socket, a unit,
a device or real sleep.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import socket  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from dataclasses import replace  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from test_daemon_logic import daemon_module  # noqa: E402  (installs evdev/pyudev stubs if needed)
from test_daemon_overhaul_20261022 import Clock, armed_daemon, base_daemon, drain  # noqa: E402
import input_lock_safety as safety  # noqa: E402
from input_lock_daemon import ClientState, LogindMonitor, RecoveryRequired, TransitionError  # noqa: E402
from input_lock_safety import SafetyPolicy, Sample, Sensor  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
_ENVIRONMENT = mock.patch.dict(os.environ)


def setUpModule():
    # Never let a test talk to a real service manager's notify socket.
    _ENVIRONMENT.start()
    os.environ.pop("NOTIFY_SOCKET", None)


def tearDownModule():
    _ENVIRONMENT.stop()


def cpu(identity, celsius):
    return Sensor(identity, "cpu", celsius, 95, 104, 107)


def chassis(identity, celsius):
    return Sensor(identity, "chassis", celsius, 65, 70, 73)


def hot_sample(at, celsius=106.0, preparing=None, sen=40.0):
    """The reviewer's window_sim.py sample: armed, lid closed, on battery."""
    return Sample(at, True, [cpu("thermal:TCPU", celsius), chassis("thermal:SEN1", sen),
                             chassis("thermal:SEN2", sen)],
                  False, 50.0, True, 1, 0.9, True, True, preparing=preparing)


class SuspendStats:
    """A fake /sys/power/suspend_stats directory."""

    def __init__(self, success=5, fail=1):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.set(success, fail)
        self.patch = mock.patch.object(daemon_module, "SUSPEND_STATS_DIR", self.path)
        self.patch.start()

    def set(self, success, fail):
        (self.path / "success").write_text(f"{success}\n")
        (self.path / "fail").write_text(f"{fail}\n")

    def close(self):
        self.patch.stop()
        self.tmp.cleanup()


def root_client(daemon, fd=9):
    daemon.clients[fd] = ClientState(mock.Mock(), 0, pid=1)


# --------------------------------------------------------------------- S-R1


class SleepWindowRootOnlyTests(unittest.TestCase):
    """S-R1: prepare-sleep/resume-sleep only from root (the system-sleep hook)."""

    def test_user_requests_are_refused_with_a_clear_error(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon._send_and_close = mock.Mock()
        with mock.patch.object(daemon_module, "sd_notify") as notify, \
                mock.patch.object(daemon_module.time, "monotonic", clock):
            for command in ("prepare-sleep", "resume-sleep"):
                with self.assertLogs("input-lock-daemon", "WARNING") as logs:
                    daemon._handle_request(3, {"cmd": command})
                response = daemon._send_and_close.call_args.args[1]
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"],
                                 f"{command} is accepted only from root (the system-sleep hook)")
                self.assertIn("SLEEP_WINDOW_REQUEST_REFUSED", "\n".join(logs.output))
        notify.assert_not_called()
        self.assertIsNone(daemon.sleep_window_since)
        self.assertIsNone(daemon.safety.sleep_window_since)

    def test_unknown_peer_is_refused(self):
        daemon = base_daemon()
        daemon._send_and_close = mock.Mock()
        daemon._handle_request(42, {"cmd": "prepare-sleep"})
        self.assertFalse(daemon._send_and_close.call_args.args[1]["ok"])
        self.assertIsNone(daemon.sleep_window_since)

    def test_root_hook_opens_and_closes_the_window(self):
        daemon = base_daemon(Clock(10.0))
        daemon._send_and_close = mock.Mock()
        root_client(daemon)
        stats = SuspendStats()
        self.addCleanup(stats.close)
        with mock.patch.object(daemon_module, "sd_notify") as notify:
            daemon._handle_request(9, {"cmd": "prepare-sleep"})
            self.assertTrue(daemon._send_and_close.call_args.args[1]["ok"])
            self.assertIsNotNone(daemon.sleep_window_since)
            daemon._handle_request(9, {"cmd": "resume-sleep"})
            self.assertTrue(daemon._send_and_close.call_args.args[1]["ok"])
        self.assertIsNone(daemon.sleep_window_since)
        self.assertEqual([c.args[0] for c in notify.call_args_list],
                         ["WATCHDOG=1", "WATCHDOG_USEC=300000000", "WATCHDOG_USEC=20000000", "WATCHDOG=1"])

    def test_hook_stays_posix_sh_and_calls_the_cli(self):
        hook = (REPO / "integration" / "input-lock-system-sleep").read_text()
        self.assertTrue(hook.startswith("#!/bin/sh\n"))
        self.assertNotIn("[[", hook)
        self.assertIn("/usr/bin/input-lock prepare-sleep", hook)
        self.assertIn("/usr/bin/input-lock resume-sleep", hook)

    def test_window_cap_is_about_two_minutes(self):
        self.assertEqual(daemon_module.SLEEP_WINDOW_MAX_SECONDS, 120.0)
        self.assertEqual(safety.SLEEP_WINDOW_MAX_SECONDS, 300.0)


class ThermalSleepWindowScenarioTests(unittest.TestCase):
    """The reviewer's window_sim.py: armed, lid closed, on battery, TCPU 106 C.

    Before the fix one same-UID prepare-sleep moved the decision from +62 s
    to +660 s.
    """

    def setUp(self):
        self.stats = SuspendStats()

    def tearDown(self):
        self.stats.close()

    def simulate(self, *, window=None, preparing=None, seconds=800, on_step=None):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        actions = []
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "request_service"), \
                mock.patch.object(daemon_module.threading, "Thread"), \
                mock.patch.object(daemon_module, "sd_notify"):
            if window == "user":
                daemon._send_and_close = mock.Mock()
                daemon._handle_request(3, {"cmd": "prepare-sleep"})   # fd 3 = uid 1000
            elif window == "root":
                daemon._send_and_close = mock.Mock()
                root_client(daemon)
                daemon._handle_request(9, {"cmd": "prepare-sleep"})
            for step in range(seconds):
                clock.now = 100.0 + step
                if on_step is not None:
                    on_step(step)
                daemon.last_power_at = daemon.last_logind_at = clock.now
                if step % 2 == 0:
                    daemon.safety_sample = hot_sample(clock.now, preparing=preparing)
                if 3 in daemon.clients:
                    daemon.clients[3].deadline = clock.now + 35
                daemon.next_safety_tick = 0
                daemon._handle_timers()
                if daemon.protective_result is not None:
                    actions.append(step)
                    break
        return daemon, (actions[0] if actions else None)

    def test_without_a_window_protection_acts_after_about_a_minute(self):
        daemon, step = self.simulate()
        self.assertIsNotNone(step)
        self.assertTrue(60 <= step <= 64, step)
        self.assertEqual(daemon.protective_result["reason"], "THERMAL_HIGH_RISK")

    def test_same_uid_prepare_sleep_no_longer_delays_protection(self):
        daemon, step = self.simulate(window="user")
        self.assertTrue(60 <= step <= 64, step)
        self.assertIsNone(daemon.sleep_window_since)

    def test_root_window_without_resume_is_capped_and_evidence_counts(self):
        daemon, step = self.simulate(window="root", preparing=None)
        self.assertIsNotNone(step)
        self.assertTrue(120 <= step <= 124, step)
        self.assertIsNone(daemon.sleep_window_since)
        rule = daemon.safety.last_decision["thermal_evidence"]["rule"]
        self.assertEqual(rule["rule"], "cpu-protective")
        # The heat during the window was counted, not discarded.
        self.assertGreaterEqual(rule["elapsed_seconds"], 110)

    def test_root_window_closes_early_when_logind_is_not_preparing(self):
        daemon, step = self.simulate(window="root", preparing=False)
        self.assertTrue(60 <= step <= 64, step)

    def test_root_window_closes_early_when_a_suspend_completed(self):
        def resumed(step):
            if step == 10:
                self.stats.set(6, 1)
        daemon, step = self.simulate(window="root", preparing=True, on_step=resumed)
        # The policy still ignores logind's own "preparing" up to its cap,
        # but the daemon window (paused deadlines, watchdog) closed at once.
        self.assertIsNotNone(step)
        self.assertIsNone(daemon.sleep_window_since)

    def test_kernel_counter_change_closes_the_window(self):
        clock = Clock(100.0)
        daemon = base_daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "sd_notify"):
            drain(daemon, "prepare-sleep")
            self.assertEqual(daemon.sleep_stats_at_open, (5, 1))
            clock.now = 101.0
            daemon._safety_tick()
            self.assertIsNotNone(daemon.sleep_window_since)
            self.stats.set(5, 2)          # a failed attempt also means awake again
            clock.now = 102.0
            with self.assertLogs("input-lock-daemon", "INFO") as logs:
                daemon._safety_tick()
        self.assertIsNone(daemon.sleep_window_since)
        self.assertIn("source=resume-evidence:suspend-stats", "\n".join(logs.output))

    def test_one_not_preparing_sample_or_an_older_one_is_not_enough(self):
        clock = Clock(100.0)
        daemon = base_daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "sd_notify"):
            daemon.safety_sample = hot_sample(99.0, preparing=False)   # read before the window
            drain(daemon, "prepare-sleep")
            clock.now = 101.0
            daemon._safety_tick()
            daemon.safety_sample = hot_sample(101.0, preparing=False)
            daemon._safety_tick()
            clock.now = 102.0
            daemon._safety_tick()                                        # same sample again
            self.assertIsNotNone(daemon.sleep_window_since)
            daemon.safety_sample = hot_sample(103.0, preparing=True)     # resets the run
            clock.now = 103.0
            daemon._safety_tick()
            daemon.safety_sample = hot_sample(105.0, preparing=False)
            clock.now = 105.0
            daemon._safety_tick()
            self.assertIsNotNone(daemon.sleep_window_since)
            daemon.safety_sample = hot_sample(107.0, preparing=False)
            clock.now = 107.0
            daemon._safety_tick()
        self.assertIsNone(daemon.sleep_window_since)

    def test_policy_cap_of_300_seconds_still_applies_to_logind_preparing(self):
        # logind stuck in "preparing" (no daemon window): the policy waits at
        # most its own 300 s cap, then acts on the evidence it gathered.
        daemon, step = self.simulate(preparing=True)
        self.assertIsNotNone(step)
        self.assertTrue(299 <= step <= 304, step)

    def test_decision_made_inside_the_window_is_acted_on_when_it_closes(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon.lid_is_closed = True
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "request_service"), \
                mock.patch.object(daemon_module.threading, "Thread"), \
                mock.patch.object(daemon_module, "sd_notify"):
            drain(daemon, "prepare-sleep")
            daemon.safety.decide("THERMAL_EMERGENCY")       # e.g. after the policy cap expired
            daemon._start_protection("THERMAL_EMERGENCY")
            self.assertIsNone(daemon.protective_result)    # deferred in the window
            self.assertTrue(daemon.ignore_lid_close)
            drain(daemon, "resume-sleep")
        self.assertEqual(daemon.protective_result["reason"], "THERMAL_EMERGENCY")
        self.assertFalse(daemon.ignore_lid_close)

    def test_deferred_controller_loss_does_not_hide_a_later_policy_decision(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon.lid_is_closed = True
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "request_service"), \
                mock.patch.object(daemon_module.threading, "Thread"), \
                mock.patch.object(daemon_module, "sd_notify"):
            drain(daemon, "prepare-sleep")
            daemon._close_client(3)                          # deferred CONTROLLER_LOST
            daemon.safety.decide("BATTERY_CRITICAL")         # later decision, disarmed
            daemon._start_protection("BATTERY_CRITICAL")
            daemon.clients[8] = ClientState(mock.Mock(), 1000, subscribed=True, deadline=1e9)
            daemon.controller_fd = 8                         # tray is back
            drain(daemon, "resume-sleep")
        self.assertEqual(daemon.protective_result["reason"], "BATTERY_CRITICAL")


# --------------------------------------------------------------------- S-R2


class FakeStop:
    """safety_stop stand-in: stops the telemetry loop after N samples."""

    def __init__(self, limit):
        self.limit = limit
        self.posts = []

    def is_set(self):
        return len(self.posts) >= self.limit

    def wait(self, _seconds):
        time.sleep(0.005)
        return self.is_set()

    def set(self):
        self.limit = 0


class ReadinessProbeThreadTests(unittest.TestCase):
    """S-R2: the probe runs on its own thread; telemetry never waits on it."""

    def test_telemetry_keeps_sampling_while_the_probe_hangs(self):
        daemon = base_daemon()
        release = threading.Event()
        probe_started = threading.Event()

        def hanging_probe():
            probe_started.set()
            release.wait(5)
            return []

        class FakeTelemetry:
            def read(self, *, stack_ready=False, **_kwargs):
                probe_started.wait(1)    # the probe call is in progress (patched)
                return Sample(time.monotonic(), False, [], stack_ready=stack_ready)

        stop = FakeStop(limit=5)
        daemon.safety_stop = stop
        daemon._post_control = lambda event: stop.posts.append(json.loads(event.partition(":")[2]))
        try:
            with mock.patch.object(daemon_module, "protection_errors", hanging_probe), \
                    mock.patch.object(daemon_module, "Telemetry", FakeTelemetry):
                started = time.monotonic()
                daemon._sample_safety()
                elapsed = time.monotonic() - started
                self.assertTrue(probe_started.wait(1))
        finally:
            release.set()
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(stop.posts), 5)
        for sample in stop.posts:
            self.assertFalse(sample["stack_ready"])
            self.assertEqual(sample["protection_errors"], ["native protection probe pending"])

    def test_cached_result_and_staleness(self):
        clock = Clock(1000.0)
        daemon = base_daemon()
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            self.assertEqual(daemon._cached_protection_errors(), ["native protection probe pending"])
            daemon.protection_probe_errors, daemon.protection_probe_at = (), 990.0
            self.assertEqual(daemon._cached_protection_errors(), [])
            clock.now = 990.0 + daemon_module.PROTECTION_PROBE_STALE_SECONDS + 1
            self.assertEqual(daemon._cached_protection_errors(),
                             ["native protection probe has not answered recently"])

    def test_probe_loop_caches_errors_and_exceptions(self):
        daemon = base_daemon()
        daemon.safety_stop = SimpleNamespace(is_set=iter([False, True]).__next__, wait=lambda _s: False)
        with mock.patch.object(daemon_module, "protection_errors", side_effect=RuntimeError("bus")):
            daemon._probe_protection()
        self.assertEqual(daemon.protection_probe_errors, ("native protection probe failed: bus",))
        self.assertIsNotNone(daemon.protection_probe_at)

    def test_bounded_call_times_out_without_waiting_for_the_call(self):
        release = threading.Event()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "did not answer"):
                safety.bounded_call(lambda: release.wait(5), 0.1, "fixture")
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(safety.bounded_call(lambda: 7, 1, "fixture"), 7)
        with self.assertRaises(ValueError):
            safety.bounded_call(mock.Mock(side_effect=ValueError("x")), 1, "fixture")

    def test_report_with_a_hanging_logind_fails_instead_of_hanging(self):
        release = threading.Event()
        try:
            with mock.patch.object(safety, "NATIVE_STATUS_TIMEOUT_SECONDS", 0.1), \
                    mock.patch.object(safety, "latch_state", return_value=("absent", "")):
                started = time.monotonic()
                report = safety.protection_report(run=mock.Mock(), status=lambda: release.wait(5),
                                                  counter=lambda: 1)
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("did not answer", report["errors"][0])

    def test_sleep_probe_never_blocks_the_telemetry_thread_on_connect(self):
        release = threading.Event()
        properties = mock.Mock()
        properties.Get.return_value = True
        bus = mock.Mock()

        def connect():
            release.wait(5)
            return bus, properties
        probe = safety._LogindSleepProbe(connect=connect)
        try:
            started = time.monotonic()
            self.assertIsNone(probe())
            self.assertIsNone(probe())          # one connect attempt in flight
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
        deadline = time.monotonic() + 2
        while probe.properties is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(probe())
        properties.Get.assert_called_with("org.freedesktop.login1.Manager", "PreparingForSleep", timeout=1)
        properties.Get.side_effect = RuntimeError("gone")
        self.assertIsNone(probe())
        bus.close.assert_called_once()


# --------------------------------------------------------------- S-R3, S-R5


class DescriptorFactory:
    """An inhibit() stand-in returning real pipe descriptors (like UnixFd.take())."""

    def __init__(self):
        self.fds = []

    def __call__(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        self.fds.append(read_fd)
        return SimpleNamespace(take=lambda: read_fd)

    @staticmethod
    def is_open(fd):
        try:
            os.fstat(fd)
            return True
        except OSError:
            return False


class SleepDelayInhibitorTests(unittest.TestCase):
    """S-R3: hold a delay lock; extend the watchdog first, then release it."""

    def test_order_watchdog_then_release_then_reacquire(self):
        order = []
        monitor = LogindMonitor(1000, lambda event: order.append(("callback", event)))
        inhibit = DescriptorFactory()
        self.assertTrue(monitor.acquire_delay_inhibitor(inhibit))
        fd = inhibit.fds[0]

        def notify(message):
            order.append(("notify", message, DescriptorFactory.is_open(fd)))
        with mock.patch.object(daemon_module, "sd_notify", notify):
            monitor.prepare_for_sleep(True)
        self.assertEqual(order[0], ("notify", "WATCHDOG=1", True))
        self.assertEqual(order[1], ("notify", "WATCHDOG_USEC=300000000", True))
        self.assertEqual(order[2], ("callback", "prepare-sleep"))
        self.assertFalse(DescriptorFactory.is_open(fd))
        # No new lock while the sleep is in progress.
        self.assertFalse(monitor.acquire_delay_inhibitor(inhibit))
        self.assertEqual(len(inhibit.fds), 1)
        monitor.prepare_for_sleep(False)
        self.assertEqual(order[-1], ("callback", "resume-sleep"))
        self.assertTrue(monitor._refresh_wanted.is_set())
        self.assertTrue(monitor.acquire_delay_inhibitor(inhibit))
        self.assertTrue(DescriptorFactory.is_open(inhibit.fds[1]))
        monitor.release_delay_inhibitor()
        self.assertFalse(DescriptorFactory.is_open(inhibit.fds[1]))

    def test_lock_obtained_while_a_sleep_starts_is_dropped(self):
        monitor = LogindMonitor(1000, lambda event: None)
        inhibit = DescriptorFactory()

        def racing():
            descriptor = inhibit()
            with mock.patch.object(daemon_module, "sd_notify"):
                monitor.prepare_for_sleep(True)   # signal arrives during the call
            return descriptor
        self.assertFalse(monitor.acquire_delay_inhibitor(racing))
        self.assertIsNone(monitor._delay_fd)
        self.assertFalse(DescriptorFactory.is_open(inhibit.fds[0]))

    def test_inhibit_failure_is_tolerated_and_logged_once(self):
        monitor = LogindMonitor(1000, lambda event: None)
        with self.assertLogs("input-lock-daemon", "WARNING") as logs:
            for _ in range(3):
                self.assertFalse(monitor.acquire_delay_inhibitor(mock.Mock(side_effect=RuntimeError("denied"))))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("SLEEP_DELAY_INHIBITOR_UNAVAILABLE", logs.output[0])

    def test_inhibitor_is_delay_mode_for_sleep_only(self):
        api = daemon_module._LogindBusApi.__new__(daemon_module._LogindBusApi)
        api.manager = mock.Mock()
        api.inhibit_sleep_delay()
        args = api.manager.Inhibit.call_args.args
        self.assertEqual((args[0], args[3]), ("sleep", "delay"))
        self.assertEqual(api.manager.Inhibit.call_args.kwargs, {"timeout": 3})

    def test_polkit_allows_only_the_delay_sleep_lock(self):
        rules = (REPO / "config" / "90-input-lock.rules").read_text()
        self.assertIn('"org.freedesktop.login1.inhibit-delay-sleep"', rules)
        self.assertNotIn("inhibit-block-sleep", rules)
        self.assertNotIn("SD_LOGIND_SKIP_INHIBITORS", rules)


class FakeApi:
    def __init__(self, block=None, fail_sessions=False):
        self.block = block
        self.fail_sessions = fail_sessions
        self.closed = False
        self.inhibit = DescriptorFactory()

    def list_sessions(self):
        if self.block is not None:
            self.block.wait(5)
        if self.fail_sessions:
            raise RuntimeError("org.freedesktop.DBus.Error.NoReply")
        return [("2", 1000, "user", "seat0", "/session/2")]

    def session_properties(self, path):
        return dict(Id="2", Active=True, Remote=False, Class="user", Type="wayland",
                    Seat=("seat0", "/seat"))

    def power_state(self):
        return {"on_battery": False, "lid_closed": False, "percentage": 80.0,
                "warning_level": 1, "critical_action": "PowerOff"}

    def inhibit_sleep_delay(self):
        return self.inhibit()

    def close(self):
        self.closed = True


class RefreshThreadTests(unittest.TestCase):
    """S-R3: synchronous refresh calls never delay the signal path."""

    def run_loop(self, monitor, connect):
        thread = threading.Thread(target=monitor.refresh_loop, args=(connect,), daemon=True)
        thread.start()
        return thread

    def test_prepare_for_sleep_is_not_stuck_behind_a_slow_refresh(self):
        events = []
        monitor = LogindMonitor(1000, events.append)
        monitor.REFRESH_SECONDS = 0.01
        block = threading.Event()
        api = FakeApi(block=block)
        thread = self.run_loop(monitor, lambda: api)
        try:
            deadline = time.monotonic() + 2
            while not api.inhibit.fds and time.monotonic() < deadline:
                time.sleep(0.005)
            started = time.monotonic()
            with mock.patch.object(daemon_module, "sd_notify"):
                monitor.prepare_for_sleep(True)      # refresh is blocked in ListSessions
                monitor.properties_changed("org.freedesktop.UPower", {}, [], path="/x")
                monitor.request_refresh()
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertIn("prepare-sleep", events)
            self.assertFalse(DescriptorFactory.is_open(api.inhibit.fds[0]))
        finally:
            monitor.stop()
            block.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(api.closed)

    def test_failures_reconnect_and_report_logind_error(self):
        events = []
        monitor = LogindMonitor(1000, events.append)
        monitor.REFRESH_SECONDS = 0.01
        apis = [FakeApi(fail_sessions=True), FakeApi()]
        attempts = []

        def connect():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown")
            return apis.pop(0) if apis else FakeApi()
        with self.assertLogs("input-lock-daemon", "WARNING"):
            thread = self.run_loop(monitor, connect)
            deadline = time.monotonic() + 2
            while "session-active" not in events and time.monotonic() < deadline:
                time.sleep(0.005)
            monitor.stop()
            thread.join(2)
        self.assertGreaterEqual(len(attempts), 3)
        self.assertEqual(events[:2], ["logind-error", "power-unavailable"])
        self.assertIn("session-active", events)
        self.assertTrue(any(e.startswith("power-state:") for e in events))
        monitor.release_delay_inhibitor()


def fake_dbus_modules(record):
    class BusConnection:
        def __init__(self, address, mainloop=None):
            self.address, self.mainloop, self.closed = address, mainloop, False
            self.receivers, self.on_disconnect = [], []
            record.setdefault("buses", []).append(self)

        def set_exit_on_disconnect(self, value):
            self.exit_on_disconnect = value

        def call_on_disconnection(self, callback):
            self.on_disconnect.append(callback)

        def add_signal_receiver(self, handler, **kwargs):
            self.receivers.append((handler, kwargs))

        def get_object(self, *args, **kwargs):
            return object()

        def close(self):
            self.closed = True

    class Interface:
        def __init__(self, obj, interface):
            self.interface = interface

        def __getattr__(self, name):
            def method(*args, **kwargs):
                raise RuntimeError("fixture bus has no " + name)
            return method

    class MainLoop:
        def __init__(self):
            self.quit_event = threading.Event()
            record["loop"] = self

        def run(self):
            record["running"].set()
            self.quit_event.wait(5)

        def quit(self):
            self.quit_event.set()

    dbus = types.ModuleType("dbus")
    dbus.bus = SimpleNamespace(BusConnection=BusConnection, BUS_SYSTEM="system")
    dbus.Interface = Interface

    def no_shared_bus(*args, **kwargs):
        raise AssertionError("the monitor must not use the process-shared SystemBus")
    dbus.SystemBus = no_shared_bus
    mainloop = types.ModuleType("dbus.mainloop")
    glib = types.ModuleType("dbus.mainloop.glib")
    glib.DBusGMainLoop = lambda set_as_default=False: "adapter"
    gi = types.ModuleType("gi")
    repository = types.ModuleType("gi.repository")
    repository.GLib = SimpleNamespace(MainLoop=MainLoop)
    return {"dbus": dbus, "dbus.mainloop": mainloop, "dbus.mainloop.glib": glib,
            "gi": gi, "gi.repository": repository}


class MonitorConnectionTests(unittest.TestCase):
    """S-R5: a private connection per monitor run; quit on Disconnected."""

    def test_private_connection_quits_and_closes_on_disconnect(self):
        record = {}
        with mock.patch.dict(sys.modules, fake_dbus_modules(record)), \
                self.assertLogs("input-lock-daemon", "WARNING") as logs:
            events = []
            monitor = LogindMonitor(1000, events.append)
            monitor.REFRESH_SECONDS = 0.01
            record["running"] = threading.Event()
            monitor.start()
            self.assertTrue(record["running"].wait(2))
            signal_bus = record["buses"][0]
            self.assertEqual(signal_bus.mainloop, "adapter")
            self.assertEqual(signal_bus.address, "system")
            self.assertFalse(signal_bus.exit_on_disconnect)
            names = {kwargs["signal_name"] for _handler, kwargs in signal_bus.receivers}
            self.assertEqual(names, {"PrepareForSleep", "PropertiesChanged", "SessionNew", "SessionRemoved"})
            # Refresh calls run on other connections (no main loop).
            deadline = time.monotonic() + 2
            while len(record["buses"]) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertIsNone(record["buses"][1].mainloop)
            signal_bus.on_disconnect[0](signal_bus)       # dbus-broker restarted
            monitor.join(3)
        self.assertFalse(monitor.is_alive())
        self.assertTrue(signal_bus.closed)
        self.assertEqual(events[-1], "logind-monitor-stopped")
        self.assertIn("LOGIND_BUS_DISCONNECTED", "\n".join(logs.output))

    def test_every_restart_uses_a_new_connection(self):
        record = {}
        with mock.patch.dict(sys.modules, fake_dbus_modules(record)), \
                self.assertLogs("input-lock-daemon", "WARNING"):
            first_buses = []
            for _ in range(2):
                monitor = LogindMonitor(1000, lambda event: None)
                monitor.REFRESH_SECONDS = 0.01
                record["running"] = threading.Event()
                before = len(record.get("buses", []))
                monitor.start()
                self.assertTrue(record["running"].wait(2))
                first_buses.append(record["buses"][before])
                monitor.stop()
                monitor.join(3)
        self.assertIsNot(first_buses[0], first_buses[1])
        self.assertTrue(all(bus.closed for bus in first_buses))


# --------------------------------------------------------------------- S-R4


class Rig:
    """1 s daemon ticks, a new Sample every 2 s (as in the safety overhaul tests)."""

    def __init__(self, extra=()):
        self.now = 1000.0
        self.policy = SafetyPolicy(lambda: self.now)
        self.extra = list(extra)
        for _ in range(5):
            self.deliver(self.sample(70, 70, closed=False))
        self.policy.arm(replace(self.sample(70, 70, closed=False), at=self.now))

    def sample(self, tcpu, pkg, closed=True, sen1=45.0, sen2=45.0, nvme=None, chassis_present=True):
        sensors = [cpu("thermal:TCPU", tcpu), cpu("thermal:x86_pkg_temp", pkg),
                   cpu("hwmon:coretemp:fixture", pkg)]
        if chassis_present:
            sensors += [chassis("thermal:SEN1", sen1), chassis("thermal:SEN2", sen2)]
        if nvme is not None:
            sensors.append(Sensor("hwmon:nvme:fixture", "storage", nvme, 77, 80, 87.85))
        return Sample(self.now + 1, closed, sensors, True, 80.0, False, 1, 0.1, True, True)

    def deliver(self, sample):
        for _ in range(2):
            self.now += 1
            reason = self.policy.update(sample)
            if reason:
                return reason
        return None

    def run(self, seconds, **kwargs):
        start = self.now
        while self.now - start < seconds:
            reason = self.deliver(self.sample(**kwargs))
            if reason:
                return reason, self.now - start
        return None, self.now - start


class IndependentWitnessTests(unittest.TestCase):
    """S-R4: TCPU, x86_pkg_temp and coretemp package never confirm each other."""

    def test_origin_groups(self):
        self.assertEqual(safety.sensor_origin(cpu("thermal:TCPU", 1)), "cpu-package")
        self.assertEqual(safety.sensor_origin(cpu("thermal:x86_pkg_temp", 1)), "cpu-package")
        self.assertEqual(safety.sensor_origin(Sensor("hwmon:coretemp:x:cores", "cpu-core", 1, 2, 3, 4)),
                         "cpu-package")
        self.assertEqual(safety.sensor_origin(chassis("thermal:SEN1", 1)), "thermal:SEN1")

    def test_same_package_readings_are_not_an_emergency_witness(self):
        rig = Rig()
        # 107.5 on all three package readings, chassis cool: not an emergency
        # after 3 samples; the protective rule acts after 60 s.
        reason, elapsed = rig.run(20, tcpu=107.5, pkg=107.5)
        self.assertIsNone(reason)
        reason, elapsed = rig.run(60, tcpu=107.5, pkg=107.5)
        self.assertEqual(reason, "THERMAL_HIGH_RISK")

    def test_chassis_witness_at_its_warning_level_confirms(self):
        rig = Rig()
        reason, elapsed = rig.run(20, tcpu=107.5, pkg=107.5, sen1=65.0)
        self.assertEqual(reason, "THERMAL_EMERGENCY")
        self.assertLessEqual(elapsed, 8)
        evidence = rig.policy.last_decision["thermal_evidence"]["rule"]
        self.assertEqual(evidence["rule"], "cpu-corroborated-emergency")
        self.assertEqual(evidence["window"][-1]["witness"], "thermal:SEN1")
        self.assertEqual(evidence["window"][-1]["witness_warning_c"], 65)
        self.assertNotIn("corroboration_c", evidence)

    def test_chassis_below_warning_is_no_witness(self):
        rig = Rig()
        self.assertIsNone(rig.run(20, tcpu=107.5, pkg=107.5, sen1=64.9, sen2=64.9)[0])

    def test_nvme_witness_confirms(self):
        rig = Rig()
        reason, elapsed = rig.run(20, tcpu=107.5, pkg=95, nvme=77.0)
        self.assertEqual(reason, "THERMAL_EMERGENCY")
        self.assertEqual(rig.policy.last_decision["thermal_evidence"]["rule"]["window"][-1]["witness"],
                         "hwmon:nvme:fixture")

    def test_without_an_independent_group_only_the_108_rule_applies(self):
        rig = Rig()
        reason, elapsed = rig.run(8, tcpu=109, pkg=109, chassis_present=False)
        self.assertIsNone(reason)
        reason, elapsed = rig.run(30, tcpu=109, pkg=109, chassis_present=False)
        self.assertEqual(reason, "THERMAL_EMERGENCY")
        self.assertEqual(rig.policy.last_decision["thermal_evidence"]["rule"]["rule"],
                         "cpu-single-source-extreme")

    def test_flapping_100_108_is_kept_as_documented(self):
        # S-R8d, lead decision: keep the behaviour, document it.
        rig = Rig()
        start = rig.now
        reason = None
        index = 0
        while rig.now - start < 200 and reason is None:
            value = 100 if index % 2 else 108
            reason = rig.deliver(rig.sample(value, value))
            index += 1
        self.assertIsNone(reason)
        self.assertEqual(rig.policy.state, "LID_CLOSED_WARNING")
        doc = (REPO / "docs" / "independent-lid-protection.md").read_text()
        self.assertIn("100/108 °C", doc)
        self.assertIn("lead decision", doc)


# --------------------------------------------------------------------- S-R6


class BoundedReadTests(unittest.TestCase):
    """S-R6: root never follows, blocks on, or reads unbounded daemon files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_regular_file_symlink_fifo_size_and_missing(self):
        good = self.dir / "safety.json"
        good.write_text(json.dumps({"effective": True}))
        self.assertEqual(safety._read_bounded_json(good), {"effective": True})
        link = self.dir / "link.json"
        link.symlink_to(good)
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            safety._read_bounded_json(link)
        fifo = self.dir / "fifo.json"
        os.mkfifo(fifo)
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            safety._read_bounded_json(fifo)
        self.assertLess(time.monotonic() - started, 1)
        big = self.dir / "big.json"
        big.write_text('"' + "x" * 2048 + '"')
        with self.assertRaisesRegex(ValueError, "larger than"):
            safety._read_bounded_json(big, limit=1024)
        self.assertEqual(safety.MAX_STATE_FILE_BYTES, 1 << 20)
        with self.assertRaises(FileNotFoundError):
            safety._read_bounded_json(self.dir / "absent.json")

    def test_recovery_never_trusts_the_daemon_receipt_copy(self):
        journal = self.dir / "safety.json"
        (self.dir / safety.RESULT_NAME).write_text(json.dumps(
            {"boot_id": "b", "generation": 3, "transition_confirmed": True}))
        with mock.patch.object(safety, "ROOT_RECEIPT", self.dir / "root" / safety.RESULT_NAME):
            self.assertIsNone(safety._read_receipt(journal))
            self.assertEqual(safety._read_receipt(journal, trust_daemon_copy=True)["generation"], 3)

    def test_record_failure_ignores_a_symlinked_receipt(self):
        target = self.dir / "elsewhere.json"
        target.write_text(json.dumps({"boot_id": "b", "dispatch_accepted": True}))
        receipt = self.dir / "receipt.json"
        receipt.symlink_to(target)
        with self.assertLogs("input-lock-safety", "WARNING"):
            outcome = safety.record_failure("exit-code", latch=self.dir / "failure.json", receipt=receipt,
                                            counter=lambda: 1, boot=lambda: "b")
        self.assertEqual(outcome, "not-latched")
        self.assertFalse((self.dir / "failure.json").exists())

    def test_protect_refuses_a_symlinked_journal_without_dispatch(self):
        target = self.dir / "planted.json"
        target.write_text(json.dumps({"safety": {"state": "PROTECTIVE_ACTION_PENDING"}}))
        journal = self.dir / "safety.json"
        journal.symlink_to(target)
        records, dispatched = [], []
        with mock.patch.object(safety, "ROOT_RECEIPT", self.dir / "root.json"), \
                mock.patch.object(safety, "atomic_json", side_effect=lambda p, v, **k: records.append(v)), \
                self.assertLogs("input-lock-safety", "ERROR"):
            result = safety.protect_main(mock.Mock(), lambda: True, mock.Mock(), journal,
                                         mock.Mock(), lambda: 1, recovery=False,
                                         status=mock.Mock(), latch=self.dir / "failure.json",
                                         dispatch=lambda: dispatched.append(1))
        self.assertEqual(result, 1)
        self.assertEqual(dispatched, [])
        self.assertEqual(records[-1]["result"], "recovery-required")
        self.assertIn("not a regular file", records[-1]["detail"])


# --------------------------------------------------------------------- S-R7


class RecoveryRequiredTests(unittest.TestCase):
    """S-R7: "terminate for recovery" is never swallowed by a request handler."""

    def stuck_lid_daemon(self, clock):
        daemon = armed_daemon(clock)
        daemon.lid_is_closed = True
        daemon.safety_sample.lid_closed = True
        lid = daemon.devices["lid"]
        lid.device.ungrab.side_effect = OSError("ungrab failed")
        lid.device.close.side_effect = OSError("close failed")
        del daemon._emit_state_event          # the real method
        controller = daemon.clients[3]
        controller.sock.send.side_effect = BrokenPipeError()
        daemon.clients[5] = ClientState(mock.Mock(), 1000, pid=55)
        return daemon

    def test_failed_release_while_closing_the_controller_inside_a_request(self):
        clock = Clock(100.0)
        daemon = self.stuck_lid_daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "request_service") as request, \
                self.assertLogs("input-lock-daemon", "ERROR"):
            with self.assertRaises(RecoveryRequired):
                daemon._handle_request(5, {"cmd": "unlock", "mode": "keyboard"})
        request.assert_not_called()
        self.assertNotIn(3, daemon.clients)
        self.assertIsInstance(RecoveryRequired("x"), TransitionError)

    def test_hotkey_and_idle_lock_handlers_re_raise(self):
        clock = Clock(100.0)
        daemon = base_daemon(clock)
        daemon._emergency_unlock = mock.Mock(side_effect=RecoveryRequired("stuck"))
        daemon.pending_hotkey = daemon_module.PendingHotkey(
            "emergency-unlock", "all-input", frozenset(), "emergency-hotkey", 0.0)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            with self.assertRaises(RecoveryRequired):
                daemon._maybe_fire_pending_hotkey()
        daemon = base_daemon(clock)
        daemon.locked_kinds = set()
        daemon.idle_lock_enabled = True
        daemon.last_input_activity = 0.0
        daemon._set_locked_kinds = mock.Mock(side_effect=RecoveryRequired("stuck"))
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            with self.assertRaises(RecoveryRequired):
                daemon._handle_timers()


# --------------------------------------------------------------------- S-R8


class MinorRootItemsTests(unittest.TestCase):
    def test_a_our_protective_suspend_keeps_its_pending_state_through_prepare_sleep(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon.lid_is_closed = True
        daemon.safety_sample.lid_closed = True
        self.assertTrue(daemon.config.unlock_on_suspend)
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "request_service"), \
                mock.patch.object(daemon_module.threading, "Thread"), \
                mock.patch.object(daemon_module, "sd_notify"):
            daemon._start_protection("THERMAL_EMERGENCY")
            self.assertEqual(daemon.safety.state, "PROTECTIVE_ACTION_PENDING")
            daemon._write_safety.reset_mock()
            drain(daemon, "prepare-sleep")          # PrepareForSleep of our own suspend
            daemon._send_and_close = mock.Mock()
            root_client(daemon)
            daemon._handle_request(9, {"cmd": "prepare-sleep"})   # and the hook
        self.assertEqual(daemon.safety.state, "PROTECTIVE_ACTION_PENDING")
        self.assertEqual(daemon.safety.reason, "THERMAL_EMERGENCY")
        daemon._write_safety.assert_not_called()

    def lid_telemetry_case(self, readable):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon.lid_is_closed = True
        daemon.safety_sample.lid_closed = True
        lid = daemon.devices["lid"]
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "sd_notify"):
            drain(daemon, "prepare-sleep")
            daemon._start_protection("LID_TELEMETRY_LOST")
            self.assertEqual(daemon.deferred_protection, "LID_TELEMETRY_LOST")
            drain(daemon, "resume-sleep")
            # P2-1: the switch state is read with EVIOCGSW on the device's fd
            # (python3-evdev 1.9 has no InputDevice.switches()).
            lid.device = SimpleNamespace(fd=57)
            calls = []

            def ioctl(fd, request, buffer, mutate=True):
                calls.append((fd, request, len(buffer), mutate))
                if not readable:
                    raise OSError(19, "No such device")
                buffer[0] = 0x01                      # SW_LID set: closed
                return 0
            daemon._start_protection = mock.Mock()
            daemon.clients[3].deadline = 1e9
            clock.now = 100.0 + daemon_module.CONTROLLER_TIMEOUT + 1
            with mock.patch.object(daemon_module.fcntl, "ioctl", ioctl):
                daemon._handle_timers()
        self.assertEqual(calls, [(57, daemon_module.eviocgsw(3), 3, True)])
        return daemon

    def test_b_deferred_lid_telemetry_loss_is_rechecked(self):
        daemon = self.lid_telemetry_case(readable=False)
        daemon._start_protection.assert_called_once_with("LID_TELEMETRY_LOST")
        daemon = self.lid_telemetry_case(readable=True)
        daemon._start_protection.assert_not_called()
        self.assertTrue(daemon.ignore_lid_close)

    def test_c_helper_units_tolerate_a_missing_state_directory(self):
        for name in ("input-lock-protect.service", "input-lock-failsafe.service"):
            text = (REPO / "systemd" / name).read_text()
            self.assertRegex(text, r"(?m)^ReadWritePaths=-/var/lib/input-lock$")
            self.assertIsNone(re.search(r"(?m)^ReadWritePaths=/", text), name)
        self.assertNotIn("ReadWritePaths", safety.UNIT_PROPERTIES)


# --------------------------------------------------------------------- U-R1


class ControllerFinalEventTests(unittest.TestCase):
    """U-R1: the tray gets the final state event before its socket closes."""

    def setUp(self):
        self.clock = Clock(100.0)
        self.daemon = armed_daemon(self.clock)
        self.server_side, self.peer = socket.socketpair()
        self.server_side.setblocking(False)
        self.peer.settimeout(1)
        self.daemon.clients[3].sock = self.server_side
        del self.daemon._emit_state_event          # the real method

    def tearDown(self):
        self.server_side.close()
        self.peer.close()

    def received(self):
        data = b""
        while True:
            chunk = self.peer.recv(65536)
            if not chunk:
                return [json.loads(line) for line in data.splitlines() if line.strip()]
            data += chunk

    def expire(self):
        with mock.patch.object(daemon_module.time, "monotonic", self.clock), \
                mock.patch.object(daemon_module, "request_service"), \
                mock.patch.object(daemon_module.threading, "Thread"):
            self.clock.now = 136.0
            self.daemon._handle_timers()

    def test_lease_expiry_with_open_lid(self):
        self.daemon.safety_sample.lid_closed = False
        self.expire()
        self.assertNotIn(3, self.daemon.clients)
        messages = self.received()                   # ends with EOF
        self.assertEqual(len(messages), 1)
        disable = messages[0]["state"]["last_lid_disable"]
        self.assertEqual(disable["reason"], "controller-lease-expired")
        self.assertFalse(disable["requested_by_user"])
        self.assertFalse(disable["user_initiated"])
        self.assertFalse(messages[0]["state"]["ignore_lid_close"])

    def test_lease_expiry_with_closed_lid_protects_and_names_the_lease(self):
        self.daemon.lid_is_closed = True
        self.daemon.safety_sample.lid_closed = True
        self.expire()
        self.assertEqual(self.daemon.protective_result["reason"], "CONTROLLER_LOST")
        messages = self.received()
        self.assertEqual(messages[-1]["state"]["last_lid_disable"]["reason"], "controller-lease-expired")

    def test_broken_connection_keeps_controller_disconnected(self):
        self.daemon.safety_sample.lid_closed = False
        self.peer.close()
        with mock.patch.object(daemon_module.time, "monotonic", self.clock):
            self.daemon._read_client(3)
        self.assertNotIn(3, self.daemon.clients)
        self.assertEqual(self.daemon.last_lid_disable["reason"], "controller-disconnected")

    def test_final_event_is_sent_even_without_a_lid_change(self):
        self.daemon._disable_lid("lid-ignore-disabled", requested_by_user=True)
        # Drain what the disable already sent.
        self.peer.settimeout(0.2)
        try:
            self.peer.recv(65536)
        except socket.timeout:
            pass
        self.peer.settimeout(1)
        self.expire()
        messages = self.received()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["reason"], "controller-lease-expired")
        self.assertEqual(messages[0]["state"]["last_lid_disable"]["reason"], "lid-ignore-disabled")


# --------------------------------------------------------------------- U-R5


class UserInitiatedTests(unittest.TestCase):
    """U-R5 / interface 2: user_initiated vs requested_by_user."""

    def disable_with(self, action):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon._persist_user_settings = mock.Mock()
        daemon._send_and_close = mock.Mock()
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "request_service"), \
                mock.patch.object(daemon_module.threading, "Thread"), \
                mock.patch.object(daemon_module, "sd_notify"):
            action(daemon)
        self.assertFalse(daemon.ignore_lid_close)
        return daemon.last_lid_disable

    def reload(self, daemon, *, ipc):
        daemon._rescan_devices = mock.Mock()
        daemon.config_path = Path("/nonexistent/input-lock-fixture.conf")   # load_config is patched
        daemon._load_primary_hotkey_override = lambda fallback, emergency: fallback
        if ipc:
            daemon._handle_request(3, {"cmd": "reload"})
        with mock.patch.object(daemon_module, "load_config", return_value=daemon.config):
            daemon._reload_config()

    def test_user_initiated_paths(self):
        cases = {
            "set-lid-ignore false": lambda d: d._handle_request(3, {"cmd": "set-lid-ignore", "enabled": False}),
            "emergency hotkey": lambda d: d._emergency_unlock("emergency-hotkey"),
            "gnome emergency": lambda d: d._emergency_unlock("gnome-emergency-hotkey"),
            "tty recovery": lambda d: d._emergency_unlock("tty-recovery-hotkey"),
            "ipc emergency (user)": lambda d: d._handle_request(3, {"cmd": "emergency-unlock"}),
            "ipc emergency (installer, root)": lambda d: (root_client(d), d._handle_request(9, {"cmd": "emergency-unlock"})),
            "hotkey": lambda d: d._set_primary_hotkey("CTRL+ALT+K"),
            "lock kinds": lambda d: (setattr(d, "locked_kinds", {"keyboard"}), d._set_default_lock_kinds({"keyboard"})),
            "allowed keys": lambda d: (setattr(d, "locked_kinds", {"keyboard"}), d._set_allowed_keys(())),
            "ipc reload": lambda d: self.reload(d, ipc=True),
        }
        for name, action in cases.items():
            with self.subTest(name):
                disable = self.disable_with(action)
                self.assertTrue(disable["user_initiated"], disable)
                self.assertEqual(disable["requested_by_user"], name == "set-lid-ignore false")

    def test_not_user_initiated_paths(self):
        cases = {
            "controller lost": lambda d: d._close_client(3),
            "lease expired": lambda d: d._close_client(3, lease_expired=True),
            "session inactive": lambda d: drain(d, "session-inactive"),
            "protective": lambda d: d._start_protection("THERMAL_EMERGENCY"),
            "sighup reload": lambda d: self.reload(d, ipc=False),
            "daemon shutdown": lambda d: d._emergency_unlock("daemon-shutdown"),
            "auto unlock": lambda d: d._emergency_unlock("auto-unlock-timer"),
        }
        for name, action in cases.items():
            with self.subTest(name):
                disable = self.disable_with(action)
                self.assertFalse(disable["user_initiated"], disable)
                self.assertFalse(disable["requested_by_user"], disable)

    def test_log_line_carries_both_flags(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        with self.assertLogs("input-lock-daemon", "INFO") as logs:
            daemon._emergency_unlock("emergency-hotkey")
        self.assertIn("reason=emergency-unlock requested_by_user=False user_initiated=True",
                      "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
