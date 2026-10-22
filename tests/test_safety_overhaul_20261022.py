"""Safety overhaul (review 2026-10-22: A1, A2, A9-A13, S1, S5, S6, section F, G1).

Simulations only: fake clocks, temporary directories, injected systemctl,
logind and kernel counters. Nothing here touches a bus, a unit or real sleep.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import input_lock_safety as safety  # noqa: E402
from input_lock_safety import SafetyPolicy, Sample, Sensor, Telemetry  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def cpu(identity, celsius):
    """Thresholds Telemetry derives on the EliteBook (110C trip): 95/104/107."""
    return Sensor(identity, "cpu", celsius, 95, 104, 107)


def sensors(tcpu=70.0, pkg=70.0, sen1=45.0, sen2=45.0, nvme=None):
    rows = [cpu("thermal:TCPU", tcpu), cpu("thermal:x86_pkg_temp", pkg),
            Sensor("thermal:SEN1", "chassis", sen1, 65, 70, 73),
            Sensor("thermal:SEN2", "chassis", sen2, 65, 70, 73)]
    if nvme is not None:
        rows.append(Sensor("hwmon:nvme:fixture", "storage", nvme, 77, 80, 87.85))
    return [row for row in rows if row.celsius is not None]


def make(at, closed=True, **changes):
    base = Sample(at, closed, sensors(), True, 80., False, 1, .1, True, True)
    return replace(base, **changes)


class Rig:
    """A daemon-like driver: 1 s ticks, a new Sample every `step` seconds."""

    def __init__(self, test, arm=True):
        self.now = 1000.
        self.policy = SafetyPolicy(lambda: self.now)
        self.sample = None
        for _ in range(5):
            self.deliver(make(self.now, closed=False), ticks=2)
        if arm:
            self.policy.arm(make(self.now, closed=False))

    def deliver(self, sample, ticks=2):
        self.sample = sample
        for _ in range(ticks):
            self.now += 1
            reason = self.policy.update(sample)
            if reason:
                return reason
        return None

    def run(self, seconds, step=2, **changes):
        """Feed new samples for `seconds`; return (decision, seconds elapsed)."""
        start = self.now
        while self.now - start < seconds:
            reason = self.deliver(make(self.now + 1, **changes), ticks=step)
            if reason:
                return reason, self.now - start
        return None, self.now - start

    def run_sequence(self, rows, **changes):
        start = self.now
        for row in rows:
            reason = self.deliver(make(self.now + 1, sensors=row, **changes))
            if reason:
                return reason, self.now - start
        return None, self.now - start


class TickVersusSampleTests(unittest.TestCase):
    def test_one_spike_sample_held_across_many_ticks_never_acts(self):
        rig = Rig(self)
        spike = make(rig.now + 1, sensors=sensors(110, 110))
        self.assertIsNone(rig.deliver(spike, ticks=8))  # Daemon ticks; no new sample.
        self.assertEqual(rig.run(60)[0], None)
        self.assertTrue(rig.policy.armed)

    def test_repeated_ticks_do_not_count_as_dwell(self):
        rig = Rig(self)
        hot = make(rig.now + 1, sensors=sensors(106, 106))
        # 90 daemon ticks on one sample are one sample of evidence.
        self.assertIsNone(rig.deliver(hot, ticks=25))
        self.assertTrue(rig.policy.armed)
        self.assertEqual(rig.policy.thermal_level, 0)

    def test_two_corroborated_samples_are_not_enough_three_are(self):
        rig = Rig(self)
        # S-R4: the witness is an independent origin (SEN1 >= its 65C
        # warning), not a second reading of the same CPU package.
        rows = [sensors(108, 106, sen1=66)] * 2 + [sensors(70, 70)] * 5
        self.assertEqual(rig.run_sequence(rows)[0], None)
        reason, _ = rig.run_sequence([sensors(108, 106, sen1=66)] * 3)
        self.assertEqual(reason, "THERMAL_EMERGENCY")
        rule = rig.policy.last_decision["thermal_evidence"]["rule"]
        self.assertEqual(rule["rule"], "cpu-corroborated-emergency")
        self.assertEqual(len(rule["window"]), 3)

    def test_uncorroborated_extreme_needs_ten_seconds(self):
        rig = Rig(self)
        # Second source more than 6C below: not corroborated.
        reason, elapsed = rig.run(30, sensors=sensors(109, 95))
        self.assertEqual(reason, "THERMAL_EMERGENCY")
        self.assertGreaterEqual(elapsed, 10)
        self.assertEqual(rig.policy.last_decision["thermal_evidence"]["rule"]["rule"],
                         "cpu-single-source-extreme")

    def test_107_single_source_is_not_an_emergency(self):
        rig = Rig(self)
        # 107C on one source: not >=108 and not corroborated -> protective path only.
        reason, elapsed = rig.run(120, sensors=sensors(107, 95))
        self.assertEqual(reason, "THERMAL_HIGH_RISK")
        self.assertGreaterEqual(elapsed, 60)


class SustainedVersusSpikeTests(unittest.TestCase):
    def test_sustained_protective_heat_acts_only_after_sixty_seconds(self):
        rig = Rig(self)
        reason, _ = rig.run(55, sensors=sensors(105, 103))
        self.assertIsNone(reason)
        reason, _ = rig.run(30, sensors=sensors(105, 103))
        self.assertEqual(reason, "THERMAL_HIGH_RISK")
        evidence = rig.policy.last_decision["thermal_evidence"]
        self.assertEqual(evidence["rule"]["rule"], "cpu-protective")
        self.assertEqual(evidence["rule"]["basis"], "median-of-5")
        self.assertGreaterEqual(evidence["rule"]["elapsed_seconds"], 60)
        self.assertTrue(all(row["celsius"] >= 101 for row in evidence["rule"]["window"]))
        self.assertIn("median_c", evidence["sensors"][0])

    def test_isolated_spikes_inside_normal_load_never_act(self):
        rig = Rig(self)
        rows = ([sensors(98, 97)] * 4 + [sensors(110, 101)]) * 30
        self.assertEqual(rig.run_sequence(rows)[0], None)
        self.assertTrue(rig.policy.armed)

    def test_two_isolated_dips_are_tolerated(self):
        rig = Rig(self)
        # Raw 90,90,105,90 pulls the median of five below 101 for exactly two samples.
        hot, dip = sensors(105, 105), sensors(90, 90)
        rows = [hot] * 12 + [dip, dip, hot, dip] + [hot] * 30
        reason, elapsed = rig.run_sequence(rows)
        self.assertEqual(reason, "THERMAL_HIGH_RISK")
        self.assertLess(elapsed, 80)
        self.assertEqual(rig.policy.last_decision["thermal_evidence"]["rule"]["drops"], 2)

    def test_long_cool_period_restarts_the_protective_timer(self):
        rig = Rig(self)
        rows = [sensors(105, 105)] * 20 + [sensors(80, 80)] * 6 + [sensors(105, 105)] * 20
        self.assertEqual(rig.run_sequence(rows)[0], None)


class FlappingHeatTests(unittest.TestCase):
    """A2: a level counts while risk >= L, not == L; hysteresis spans flaps."""

    def test_flapping_across_the_warning_threshold_still_warns(self):
        for low, high in ((94, 101), (94, 96), (96, 101)):
            with self.subTest(low=low, high=high):
                rig = Rig(self)
                rows = [sensors(low, low), sensors(high, high)] * 30
                self.assertEqual(rig.run_sequence(rows)[0], None)
                self.assertEqual(rig.policy.thermal_level, 1)
                self.assertEqual(rig.policy.state, "LID_CLOSED_WARNING")

    def test_flapping_inside_the_protective_band_acts(self):
        rig = Rig(self)
        rows = [sensors(105, 105), sensors(102, 102)] * 50
        reason, elapsed = rig.run_sequence(rows)
        self.assertEqual(reason, "THERMAL_HIGH_RISK")
        self.assertLess(elapsed, 90)

    def test_chassis_and_storage_rules(self):
        rig = Rig(self)
        reason, elapsed = rig.run(80, sensors=sensors(sen2=70.5))
        self.assertEqual(reason, "THERMAL_HIGH_RISK")
        self.assertGreaterEqual(elapsed, 60)
        rig = Rig(self)
        reason, elapsed = rig.run(40, sensors=sensors(sen1=73.5))
        self.assertEqual(reason, "THERMAL_EMERGENCY")
        self.assertGreaterEqual(elapsed, 10)
        rig = Rig(self)
        self.assertEqual(rig.run(120, sensors=sensors(nvme=78))[0], None)
        self.assertEqual(rig.policy.thermal_level, 1)
        reason, elapsed = rig.run(40, sensors=sensors(nvme=80.5))
        self.assertEqual(reason, "THERMAL_HIGH_RISK")
        self.assertGreaterEqual(elapsed, 20)

    def test_cpu_core_readings_never_decide(self):
        rig = Rig(self)
        core = Sensor("hwmon:coretemp:fixture:cores", "cpu-core", 112, 95, 104, 107)
        self.assertEqual(rig.run(120, sensors=sensors() + [core])[0], None)


class BatteryTests(unittest.TestCase):
    def test_critical_battery_while_charging_does_not_suspend(self):
        rig = Rig(self)
        for changes in ({"battery": 5., "ac": True, "discharging": False},
                        {"battery": 5., "ac": True, "discharging": None}):
            self.assertEqual(rig.run(60, **changes)[0], None)
        self.assertTrue(rig.policy.armed)

    def test_critical_battery_while_draining_suspends(self):
        for changes in ({"battery": 5., "ac": False, "discharging": True},
                        {"battery": 5., "ac": None, "discharging": True},
                        {"battery": 5., "ac": False, "discharging": None}):
            with self.subTest(changes=changes):
                rig = Rig(self)
                reason, elapsed = rig.run(10, **changes)
                self.assertEqual(reason, "BATTERY_CRITICAL")
                self.assertGreaterEqual(elapsed, 2)


class TelemetryLossTests(unittest.TestCase):
    def test_partial_sensor_loss_is_not_telemetry_loss(self):
        rig = Rig(self)
        for row in ([cpu("thermal:x86_pkg_temp", 70)] + sensors()[2:],
                    sensors()[:2] + [sensors()[3]]):
            self.assertEqual(rig.run(120, sensors=row)[0], None)
        self.assertTrue(rig.policy.armed)
        self.assertEqual(rig.policy.state, "LID_CLOSED_MONITORED")

    def test_whole_cpu_category_loss_takes_thirty_seconds_of_samples(self):
        rig = Rig(self)
        reason, elapsed = rig.run(80, sensors=sensors()[2:])
        self.assertEqual(reason, "TELEMETRY_LOST")
        self.assertGreaterEqual(elapsed, 30)
        self.assertLessEqual(elapsed, 36)
        self.assertEqual(rig.policy.last_decision["detail"], {"missing_categories": ["cpu"]})

    def test_brief_category_gaps_restart_the_loss_timer(self):
        rig = Rig(self)
        for _ in range(4):
            self.assertEqual(rig.run(20, sensors=sensors()[2:])[0], None)
            self.assertEqual(rig.run(4)[0], None)
        self.assertTrue(rig.policy.armed)

    def test_frozen_sample_with_live_ticks_is_lost_after_thirty_seconds(self):
        rig = Rig(self)
        stale = make(rig.now + 1)
        self.assertIsNone(rig.deliver(stale, ticks=29))
        self.assertEqual(rig.deliver(stale, ticks=5), "TELEMETRY_LOST")
        self.assertIn("stale_seconds", rig.policy.last_decision["detail"])

    def test_readiness_probe_failure_or_hard_block_only_warns(self):
        rig = Rig(self)
        errors = ["ordinary suspend unavailable, blocked, or already in progress"]
        reason, _ = rig.run(600, stack_ready=False, protection_errors=errors)
        self.assertIsNone(reason)
        self.assertTrue(rig.policy.armed)
        self.assertEqual(rig.policy.state, "LID_CLOSED_WARNING")
        self.assertIn("SUPERVISION_DEGRADED", rig.policy.events)

    def test_unreadable_channel_error_only_warns(self):
        rig = Rig(self)
        self.assertEqual(rig.run(300, errors=["unreadable:coretemp:temp7"])[0], None)


class SampleGapTests(unittest.TestCase):
    """Daemon stops calling update() during PrepareForSleep .. resume + 35 s."""

    def test_gap_without_updates_is_neither_dwell_nor_loss(self):
        rig = Rig(self)
        self.assertEqual(rig.run(50, sensors=sensors(105, 105))[0], None)
        rig.now += 120  # No update() calls at all (suspend + controller grace).
        reason, elapsed = rig.run(20, sensors=sensors(105, 105))
        self.assertIsNone(reason)
        self.assertTrue(rig.policy.armed)
        self.assertIn("SAMPLE_GAP_RESET", rig.policy.events)
        # The protective timer restarted after the gap.
        reason, elapsed = rig.run(80, sensors=sensors(105, 105))
        self.assertEqual(reason, "THERMAL_HIGH_RISK")
        self.assertGreaterEqual(rig.policy.last_decision["thermal_evidence"]["rule"]["window"][-1]["at_offset"], 60)

    def test_gap_does_not_complete_a_category_loss(self):
        rig = Rig(self)
        self.assertEqual(rig.run(20, sensors=sensors()[2:])[0], None)
        rig.now += 300
        self.assertEqual(rig.run(20, sensors=sensors()[2:])[0], None)
        self.assertEqual(rig.run(20, sensors=sensors()[2:])[0], "TELEMETRY_LOST")

    def test_stale_sample_after_paused_ticks_gets_grace(self):
        rig = Rig(self)
        old = make(rig.now + 1)
        rig.deliver(old)
        rig.now += 90  # The daemon itself was frozen; the sample is 90 s old.
        self.assertIsNone(rig.deliver(old, ticks=3))
        self.assertIn("SUPERVISION_PAUSE_DETECTED", rig.policy.events)
        self.assertEqual(rig.run(10)[0], None)

    def test_medians_ignore_rows_from_before_a_gap(self):
        rig = Rig(self, arm=False)
        rig.run_sequence([sensors(106, 106)] * 5)
        rig.now += 60
        rig.deliver(make(rig.now + 1, closed=False, sensors=sensors(60, 60)))
        self.assertEqual(rig.policy.median("thermal:TCPU"), 60)

    def test_hot_battery_samples_across_a_gap_need_new_evidence(self):
        rig = Rig(self)
        draining = {"battery": 5., "ac": False, "discharging": True}
        rig.deliver(make(rig.now + 1, **draining))
        rig.now += 120
        self.assertIsNone(rig.deliver(make(rig.now + 1, **draining), ticks=1))
        self.assertTrue(rig.policy.armed)


class SleepWindowTests(unittest.TestCase):
    def test_preparing_suppresses_every_action(self):
        rig = Rig(self)
        for changes in ({"sensors": sensors(109, 109)}, {"sensors": []},
                        {"battery": 3., "ac": False, "discharging": True}):
            with self.subTest(changes=changes):
                rig = Rig(self)
                self.assertEqual(rig.run(200, preparing=True, **changes)[0], None)
                self.assertTrue(rig.policy.armed)
                self.assertTrue(rig.policy.snapshot()["os_sleep_in_progress"])

    def test_sustained_heat_is_acted_on_after_the_window_ends(self):
        rig = Rig(self)
        self.assertEqual(rig.run(80, preparing=True, sensors=sensors(105, 105))[0], None)
        reason, elapsed = rig.run(10, preparing=False, sensors=sensors(105, 105))
        self.assertEqual(reason, "THERMAL_HIGH_RISK")
        self.assertLessEqual(elapsed, 4)
        self.assertIn("OS_SLEEP_WINDOW_ENDED", rig.policy.events)

    def test_window_end_restarts_loss_timers(self):
        rig = Rig(self)
        self.assertEqual(rig.run(60, preparing=True, sensors=[])[0], None)
        reason, elapsed = rig.run(60, sensors=[])
        self.assertEqual(reason, "TELEMETRY_LOST")
        self.assertGreaterEqual(elapsed, 30)

    def test_window_is_bounded(self):
        rig = Rig(self)
        reason, elapsed = rig.run(400, preparing=True, sensors=sensors(105, 105))
        self.assertEqual(reason, "THERMAL_HIGH_RISK")
        self.assertGreater(elapsed, safety.SLEEP_WINDOW_MAX_SECONDS)
        self.assertIn("OS_SLEEP_WINDOW_EXPIRED", rig.policy.events)

    def test_daemon_hook_opens_and_closes_the_window(self):
        rig = Rig(self)
        rig.policy.note_sleep_window(True)
        self.assertEqual(rig.run(90, sensors=sensors(109, 109))[0], None)
        rig.policy.note_sleep_window(False)
        self.assertEqual(rig.run(6, sensors=sensors(109, 109))[0], "THERMAL_EMERGENCY")


class MeasuredThermalTests(unittest.TestCase):
    """G1: status/logging uses the same smoothed per-sensor view."""

    def states(self, rows):
        rig = Rig(self, arm=False)
        seen = []
        for row in rows:
            rig.deliver(make(rig.now + 1, closed=False, sensors=row))
            seen.append(rig.policy.snapshot()["thermal"])
        return seen

    @staticmethod
    def changes(seen):
        return sum(1 for a, b in zip(seen, seen[1:]) if a != b)

    def test_reading_hovering_at_a_threshold_does_not_flap(self):
        seen = self.states([sensors(94.5, 94), sensors(95.5, 95)] * 40)
        self.assertLessEqual(self.changes(seen), 1)

    def test_single_spike_does_not_change_the_measured_state(self):
        seen = self.states([sensors(80, 80)] * 6 + [sensors(110, 109)] + [sensors(80, 80)] * 6)
        self.assertEqual(set(seen), {"NORMAL"})

    def test_sustained_change_is_reported(self):
        seen = self.states([sensors(80, 80)] * 6 + [sensors(105, 105)] * 6)
        self.assertEqual(seen[-1], "HIGH_RISK")

    def test_partial_sample_stays_complete_while_categories_remain(self):
        rig = Rig(self, arm=False)
        rig.deliver(make(rig.now + 1, closed=False, sensors=sensors()[1:]))
        measured = rig.policy.measured_thermal()
        self.assertEqual(measured, {"state": "NORMAL", "fresh": True, "complete": True})


class TelemetryReadTests(unittest.TestCase):
    def tree(self, root):
        def write(path, text):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        for number, kind, temp, crit in ((1, "TCPU", "60000", "110050"), (2, "x86_pkg_temp", "61000", None),
                                         (3, "SEN1", "45000", "80050"), (4, "SEN2", "46000", "80050")):
            write(f"class/thermal/thermal_zone{number}/type", kind)
            write(f"class/thermal/thermal_zone{number}/temp", temp)
            if crit:
                write(f"class/thermal/thermal_zone{number}/trip_point_0_type", "critical")
                write(f"class/thermal/thermal_zone{number}/trip_point_0_temp", crit)
        write("class/hwmon/hwmon1/name", "coretemp")
        write("class/hwmon/hwmon1/temp1_input", "62000")
        write("class/hwmon/hwmon1/temp1_label", "Package id 0")
        write("class/hwmon/hwmon1/temp1_crit", "110000")
        write("class/hwmon/hwmon1/temp2_input", "88000")
        write("class/hwmon/hwmon1/temp2_label", "Core 0")
        write("class/hwmon/hwmon1/temp3_input", "garbage")
        write("class/hwmon/hwmon1/temp3_label", "Core 1")
        write("class/hwmon/hwmon2/name", "nvme")
        write("class/hwmon/hwmon2/temp1_input", "40000")
        write("class/hwmon/hwmon2/temp1_crit", "87850")
        write("class/power_supply/AC/type", "Mains")
        write("class/power_supply/AC/online", "1")
        write("class/power_supply/BAT0/type", "Battery")
        write("class/power_supply/BAT0/capacity", "80")
        write("class/power_supply/BAT0/status", "Charging")
        write("proc/acpi/button/lid/LID0/state", "state:      open")
        write("proc/stat", "cpu  1 2 3 4 5 6 7 8 0 0\n")

    def test_decision_sources_limits_and_cores(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.tree(root)
            sample = Telemetry(root, root / "proc").read()
            self.assertEqual(sample.errors, [])  # Unreadable core channel is not an error.
            self.assertIsNone(sample.preparing)
            by_id = {s.identity.split(":")[0] + ":" + s.identity.split(":")[1]
                     + (":cores" if s.identity.endswith(":cores") else ""): s for s in sample.sensors}
            tcpu = by_id["thermal:TCPU"]
            self.assertEqual((tcpu.kind, tcpu.warning, tcpu.high, tcpu.emergency), ("cpu", 95, 104, 107))
            self.assertEqual(by_id["thermal:x86_pkg_temp"].high, 104)
            package = by_id["hwmon:coretemp"]
            self.assertEqual((package.kind, package.celsius), ("cpu", 62))
            cores = by_id["hwmon:coretemp:cores"]
            self.assertEqual((cores.kind, cores.celsius), ("cpu-core", 88))
            chassis = by_id["thermal:SEN1"]
            self.assertEqual((chassis.warning, chassis.high, chassis.emergency), (65, 70, 73))
            nvme = by_id["hwmon:nvme"]
            self.assertEqual((nvme.kind, nvme.warning, nvme.high), ("storage", 77, 80))
            self.assertFalse(sample.lid_closed)
            self.assertFalse(sample.discharging)

    def test_sleep_probe_is_reported_and_failures_are_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.tree(root)
            self.assertTrue(Telemetry(root, root / "proc", sleep_probe=lambda: True).read().preparing)
            failing = Mock(side_effect=RuntimeError("bus down"))
            self.assertIsNone(Telemetry(root, root / "proc", sleep_probe=failing).read().preparing)

    def test_sample_round_trips_through_the_daemon_wire_format(self):
        from dataclasses import asdict
        sample = make(5., preparing=True)
        payload = json.loads(json.dumps(asdict(sample)))
        payload["sensors"] = [Sensor(**row) for row in payload["sensors"]]
        self.assertEqual(Sample(**payload), sample)


def show(**values):
    return SimpleNamespace(returncode=0, stdout="".join(f"{k}={v}\n" for k, v in values.items()))


def unit(name, **changes):
    commands = {
        "input-lock.service": "/usr/lib/input-lock/input-lock-daemon",
        safety.PROTECT_UNIT: "/usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py protect",
        safety.FAILSAFE_UNIT: "/usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py recover",
        "systemd-suspend.service": "/usr/lib/systemd/systemd-sleep suspend",
    }
    values = dict(LoadState="loaded", ActiveState="inactive", ExecStart=commands[name], Restart="no",
                  OnFailure=safety.FAILSAFE_UNIT if name in {"input-lock.service", safety.PROTECT_UNIT} else "",
                  User="", Type="notify" if name == "input-lock.service" else "oneshot",
                  WatchdogUSec="1min", TimeoutStopUSec="5s", KillMode="control-group",
                  TimeoutStartUSec="5min 30s", DropInPaths="",
                  ExecStopPost=("/usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py record-failure"
                                if name == safety.FAILSAFE_UNIT else ""))
    values.update(changes)
    return values


def native(**changes):
    return dict(supported=True, blocked=False, preparing=False, inputlock_holds_lid=False, **changes)


class ReadinessProbeTests(unittest.TestCase):
    def report(self, overrides=None, latch="absent"):
        overrides = overrides or {}

        def props(_run, name):
            return unit(name, **overrides.get(name, {}))
        with (patch.object(safety, "latch_state", return_value=(latch, "detail")),
              patch.object(safety, "unit_properties", side_effect=props)):
            return safety.protection_report(Mock(), status=native, counter=lambda: 1)

    def test_parse_usec(self):
        for text, seconds in (("20s", 20), ("2min 45s", 165), ("5min 30s", 330), ("300ms", .3),
                              ("1h 2min", 3720), ("0", 0), ("20000000", 20), ("1.5s", 1.5)):
            with self.subTest(text=text):
                self.assertAlmostEqual(safety._seconds(text), seconds)
        self.assertEqual(safety.parse_usec("infinity"), float("inf"))
        for text in ("", "soon", "20 parsecs", "s20", None):
            self.assertIsNone(safety.parse_usec(text))

    def test_current_contract_and_type_notify_pass(self):
        self.assertEqual(self.report(), {"errors": [], "info": []})
        self.assertEqual(self.report({"input-lock.service": {"Type": "simple"}})["errors"], [])

    def test_watchdog_and_stop_timeout_ranges(self):
        # 60 s from the unit (P1-1: longer than one 20 s freezer timeout), up
        # to 300 s while the daemon has extended it for a sleep.
        for value, ok in (("1min", True), ("60s", True), ("60000000", True), ("2min 30s", True),
                          ("5min", True), ("300000000", True), ("20s", False), ("59s", False),
                          ("19s", False), ("5min 1s", False), ("0", False), ("infinity", False)):
            with self.subTest(watchdog=value):
                errors = self.report({"input-lock.service": {"WatchdogUSec": value}})["errors"]
                self.assertEqual(not errors, ok)
        for value, ok in (("5s", True), ("30s", True), ("31s", False), ("infinity", False), ("0", False)):
            with self.subTest(stop=value):
                errors = self.report({"input-lock.service": {"TimeoutStopUSec": value}})["errors"]
                self.assertEqual(not errors, ok)
        for value in ("forking", "oneshot"):
            self.assertTrue(self.report({"input-lock.service": {"Type": value}})["errors"])

    def test_helper_start_timeout_must_cover_the_observe_budget(self):
        for name in (safety.PROTECT_UNIT, safety.FAILSAFE_UNIT):
            for value, ok in (("5min", True), ("5min 30s", True), ("30min", True),
                              ("4min 59s", False), ("2min 45s", False), ("infinity", False), ("garbage", False)):
                with self.subTest(unit=name, value=value):
                    errors = self.report({name: {"TimeoutStartUSec": value}})["errors"]
                    self.assertEqual(not errors, ok)

    def test_drop_ins_are_information_only(self):
        report = self.report({"input-lock.service": {"DropInPaths": "/etc/systemd/system/input-lock.service.d/x.conf"}})
        self.assertEqual(report["errors"], [])
        self.assertIn("drop-ins", report["info"][0])

    def test_stale_latch_is_information_active_latch_is_an_error(self):
        report = self.report(latch="stale")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["info"], ["detail"])
        self.assertEqual(self.report(latch="active")["errors"], [safety.LATCH_ERROR])

    def test_unit_files_in_the_repository_match_the_probe(self):
        for name in ("input-lock-protect.service", "input-lock-failsafe.service"):
            text = (REPO / "systemd" / name).read_text()
            timeout = re.search(r"^TimeoutStartSec=(.+)$", text, re.M).group(1)
            self.assertGreaterEqual(safety._seconds(timeout), safety.HELPER_START_TIMEOUT_MIN_SECONDS)
            for directive in ("PrivateDevices=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
                              "ProtectKernelLogs=yes", "ProtectControlGroups=yes", "RestrictNamespaces=yes",
                              "LockPersonality=yes", "SystemCallArchitectures=native",
                              "NoNewPrivileges=yes", "ReadWritePaths=-/var/lib/input-lock",
                              "StateDirectory=input-lock-protection", "StateDirectoryMode=0755"):
                self.assertIn(directive, text, name)
            self.assertNotIn("SD_LOGIND_SKIP_INHIBITORS", text)
            # Lead decision: no unverifiable capability bounding set on the
            # last-resort safety path.
            self.assertIsNone(re.search(r"^\s*CapabilityBoundingSet=", text, re.M), name)


class LatchStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.latch = Path(self.tmp.name) / "failure.json"

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, record=None, counter=4, boot="boot-b", raw=None):
        if raw is not None:
            self.latch.write_text(raw)
        elif record is not None:
            self.latch.write_text(json.dumps(record))
        return safety.latch_state(self.latch, counter=lambda: counter, boot=lambda: boot)[0]

    def test_rules(self):
        self.assertEqual(self.state(), "absent")
        self.assertEqual(self.state({"error": "legacy recovery unit failed: exit-code"}), "active")
        self.assertEqual(self.state({"error": "x", "boot_id": "boot-a", "suspend_success": 4}), "stale")
        self.assertEqual(self.state({"error": "x", "boot_id": "boot-b", "suspend_success": 3}), "stale")
        self.assertEqual(self.state({"error": "x", "boot_id": "boot-b", "suspend_success": 4}), "active")
        self.assertEqual(self.state(raw="{not json"), "active")


class ProtectMainTests(unittest.TestCase):
    """A9: no duplicate sleep into a busy or stuck OS sleep path; narrow latching."""

    def protect(self, *, recovery, suspend=None, daemon=None, native_changes=None, observe_error=None,
                journal=True, monotonic=10_000.):
        state = dict(boot_id="boot", invocation_id="inv", generation=3, updated_at=100.,
                     effective=recovery, restrictions_released=not recovery,
                     safety={"state": "PROTECTIVE_ACTION_PENDING"})
        calls, records = [], []

        def read_text(path, *args, **kwargs):
            if path.name == "boot_id":
                return "boot"
            if path.name == safety.RESULT_NAME or not journal:
                raise FileNotFoundError(path)
            return json.dumps(state)

        def run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0)

        def props(_run, name):
            values = dict(ActiveState="inactive", Result="success", InvocationID="inv",
                          InactiveExitTimestampMonotonic="0")
            values.update({"systemd-suspend.service": suspend or {},
                           "input-lock.service": daemon or {}}.get(name, {}))
            return values

        telemetry = Mock()
        telemetry.read.return_value = SimpleNamespace(lid_closed=True)
        # S-R6: the journal and receipt are read through _read_bounded_json.
        with (patch.object(Path, "read_text", read_text),
              patch.object(safety, "_read_bounded_json",
                           side_effect=lambda p, *a, **k: json.loads(read_text(p))),
              patch.object(safety.time, "time", return_value=100.),
              patch.object(safety.time, "monotonic", return_value=monotonic),
              patch.object(safety, "atomic_json", side_effect=lambda p, v, **k: records.append((p, v))),
              patch.object(safety, "unit_properties", side_effect=props)):
            result = safety.protect_main(
                run, lambda: True, telemetry, Path("/fixture/safety.json"), Mock(side_effect=observe_error),
                lambda: 1, recovery=recovery, status=lambda: native(**(native_changes or {})),
                latch=Path("/fixture/failure.json"), dispatch=lambda: calls.append(["logind"]))
        return result, calls, records

    def recent(self, seconds_ago, result):
        return {"InactiveExitTimestampMonotonic": str(int((10_000 - seconds_ago) * 1_000_000)),
                "Result": result, "ActiveState": "failed" if result != "success" else "inactive"}

    @staticmethod
    def latched(records):
        return any(path.name == "failure.json" for path, _ in records)

    def test_recent_failed_os_suspend_is_never_followed_by_a_second_request(self):
        # 2026-10-21: 19:20:05 OS suspend failed -> 19:21:55 recovery suspended again.
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                result, calls, records = self.protect(recovery=recovery, suspend=self.recent(110, "exit-code"))
                self.assertEqual(result, 0)
                self.assertNotIn(["logind"], calls)
                self.assertEqual(records[-1][1]["result"], safety.DEFERRED_OS_SLEEP)
                self.assertFalse(self.latched(records))

    def test_older_failure_does_not_block_protection(self):
        result, calls, _ = self.protect(recovery=False, suspend=self.recent(200, "exit-code"))
        self.assertEqual(result, 0)
        self.assertIn(["logind"], calls)

    def test_recent_successful_sleep_defers_recovery_only(self):
        result, calls, _ = self.protect(recovery=False, suspend=self.recent(60, "success"))
        self.assertIn(["logind"], calls)
        result, calls, records = self.protect(recovery=True, suspend=self.recent(60, "success"))
        self.assertEqual(result, 0)
        self.assertNotIn(["logind"], calls)
        self.assertEqual(records[-1][1]["result"], safety.DEFERRED_OS_SLEEP)

    def test_watchdog_killed_daemon_defers_without_latch(self):
        result, calls, records = self.protect(recovery=True, daemon={"Result": "watchdog"})
        self.assertEqual(result, 0)
        self.assertEqual(calls, [["/usr/bin/systemctl", "stop", "input-lock.service"]])
        self.assertEqual(records[-1][1]["detail"], "input-lock.service Result=watchdog")
        self.assertFalse(self.latched(records))

    def test_missing_journal_is_not_eligible_and_writes_nothing(self):
        result, calls, records = self.protect(recovery=True, journal=False)
        self.assertEqual(result, 0)
        self.assertEqual(calls, [["/usr/bin/systemctl", "stop", "input-lock.service"]])
        self.assertEqual(records, [])

    def test_accepted_request_then_failed_transition_latches_with_boot_and_counter(self):
        for recovery in (False, True):
            with self.subTest(recovery=recovery):
                result, calls, records = self.protect(recovery=recovery,
                                                      observe_error=RuntimeError("existing suspend service failed"))
                self.assertEqual(result, 1)
                latch = [value for path, value in records if path.name == "failure.json"]
                self.assertEqual(len(latch), 1)
                self.assertEqual(latch[0]["boot_id"], "boot")
                self.assertEqual(latch[0]["suspend_success"], 1)
                self.assertEqual(latch[0]["error"], "existing suspend service failed")

    def test_receipt_is_written_to_the_root_directory_and_the_daemon_copy(self):
        _result, _calls, records = self.protect(recovery=False)
        paths = {path for path, _ in records}
        self.assertIn(safety.ROOT_RECEIPT, paths)
        self.assertIn(Path("/fixture/safety-action-result.json"), paths)
        self.assertEqual(records[-1][1]["result"], "suspend-transition-confirmed")
        self.assertTrue(records[-1][1]["dispatch_accepted"])


class RecordFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.latch = base / "failure.json"
        self.receipt = base / safety.RESULT_NAME

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, result, counter=4, boot="boot-b"):
        return safety.record_failure(result, latch=self.latch, receipt=self.receipt,
                                     counter=lambda: counter, boot=lambda: boot, now=lambda: 50.)

    def test_success_and_no_evidence_do_not_latch(self):
        self.assertEqual(self.record("success"), "no-failure")
        self.assertEqual(self.record("exit-code"), "not-latched")
        self.receipt.write_text(json.dumps({"boot_id": "boot-b", "dispatch_accepted": False}))
        self.assertEqual(self.record("timeout"), "not-latched")
        self.receipt.write_text(json.dumps({"boot_id": "boot-a", "dispatch_accepted": True}))
        self.assertEqual(self.record("timeout"), "not-latched")
        self.assertFalse(self.latch.exists())

    def test_accepted_unconfirmed_request_latches(self):
        self.receipt.write_text(json.dumps({"boot_id": "boot-b", "dispatch_accepted": True,
                                            "transition_confirmed": False, "generation": 7}))
        self.assertEqual(self.record("timeout"), "created")
        value = json.loads(self.latch.read_text())
        self.assertEqual((value["boot_id"], value["suspend_success"], value["generation"]), ("boot-b", 4, 7))
        self.assertEqual(self.latch.stat().st_mode & 0o777, 0o644)

    def test_existing_latch_is_appended_never_overwritten(self):
        original = {"schema": 2, "error": "existing suspend service failed: exit-code",
                    "boot_id": "boot-b", "suspend_success": 4}
        self.latch.write_text(json.dumps(original))
        self.assertEqual(self.record("exit-code"), "appended")
        value = json.loads(self.latch.read_text())
        self.assertEqual(value["error"], original["error"])
        self.assertEqual(value["later_failures"][0]["service_result"], "exit-code")

    def test_new_failure_reactivates_a_stale_latch(self):
        self.latch.write_text(json.dumps({"error": "old", "boot_id": "boot-a", "suspend_success": 1}))
        self.assertEqual(self.record("timeout"), "appended")
        value = json.loads(self.latch.read_text())
        self.assertEqual((value["error"], value["boot_id"], value["suspend_success"]), ("old", "boot-b", 4))
        self.assertEqual(safety.latch_state(self.latch, counter=lambda: 4, boot=lambda: "boot-b")[0], "active")

    def test_unreadable_existing_latch_is_kept(self):
        self.latch.write_text("{corrupt")
        self.assertEqual(self.record("exit-code"), "kept")
        self.assertEqual(self.latch.read_text(), "{corrupt")


class AtomicJsonTests(unittest.TestCase):
    def test_mode_is_set_before_rename_and_symlinks_are_replaced_not_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            outside = base / "outside"
            outside.write_text("secret")
            outside.chmod(0o600)
            path = base / "receipt.json"
            path.symlink_to(outside)
            with patch.object(safety.os, "chmod", side_effect=AssertionError("path chmod used")):
                safety.atomic_json(path, {"ok": True}, mode=0o644)
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(outside.read_text(), "secret")
            self.assertEqual(outside.stat().st_mode & 0o777, 0o600)


def systemctl(states=None, hook=None):
    def run(argv, **_):
        if hook:
            hook()
        return show(ActiveState=(states or {}).get(argv[2], "inactive"))
    return run


class AcknowledgeHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.latch = base / "protection" / "failure.json"
        self.latch.parent.mkdir()
        self.data = json.dumps({"error": "existing suspend service failed: exit-code"}).encode()
        self.latch.write_bytes(self.data)
        self.archive = base / "ARCHIVE" / "InputLock"
        self.uid = os.getuid()

    def tearDown(self):
        self.tmp.cleanup()

    def ack(self, run=None, uid=None):
        return safety.acknowledge_failure(run=run or systemctl(), latch=self.latch, archive=self.archive,
                                          euid=lambda: 0, now=lambda: 0,
                                          expected_uid=self.uid if uid is None else uid)

    def assert_refused(self, pattern, **kwargs):
        with self.assertRaisesRegex(RuntimeError, pattern):
            self.ack(**kwargs)
        self.assertEqual(list(self.latch.parent.glob("failure-acknowledged-*")), [])

    def test_symlink_fifo_hardlink_and_owner_are_refused(self):
        self.assert_refused("regular file", uid=self.uid + 1)
        os.link(self.latch, self.latch.with_name("second-name"))
        self.assert_refused("regular file")
        self.latch.with_name("second-name").unlink()
        target = self.latch.with_name("elsewhere.json")
        self.latch.rename(target)
        self.latch.symlink_to(target)
        self.assert_refused("regular file")
        self.latch.unlink()
        os.mkfifo(self.latch)
        self.assert_refused("regular file")

    def test_latch_changed_after_reading_is_not_renamed(self):
        changed = []

        def rewrite():
            if not changed:
                changed.append(True)
                safety.atomic_json(self.latch, {"error": "a newer failure"}, mode=0o600)
        self.assert_refused("changed during acknowledgement", run=systemctl(hook=rewrite))
        self.assertEqual(json.loads(self.latch.read_text())["error"], "a newer failure")

    def test_archive_directory_owner_and_mode_are_verified(self):
        self.archive.mkdir(parents=True)
        self.archive.parent.chmod(0o700)  # Independent of the caller's umask.
        self.archive.chmod(0o770)
        self.assert_refused("not writable by group or others")
        self.archive.chmod(0o700)
        self.archive.parent.chmod(0o775)
        self.assert_refused("not writable by group or others")
        self.archive.parent.chmod(0o755)
        self.assertTrue(self.ack()["acknowledged"])

    def test_short_writes_are_completed(self):
        real_write = os.write

        def one_byte(fd, data):
            return real_write(fd, bytes(data[:1]))
        with patch.object(safety.os, "write", side_effect=one_byte):
            result = self.ack()
        self.assertEqual(Path(result["archive"]).read_bytes(), self.data)
        self.assertEqual(result["sha256"], hashlib.sha256(self.data).hexdigest())

    def test_missing_latch_still_reports_absent(self):
        self.latch.unlink()
        with self.assertRaises(FileNotFoundError):
            self.ack()


if __name__ == "__main__":
    unittest.main()
