"""Final independent review, sleep side (P1-1, P3-2).

P1-1: a failing suspend makes several freeze attempts inside one
PrepareForSleep window; kernel counters alone are no resume evidence, the
watchdog is never restored while logind still prepares, and the window cap is
derived at runtime (150 s on this host: 30 s InhibitDelayMax + 60 s + 3 x 20 s
pm_freeze_timeout). P3-2: stale logind monitor generations and a missed
PrepareForSleep(false).

Simulations only: fake clocks and sysfs, a recording sd_notify. Nothing talks
to a real bus, notify socket, device or suspend.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from dataclasses import asdict  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from test_daemon_logic import daemon_module  # noqa: E402  (installs evdev/pyudev stubs if needed)
from test_daemon_overhaul_20261022 import Clock, base_daemon, drain, make_sample  # noqa: E402
from test_review2_root_20261022 import SuspendStats, root_client  # noqa: E402
from input_lock_daemon import LogindMonitor  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
_ENVIRONMENT = mock.patch.dict(os.environ)
RESTORE = ["WATCHDOG_USEC=60000000", "WATCHDOG=1"]


def setUpModule():
    # Never let a test talk to a real service manager's notify socket.
    _ENVIRONMENT.start()
    os.environ.pop("NOTIFY_SOCKET", None)


def tearDownModule():
    _ENVIRONMENT.stop()


def sample_event(at: float, preparing: bool | None) -> str:
    """A telemetry sample as the telemetry thread posts it."""
    sample = make_sample(at)
    sample.preparing = preparing
    return "safety-sample:" + json.dumps(asdict(sample))


class FakeMonitor:
    """The parts of LogindMonitor the daemon uses for the sleep window."""

    def __init__(self, inhibit_delay_max_seconds=30.0):
        self.inhibit_delay_max_seconds = inhibit_delay_max_seconds
        self.sleep_finished = mock.Mock()


class SleepFixture(unittest.TestCase):
    """Fake clock, fake sysfs (pm_freeze_timeout 20 s, suspend counters),
    logind InhibitDelayMax 30 s and a recording sd_notify."""

    def setUp(self):
        self.clock = Clock(1000.0)
        self.stats = SuspendStats(success=40, fail=7)
        self.addCleanup(self.stats.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.freeze = Path(tmp.name) / "pm_freeze_timeout"
        self.freeze.write_text("20000\n")
        self.notify = []
        for patch in (mock.patch.object(daemon_module.time, "monotonic", self.clock),
                      mock.patch.object(daemon_module, "PM_FREEZE_TIMEOUT_PATH", self.freeze),
                      mock.patch.object(daemon_module, "sd_notify",
                                        side_effect=lambda message: self.notify.append(message) or True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.daemon = base_daemon(self.clock)
        self.monitor = FakeMonitor()
        self.daemon.logind_monitor = self.monitor
        self.restored_at = None
        self.window_closed_at = None

    def messages(self):
        out, self.notify[:] = list(self.notify), []
        return out

    def advance(self, until, *, preparing, step=2.0):
        """Run the daemon second by second up to ``until``, with a telemetry
        sample every ``step`` seconds, recording when the window closed and
        when the watchdog was restored."""
        while self.clock.now < until:
            self.clock.now = min(until, self.clock.now + 1.0)
            if preparing != "none" and (self.clock.now - 1000.0) % step == 0:
                drain(self.daemon, sample_event(self.clock.now, preparing))
            self.tick()

    def tick(self):
        self.daemon.next_safety_tick = 0
        self.daemon._handle_timers()
        if self.window_closed_at is None and self.daemon.sleep_window_since is None:
            self.window_closed_at = self.clock.now
        if self.restored_at is None and "WATCHDOG_USEC=60000000" in self.notify:
            self.restored_at = self.clock.now

    def freeze_attempt(self, seconds=20.0):
        """The kernel freezer stops the process; the attempt fails and the
        fail counter goes up. Nothing runs during the freeze."""
        self.clock.now += seconds
        success, fail = daemon_module.InputLockDaemon._suspend_stats()
        self.stats.set(success, fail + 1)
        self.tick()


class FailingSuspendReplayTests(SleepFixture):
    """P1-1 (e): today's sequence, two failing attempts in one window."""

    def open_window(self):
        drain(self.daemon, "prepare-sleep")
        self.assertEqual(self.messages(), ["WATCHDOG=1", "WATCHDOG_USEC=300000000"])
        self.assertEqual(self.daemon.sleep_window_cap, 150.0)
        self.assertEqual(self.daemon.sleep_window_since, 1000.0)
        self.advance(1010.0, preparing=True)
        self.freeze_attempt()                 # attempt 1 fails at ~1030 (+1)
        self.advance(1040.0, preparing=True)
        self.freeze_attempt()                 # 20 s gap, attempt 2 fails (+1)
        self.advance(1104.0, preparing=True)  # still preparing ~105 s in
        self.assertEqual(self.daemon._suspend_stats(), (40, 9))
        self.assertIsNone(self.window_closed_at, "a counter change closed the window")
        self.assertIsNone(self.restored_at, "watchdog restored while logind was preparing")
        self.assertNotIn("WATCHDOG_USEC=60000000", self.notify)

    def test_window_and_watchdog_cover_the_whole_failing_suspend(self):
        self.open_window()
        self.clock.now = 1105.0
        drain(self.daemon, "resume-sleep")        # PreparingForSleep(false)
        self.tick()
        self.assertEqual(self.window_closed_at, 1105.0)
        self.assertEqual(self.restored_at, 1105.0)
        sent = self.messages()
        self.assertEqual(sent.count("WATCHDOG_USEC=60000000"), 1)
        restore = sent.index("WATCHDOG_USEC=60000000")
        self.assertEqual(sent[restore:restore + 2], RESTORE)
        self.monitor.sleep_finished.assert_called_once_with(1105.0)

    def test_missed_signal_post_hook_closes_window_but_watchdog_waits(self):
        self.open_window()
        self.daemon._send_and_close = mock.Mock()
        root_client(self.daemon)
        self.clock.now = 1104.5
        self.daemon._handle_request(9, {"cmd": "resume-sleep"})   # 'post' hook
        self.assertIsNone(self.daemon.sleep_window_since)
        self.assertNotIn("WATCHDOG_USEC=60000000", self.notify)
        self.monitor.sleep_finished.assert_not_called()
        # PrepareForSleep(false) is missed; logind keeps reporting preparing
        # for a while, then false.
        self.advance(1110.0, preparing=True)
        self.assertNotIn("WATCHDOG_USEC=60000000", self.notify)
        self.advance(1112.0, preparing=False)
        self.assertEqual(self.restored_at, 1112.0)
        self.monitor.sleep_finished.assert_called_once_with(1112.0)

    def test_suspend_stats_with_logind_not_preparing_is_resume_evidence(self):
        self.open_window()
        self.clock.now = 1105.0
        drain(self.daemon, sample_event(1105.0, False))   # signal missed
        with self.assertLogs("input-lock-daemon", "INFO") as logs:
            self.tick()
        self.assertEqual(self.window_closed_at, 1105.0)
        self.assertEqual(self.restored_at, 1105.0)
        self.assertIn("source=resume-evidence:suspend-stats", "\n".join(logs.output))
        self.monitor.sleep_finished.assert_called_once_with(1105.0)

    def test_preparing_past_the_cap_keeps_the_watchdog_extended(self):
        drain(self.daemon, "prepare-sleep")
        self.messages()
        self.freeze_attempt()
        self.advance(1250.0, preparing=True)
        # Closed for supervision at the cap, but logind still prepares.
        self.assertEqual(self.window_closed_at, 1150.0)
        self.assertIsNone(self.restored_at)
        self.assertEqual(self.daemon.safety.sleep_window_since, None)
        self.advance(1252.0, preparing=False)
        self.assertEqual(self.restored_at, 1252.0)
        self.monitor.sleep_finished.assert_called_once_with(1252.0)

    def test_unknown_preparing_keeps_the_watchdog_until_the_cap(self):
        drain(self.daemon, "prepare-sleep")
        self.messages()
        self.freeze_attempt()
        self.advance(1149.0, preparing=None)     # the probe answers "unknown"
        self.assertIsNone(self.window_closed_at)
        self.assertIsNone(self.restored_at)
        self.advance(1151.0, preparing=None)
        self.assertEqual(self.window_closed_at, 1150.0)
        self.assertEqual(self.restored_at, 1150.0)
        # Unknown is not "false": the monitor is not told the sleep ended.
        self.monitor.sleep_finished.assert_not_called()

    def test_an_older_sample_never_overrides_the_signal(self):
        drain(self.daemon, "prepare-sleep")
        self.messages()
        # A sample read just before PrepareForSleep(true), delivered after it.
        drain(self.daemon, sample_event(999.5, False))
        self.clock.now = 1001.0
        self.tick()
        self.assertIs(self.daemon.logind_preparing, True)
        self.assertIsNotNone(self.daemon.sleep_window_since)
        self.assertIsNone(self.restored_at)

    def test_reentry_while_still_extended_keeps_one_extension(self):
        self.open_window()
        self.daemon._send_and_close = mock.Mock()
        root_client(self.daemon)
        self.daemon._handle_request(9, {"cmd": "resume-sleep"})   # 'post' hook
        self.clock.now = 1106.0
        drain(self.daemon, "prepare-sleep")      # the next suspend starts
        self.assertIsNotNone(self.daemon.sleep_window_since)
        self.assertEqual(self.daemon.watchdog_extended_since, 1000.0)
        self.clock.now = 1107.0
        drain(self.daemon, "resume-sleep")
        self.assertEqual(self.messages()[-2:], RESTORE)
        self.assertIsNone(self.daemon.watchdog_extended_since)


class WindowCapTests(SleepFixture):
    """P1-1 (c): InhibitDelayMax + 60 s + 3 x pm_freeze_timeout, 150-300 s."""

    def cap(self, inhibit, freeze_text):
        self.monitor.inhibit_delay_max_seconds = inhibit
        self.freeze.write_text(freeze_text)
        return self.daemon._sleep_window_cap()

    def test_formula_clamp_and_fallbacks(self):
        self.assertEqual(self.cap(30.0, "20000\n"), 150.0)        # this host: 30 + 60 + 3 x 20
        self.assertEqual(self.cap(5.0, "20000\n"), 150.0)         # 125 s -> 150 s
        self.assertEqual(self.cap(60.0, "20000\n"), 180.0)
        self.assertEqual(self.cap(30.0, "40000\n"), 210.0)
        self.assertEqual(self.cap(300.0, "20000\n"), 300.0)       # 420 s -> 300 s
        self.assertEqual(self.cap(None, "40000\n"), 210.0)        # logind unknown: 30 s
        self.assertEqual(self.cap(90.0, "garbage"), 210.0)        # sysfs unreadable: 20 s
        self.assertEqual(self.cap(-1.0, "0"), 150.0)
        self.assertEqual(self.cap(True, "40000"), 210.0)
        self.freeze.unlink()
        self.daemon.logind_monitor = None
        self.assertEqual(self.daemon._sleep_window_cap(), 150.0)

    def test_cap_is_fixed_when_the_window_opens(self):
        self.monitor.inhibit_delay_max_seconds = 90.0
        drain(self.daemon, "prepare-sleep")
        self.assertEqual(self.daemon.sleep_window_cap, 210.0)
        self.monitor.inhibit_delay_max_seconds = 5.0
        self.clock.now = 1209.0
        self.tick()
        self.assertIsNotNone(self.daemon.sleep_window_since)
        self.clock.now = 1210.0
        self.tick()
        self.assertIsNone(self.daemon.sleep_window_since)

    def test_monitor_reads_inhibit_delay_max_off_the_main_loop(self):
        monitor = LogindMonitor(1000, lambda _event: None)
        monitor.refresh_inhibit_delay_with(SimpleNamespace(inhibit_delay_max_usec=lambda: 30_000_000))
        self.assertEqual(monitor.inhibit_delay_max_seconds, 30.0)

        def broken():
            raise RuntimeError("bus gone")
        fresh = LogindMonitor(1000, lambda _event: None)
        fresh.refresh_inhibit_delay_with(SimpleNamespace(inhibit_delay_max_usec=broken))
        fresh.refresh_inhibit_delay_with(SimpleNamespace())
        fresh.refresh_inhibit_delay_with(SimpleNamespace(inhibit_delay_max_usec=lambda: -5))
        self.assertIsNone(fresh.inhibit_delay_max_seconds)


# --------------------------------------------------------------------- P3-2


class MonitorGenerationTests(unittest.TestCase):
    """P3-2 (a): a retired monitor's late refresh round is dropped."""

    def test_events_of_an_older_generation_are_dropped(self):
        daemon = base_daemon()
        daemon.wakeup_write = mock.Mock()
        daemon._on_session_lost = mock.Mock()
        created = []

        class Monitor:
            def __init__(self, uid, callback):
                self.callback = callback
                created.append(self)

            def start(self):
                pass

        with mock.patch.object(daemon_module, "LogindMonitor", Monitor):
            daemon._start_logind_monitor()
            old = created[-1].callback
            old("session-active")
            daemon._drain_wakeup()
            self.assertTrue(daemon.authorized_session_active)
            old("logind-monitor-stopped")
            # The worker, joined for only 1 s, finishes one more round.
            old("session-inactive")
            daemon._drain_wakeup()
            daemon._on_session_lost.assert_not_called()
            daemon._start_logind_monitor()        # the scheduled restart
            old("session-inactive")
            old("prepare-sleep")
            daemon._drain_wakeup()
            daemon._on_session_lost.assert_not_called()
            self.assertIsNone(daemon.sleep_window_since)
            created[-1].callback("session-inactive")
            daemon._drain_wakeup()
        daemon._on_session_lost.assert_called_once_with("session-inactive")

    def test_untagged_events_are_still_accepted(self):
        daemon = base_daemon()
        daemon.logind_generation = 7
        drain(daemon, "logind-healthy")
        self.assertGreater(daemon.last_logind_at, 0.0)


class MissedResumeSignalTests(unittest.TestCase):
    """P3-2 (b): PrepareForSleep(false) missed while the monitor survives."""

    def setUp(self):
        self.clock = Clock(500.0)
        for patch in (mock.patch.object(daemon_module.time, "monotonic", self.clock),
                      mock.patch.object(daemon_module, "sd_notify")):
            patch.start()
            self.addCleanup(patch.stop)

    def inhibit(self):
        fd = os.open(os.devnull, os.O_RDONLY)
        return fd

    def test_daemon_rearms_the_delay_inhibitor_once_logind_stopped_preparing(self):
        monitor = LogindMonitor(1000, lambda _event: None)
        self.assertTrue(monitor.acquire_delay_inhibitor(self.inhibit))
        monitor.prepare_for_sleep(True)          # releases the delay lock
        self.assertTrue(monitor._sleeping)
        self.assertFalse(monitor.acquire_delay_inhibitor(self.inhibit))
        daemon = base_daemon(self.clock)
        daemon.logind_monitor = monitor
        daemon._send_and_close = mock.Mock()
        root_client(daemon)
        drain(daemon, "prepare-sleep")
        self.clock.now = 560.0
        daemon._handle_request(9, {"cmd": "resume-sleep"})   # window closes ...
        self.assertIsNone(daemon.sleep_window_since)
        self.assertTrue(monitor._sleeping)                   # ... logind still preparing
        monitor._refresh_wanted.clear()
        self.clock.now = 562.0
        drain(daemon, sample_event(562.0, False))            # PrepareForSleep(false) missed
        self.assertFalse(monitor._sleeping)
        self.assertTrue(monitor._refresh_wanted.is_set())
        self.assertTrue(monitor.acquire_delay_inhibitor(self.inhibit))
        monitor.release_delay_inhibitor()

    def test_a_reading_older_than_the_latest_sleep_is_ignored(self):
        monitor = LogindMonitor(1000, lambda _event: None)
        monitor.prepare_for_sleep(True)
        monitor.sleep_finished(499.0)
        self.assertTrue(monitor._sleeping)
        monitor.sleep_finished(500.0)
        self.assertTrue(monitor._sleeping)
        monitor.sleep_finished(500.5)
        self.assertFalse(monitor._sleeping)

if __name__ == "__main__":
    unittest.main()
