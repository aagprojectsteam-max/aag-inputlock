"""Simulations only: fake clocks, temporary files and injected power requests."""
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from input_lock_safety import (SafetyPolicy, Sensor, Sample, Telemetry, atomic_json,
                               stack_ready, protect_main, observe_suspend)


def sample(now=100., **changes):
    return replace(Sample(now, False, [Sensor("cpu", "cpu", 45, 85, 95, 100),
                                      Sensor("chassis", "chassis", 40, 60, 68, 73)],
                          True, 80., False, 1, .1, True, True), **changes)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.policy = SafetyPolicy(lambda: self.now)
        self.policy.arm(sample(self.now))

    def update(self, after=0, **changes):
        self.now += after
        return self.policy.update(sample(self.now, lid_closed=True, **changes))

    def feed(self, seconds, **changes):
        """A new closed-lid sample every 2 s, delivered on two 1 s daemon ticks."""
        end = self.now + seconds
        changes = {"lid_closed": True, **changes}
        while self.now + 2 <= end:
            current = sample(self.now + 1, **changes)
            for _tick in range(2):
                self.now += 1
                reason = self.policy.update(current)
                if reason:
                    return reason
        return None

    def test_closed_lid_has_no_time_limit_over_days_and_weeks(self):
        self.assertIsNone(self.update())
        self.assertEqual(self.policy.state, "LID_CLOSED_MONITORED")
        for seconds in (120, 120, 3600, 86400, 14 * 86400):
            self.assertIsNone(self.update(seconds))
            self.assertTrue(self.policy.armed)
            self.assertIsNone(self.policy.deadline)
            self.assertIsNone(self.policy.snapshot()["deadline"])
            self.assertEqual(self.policy.snapshot()["lid_ignore_mode"],
                             "continuous_until_disabled")
            self.assertNotIn("CLOSED_LID_DEADLINE", self.policy.events)
        self.assertEqual(self.policy.events.count("CONTINUOUS_MONITORING_STARTED"), 1)
        self.assertGreater(self.policy.snapshot()["closed_seconds"], 14 * 86400)

    def test_opening_and_reclosing_keeps_arm_and_starts_new_observation(self):
        self.update()
        self.now += 120
        self.policy.update(sample(self.now))
        self.assertIsNone(self.policy.closed_since)
        self.assertTrue(self.policy.armed)
        self.assertIsNone(self.policy.deadline)
        self.assertIn("CONTINUOUS_MONITORING_ENDED", self.policy.events)
        self.update(1)
        self.assertEqual(self.policy.closed_since, self.now)
        self.assertIsNone(self.policy.deadline)
        self.assertEqual(self.policy.events.count("CONTINUOUS_MONITORING_STARTED"), 2)

    def test_battery_and_sustained_high_cpu_do_not_trigger_suspend_or_timer(self):
        self.update(ac=False, discharging=True, cpu_busy=.99)
        for passed in (10, 110, 120, 600, 86400):
            self.assertIsNone(self.update(passed, ac=False, discharging=True, cpu_busy=.99))
            self.assertEqual(self.policy.state, "LID_CLOSED_MONITORED")
            self.assertTrue(self.policy.armed)
            self.assertIsNone(self.policy.snapshot()["deadline"])
            self.assertEqual(self.policy.snapshot()["deadline_shortened_by"], [])
        self.assertNotIn("DEADLINE_SHORTENED_ON_BATTERY", self.policy.events)
        self.assertNotIn("DEADLINE_SHORTENED_SUSTAINED_HIGH_CPU", self.policy.events)
        self.assertIsNone(self.update(3600, ac=True, discharging=False, cpu_busy=.02))
        self.assertIn("AC_CONNECTED", self.policy.events)

    def test_low_battery_warns_but_only_critical_battery_requests_suspend(self):
        self.assertIsNone(self.update(2, battery=18, ac=False, discharging=True))
        self.assertEqual(self.policy.state, "LID_CLOSED_WARNING")
        self.assertEqual(self.policy.reason, "BATTERY_WARNING")
        self.assertIsNone(self.update(7200, battery=18, ac=False, discharging=True))
        self.assertTrue(self.policy.armed)
        self.assertIsNone(self.policy.update(sample(self.now, lid_closed=False)))
        self.assertTrue(self.policy.armed)
        self.assertIsNone(self.policy.deadline)

    def test_battery_critical_requires_sustained_samples(self):
        # A11: only a draining battery is critical.
        self.assertIsNone(self.update(2, battery=5, ac=False, discharging=True))
        self.assertEqual(self.update(2, battery=5, ac=False, discharging=True), "BATTERY_CRITICAL")

    def test_upower_critical_warning_protects_even_with_high_percentage(self):
        self.update(2, warning_level=4)
        self.assertEqual(self.update(2, warning_level=4), "BATTERY_CRITICAL")

    def test_whole_missing_category_is_lost_after_thirty_seconds(self):
        for changes in ({"sensors": []}, {"sensors": [sample().sensors[1]]},
                        {"sensors": [sample().sensors[0]]}, {"battery": None, "ac": None}):
            with self.subTest(changes=changes):
                self.setUp()
                self.update()
                self.assertIsNone(self.feed(26, **changes))
                self.assertEqual(self.feed(10, **changes), "TELEMETRY_LOST")
                self.assertEqual(self.policy.last_decision["reason"], "TELEMETRY_LOST")

    def test_probe_dependency_and_partial_power_gaps_only_warn(self):
        for changes in ({"dependencies_ok": False}, {"stack_ready": False},
                        {"protection_errors": ["ordinary suspend unavailable, blocked, or already in progress"]},
                        {"ac": None}, {"battery": None}, {"discharging": None},
                        {"errors": ["unreadable:coretemp:temp7"]}):
            with self.subTest(changes=changes):
                self.setUp()
                self.update()
                self.assertIsNone(self.feed(120, **changes))
                self.assertTrue(self.policy.armed)
                self.assertEqual(self.policy.state, "LID_CLOSED_WARNING")

    def test_unknown_lid_is_supervised_as_closed_without_loss(self):
        self.update()
        self.assertIsNone(self.feed(120, lid_closed=None))
        self.assertTrue(self.policy.armed)
        self.assertIsNotNone(self.policy.closed_since)
        self.assertEqual(self.policy.state, "LID_CLOSED_WARNING")

    def test_sensor_disappearance_is_not_loss_while_its_category_remains(self):
        self.policy.required_sensors.add("another-cpu")
        self.update()
        self.assertIsNone(self.feed(120))
        self.assertTrue(self.policy.armed)

    def test_sustained_high_temperature_forces_protection_after_sixty_seconds(self):
        hot = [Sensor("cpu", "cpu", 96, 85, 95, 100), sample().sensors[1]]
        self.assertIsNone(self.update(sensors=hot))
        self.assertIsNone(self.feed(60, sensors=hot))
        self.assertEqual(self.feed(10, sensors=hot), "THERMAL_HIGH_RISK")
        self.assertFalse(self.policy.armed)
        self.assertEqual(self.policy.last_decision["reason"], "THERMAL_HIGH_RISK")
        self.assertIsNone(self.policy.last_decision["time_limit_seconds"])

    def test_thermal_emergency_preserves_exact_sensor_witnesses_and_thresholds(self):
        cpu = Sensor("thermal:TCPU", "cpu", 102.05, 85, 95, 100)
        other = Sensor("hwmon:coretemp", "cpu", 62, 85, 95, 100)
        chassis = Sensor("thermal:SEN2", "chassis", 44.05, 60, 68, 73)
        self.assertIsNone(self.update(sensors=[cpu, other, chassis]))
        self.assertEqual(self.feed(14, sensors=[cpu, other, chassis]), "THERMAL_EMERGENCY")
        evidence = self.policy.last_decision["thermal_evidence"]
        self.assertEqual(evidence["thermal_level"], 3)
        self.assertGreaterEqual(evidence["candidate_seconds"], 10.0)
        self.assertEqual(evidence["rule"]["rule"], "cpu-single-source-extreme")
        self.assertEqual(evidence["rule"]["basis"], "raw")
        self.assertGreaterEqual(len(evidence["rule"]["window"]), 3)
        self.assertEqual(len(evidence["sensors"]), 3)
        witness = evidence["sensors"][0]
        self.assertEqual(witness["identity"], "thermal:TCPU")
        self.assertEqual(witness["celsius"], 102.05)
        self.assertEqual(witness["emergency_c"], 100)
        self.assertEqual(witness["risk"], 3)
        self.assertEqual(evidence["sensors"][-1]["celsius"], 44.05)
        self.assertEqual(self.policy.snapshot()["last_decision"], self.policy.last_decision)

    def test_thermal_brief_spike_does_not_create_a_false_witness(self):
        cpu = Sensor("thermal:TCPU", "cpu", 102, 85, 95, 100)
        self.assertIsNone(self.update(sensors=[cpu, sample().sensors[1]]))
        self.assertIsNone(self.update(1))
        self.assertIsNone(self.policy.last_decision)
        self.assertTrue(self.policy.armed)

    def test_multiweek_operation_still_fails_safe_on_lost_telemetry(self):
        self.assertIsNone(self.update())
        self.assertIsNone(self.update(21 * 86400))
        self.assertTrue(self.policy.armed)
        self.assertIsNone(self.feed(20, sensors=[]))
        self.assertEqual(self.feed(20, sensors=[]), "TELEMETRY_LOST")
        self.assertFalse(self.policy.armed)
        self.assertGreater(self.policy.last_decision["closed_seconds"], 21 * 86400)

    def test_explicit_release_ends_continuous_mode_without_sleep(self):
        self.assertIsNone(self.update())
        self.policy.release()
        self.assertFalse(self.policy.armed)
        self.assertIsNone(self.policy.closed_since)
        self.assertIsNone(self.policy.deadline)
        self.assertEqual(self.policy.state, "SAFE_NORMAL")
        self.assertIsNone(self.update(31 * 86400))

    def test_thermal_spike_does_not_trigger(self):
        hot = [Sensor("cpu", "cpu", 102, 85, 95, 100), sample().sensors[1]]
        self.assertIsNone(self.update(sensors=hot))
        self.assertIsNone(self.update(1))
        self.assertEqual(self.policy.thermal_level, 0)

    def test_thermal_emergency_intervenes_before_hardware_cutoff_on_ac(self):
        hot = [Sensor("cpu", "cpu", 101, 85, 95, 100), sample().sensors[1]]
        self.update(sensors=hot)
        self.assertIsNone(self.feed(8, sensors=hot))
        self.assertEqual(self.feed(6, sensors=hot), "THERMAL_EMERGENCY")

    def test_warning_hysteresis_and_cooldown(self):
        warm = [Sensor("cpu", "cpu", 86, 85, 95, 100), sample().sensors[1]]
        self.update(sensors=warm)
        self.feed(40, sensors=warm)
        self.assertEqual(self.policy.thermal_level, 1)
        self.feed(20, sensors=[replace(warm[0], celsius=83), warm[1]])
        self.assertEqual(self.policy.thermal_level, 1)
        self.feed(10)
        self.assertEqual(self.policy.thermal_level, 0)

    def test_telemetry_recovery_retains_closed_lid_continuity(self):
        self.update(dependencies_ok=False)
        original = self.policy.closed_since
        self.update(10)
        self.assertEqual(self.policy.closed_since, original)
        self.assertIsNone(self.policy.missing_since)
        self.assertIsNone(self.policy.deadline)

    def test_arm_requires_open_lid_healthy_telemetry_and_acceptable_temperature(self):
        for s in (sample(lid_closed=True), sample(dependencies_ok=False), sample(stack_ready=False),
                  sample(sensors=[Sensor("cpu", "cpu", 99, 85, 95, 100), sample().sensors[1]])):
            with self.assertRaises(ValueError):
                SafetyPolicy(lambda: 100.).arm(s)

    def test_restart_starts_disarmed(self):
        self.update()
        fresh = SafetyPolicy()
        self.assertFalse(fresh.armed)
        self.assertEqual(fresh.state, "SAFE_NORMAL")

    def test_explicit_rearm_after_thermal_protection_starts_a_fresh_episode(self):
        hot = [Sensor("cpu", "cpu", 102, 85, 95, 100), sample().sensors[1]]
        self.update(sensors=hot)
        self.assertEqual(self.feed(14, sensors=hot), "THERMAL_EMERGENCY")
        self.assertFalse(self.policy.armed)
        self.assertEqual(self.policy.last_decision["time_limit_seconds"], None)
        self.feed(12)  # Sustained heat must have cleared before a re-arm.
        self.policy.arm(sample(self.now))  # Fresh observed open lid.
        self.update(1)
        self.assertTrue(self.policy.armed)
        self.assertEqual(self.policy.closed_since, self.now)
        self.assertIsNone(self.policy.deadline)


class DecisionDispatchTests(unittest.TestCase):
    def test_policy_and_daemon_create_one_decision_with_original_evidence(self):
        from input_lock_daemon import InputLockDaemon

        now = [100.]
        policy = SafetyPolicy(lambda: now[0])
        policy.arm(sample())
        hot = [Sensor("cpu", "cpu", 102, 85, 95, 100), Sensor("cpu2", "cpu", 101, 85, 95, 100),
               sample().sensors[1]]
        reason = None
        for _ in range(4):  # Three consecutive corroborated samples.
            now[0] += 2
            reason = policy.update(sample(now[0], lid_closed=True, sensors=hot)) or reason
        self.assertEqual(reason, "THERMAL_EMERGENCY")
        original = policy.last_decision.copy()
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        daemon.safety = policy
        daemon.devices = {}
        daemon.lid_generation = 7
        daemon._emergency_unlock = Mock()
        daemon._write_safety = Mock()
        daemon._post_control = Mock()
        with patch("input_lock_daemon.threading.Thread") as worker:
            daemon._start_protection(reason)
            worker.return_value.start.assert_called_once()
        self.assertEqual(policy.events.count("PROTECTIVE_ACTION_DECIDED"), 1)
        self.assertEqual(policy.last_decision, original)
        self.assertEqual(daemon.protective_result["decision_evidence"], original)


class TelemetryTests(unittest.TestCase):
    def test_driver_identity_invalid_trips_and_renumbering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number, kind, temp in ((7, "SEN1", "50050"), (28, "TCPU", "70000")):
                zone = root / f"class/thermal/thermal_zone{number}"
                zone.mkdir(parents=True)
                for name, value in {"type":kind,"temp":temp,"trip_point_0_type":"passive",
                                    "trip_point_0_temp":"-274000","trip_point_1_type":"critical",
                                    "trip_point_1_temp":"80050" if kind == "SEN1" else "110050"}.items():
                    (zone/name).write_text(value)
            t = Telemetry(root, root / "proc")
            first = t.read()
            (root / "class/thermal/thermal_zone7").rename(root / "class/thermal/thermal_zone97")
            second = t.read()
            self.assertEqual({s.identity for s in first.sensors}, {s.identity for s in second.sensors})
            chassis = next(s for s in second.sensors if s.kind == "chassis")
            self.assertAlmostEqual(chassis.high, 70)  # Review section F: 65/70/73C at an 80C trip.
            self.assertFalse(second.healthy(second.at))

    def test_atomic_journal_retains_previous_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"safety.json"
            atomic_json(path, {"effective":True})
            with patch("input_lock_safety.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    atomic_json(path, {"effective":False})
            self.assertTrue(json.loads(path.read_text())["effective"])
            self.assertEqual(len(list(path.parent.iterdir())), 1)


class SuspendObservationTests(unittest.TestCase):
    def test_old_success_is_not_mistaken_for_this_request(self):
        now = [100.]
        old = 'ActiveState=inactive\nResult=success\nExecMainStatus=0\nInactiveExitTimestampMonotonic=1000000\nExecMainStartTimestampMonotonic=1000000\nExecMainExitTimestampMonotonic=2000000\n'
        new = old.replace('1000000', '101000000').replace('2000000', '102000000')
        run = Mock(side_effect=[SimpleNamespace(returncode=0,stdout=old), SimpleNamespace(returncode=0,stdout=new)])
        observe_suspend(run, 100., 0, counter=lambda:1, clock=lambda:now[0], sleep=lambda seconds:now.__setitem__(0,now[0]+seconds))
        self.assertEqual(run.call_count,2)

    def test_new_failure_is_reported(self):
        run = Mock(return_value=SimpleNamespace(returncode=0,stdout='ActiveState=failed\nResult=exit-code\nInactiveExitTimestampMonotonic=101000000\n'))
        with self.assertRaises(RuntimeError):
            observe_suspend(run,100.,0,counter=lambda:1,clock=lambda:100.)

    def test_no_new_transition_has_bounded_wait(self):
        now = [100.]
        run = Mock(return_value=SimpleNamespace(returncode=0,stdout='ActiveState=inactive\nResult=success\nInactiveExitTimestampMonotonic=1\n'))
        with self.assertRaises(TimeoutError):
            observe_suspend(run,100.,0,counter=lambda:1,clock=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+seconds))
        self.assertEqual(run.call_count,240)


class ThermalMeasurementTests(unittest.TestCase):
    def test_hot_disarmed_open_lid_is_high_and_keeps_raw_sample(self):
        p=SafetyPolicy(lambda:100.)
        hot=sample(sensors=[Sensor("cpu", "cpu", 95.05, 85, 95, 100), sample().sensors[1]])
        self.assertIsNone(p.update(hot))
        before=p.snapshot()
        p.release()
        for snapshot in (before,p.snapshot()):
            self.assertEqual(snapshot["thermal"],"HIGH_RISK")
            self.assertFalse(snapshot["protection_armed"])
            self.assertEqual(snapshot["thermal_protection"],"DISARMED")
            self.assertEqual(snapshot["sample"]["sensors"][0]["celsius"],95.05)

    def test_armed_open_lid_measures_heat_without_changing_intervention(self):
        p=SafetyPolicy(lambda:100.)
        p.arm(sample())
        self.assertIsNone(p.update(sample(sensors=[Sensor("cpu","cpu",101,85,95,100),sample().sensors[1]])))
        self.assertEqual(p.snapshot()["thermal"],"EMERGENCY")
        self.assertTrue(p.snapshot()["protection_armed"])
        self.assertEqual(p.thermal_level,0)
        self.assertIsNone(p.deadline)

    def test_unknown_stale_missing_and_partial_hot_measurements(self):
        p=SafetyPolicy(lambda:100.)
        self.assertEqual(p.snapshot()["thermal"],"UNKNOWN")
        for data in (sample(now=80),sample(sensors=[]),sample(sensors=[sample().sensors[0]])):
            p.update(data)
            self.assertEqual(p.snapshot()["thermal"],"UNKNOWN")
        p.update(sample(sensors=[Sensor("cpu","cpu",95,85,95,100)]))
        self.assertEqual(p.snapshot()["thermal"],"HIGH_RISK")
        self.assertFalse(p.snapshot()["thermal_measurement"]["complete"])

    def test_measurement_does_not_wait_for_intervention_dwell(self):
        p=SafetyPolicy(lambda:100.)
        p.arm(sample())
        self.assertIsNone(p.update(sample(lid_closed=True,sensors=[Sensor("cpu","cpu",96,85,95,100),sample().sensors[1]])))
        self.assertEqual(p.snapshot()["thermal"],"HIGH_RISK")
        self.assertEqual(p.thermal_level,0)
        self.assertIsNone(p.deadline)


class ConfirmationRegressionTests(unittest.TestCase):
    def test_successful_service_without_kernel_suspend_is_unconfirmed(self):
        now=[100.]
        result=SimpleNamespace(returncode=0,stdout="ActiveState=inactive\nResult=success\nExecMainStatus=0\nInactiveExitTimestampMonotonic=101000000\nExecMainStartTimestampMonotonic=101000000\nExecMainExitTimestampMonotonic=102000000\n")
        with self.assertRaises(TimeoutError):
            observe_suspend(Mock(return_value=result),100.,0,counter=lambda:0,clock=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+seconds))


class ResumeConfirmationTests(unittest.TestCase):
    def test_native_kernel_and_service_success_needs_no_aag_receipt(self):
        result=SimpleNamespace(returncode=0,stdout="ActiveState=inactive\nResult=success\nExecMainStatus=0\nInactiveExitTimestampMonotonic=101000000\nExecMainStartTimestampMonotonic=101000000\nExecMainExitTimestampMonotonic=102000000\n")
        observe_suspend(Mock(return_value=result), 100., 0, counter=lambda:1, clock=lambda:103.)
