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

    def test_deadline_warning_high_and_protect(self):
        self.update()
        self.assertEqual(self.policy.state, "LID_CLOSED_MONITORED")
        self.update(120)
        self.assertEqual(self.policy.state, "LID_CLOSED_WARNING")
        self.update(60)
        self.assertEqual(self.policy.state, "LID_CLOSED_HIGH_RISK")
        self.assertEqual(self.update(60), "CLOSED_LID_DEADLINE")
        self.assertFalse(self.policy.armed)
        self.assertIsNone(self.update(400))  # No duplicate action.

    def test_open_cancels_and_new_close_gets_new_episode(self):
        self.update()
        self.now += 120
        self.policy.update(sample(self.now))
        self.assertIsNone(self.policy.deadline)
        self.update()
        self.assertEqual(self.policy.deadline, 460)

    def test_high_load_shortens_deadline_and_does_not_extend_when_load_falls(self):
        self.update(cpu_busy=.9)
        self.update(10, cpu_busy=.9)
        self.assertEqual(self.policy.deadline, 220)
        self.assertEqual(self.policy.snapshot()["deadline_shortened_by"], ["SUSTAINED_HIGH_CPU"])
        self.assertEqual(self.policy.events.count("DEADLINE_SHORTENED_SUSTAINED_HIGH_CPU"), 1)
        self.update(50, cpu_busy=.1)
        self.assertEqual(self.policy.deadline, 220)
        self.assertEqual(self.update(60), "CLOSED_LID_DEADLINE")
        evidence = self.policy.snapshot()["last_decision"]
        self.assertEqual(evidence["deadline_shortened_by"], ["SUSTAINED_HIGH_CPU"])
        self.assertEqual(evidence["time_limit_seconds"], 120)
        self.assertEqual(evidence["reason"], "CLOSED_LID_DEADLINE")

    def test_battery_and_ac_loss_shorten_timer(self):
        self.update()
        self.update(30, ac=False, discharging=True)
        self.assertEqual(self.policy.deadline, 220)
        self.update(30)
        self.assertEqual(self.policy.deadline, 220)
        self.assertIn("AC_LOST", self.policy.events)
        self.assertEqual(self.policy.snapshot()["deadline_shortened_by"], ["ON_BATTERY"])
        self.assertEqual(self.policy.events.count("DEADLINE_SHORTENED_ON_BATTERY"), 1)

    def test_both_deadline_causes_are_recorded_once_without_extending_it(self):
        self.update(ac=False, discharging=True)
        self.update(10, ac=False, discharging=True, cpu_busy=.85)
        self.update(10, ac=False, discharging=True, cpu_busy=.85)
        self.update(10, ac=True, discharging=False, cpu_busy=.1)
        self.assertEqual(self.policy.deadline, 220)
        self.assertEqual(self.policy.snapshot()["deadline_shortened_by"],
                         ["ON_BATTERY", "SUSTAINED_HIGH_CPU"])
        for cause in ("ON_BATTERY", "SUSTAINED_HIGH_CPU"):
            self.assertEqual(self.policy.events.count("DEADLINE_SHORTENED_" + cause), 1)
        self.assertEqual(self.update(90), "CLOSED_LID_DEADLINE")
        self.assertEqual(self.policy.snapshot()["last_decision"]["deadline_shortened_by"],
                         ["ON_BATTERY", "SUSTAINED_HIGH_CPU"])
        self.policy.release()
        self.assertEqual(self.policy.snapshot()["last_decision"]["time_limit_seconds"], 120)
        self.assertEqual(self.policy.snapshot()["deadline_shortened_by"], [])

    def test_new_close_resets_deadline_cause_from_previous_episode(self):
        self.update(ac=False, discharging=True)
        self.assertEqual(self.policy.deadline_shortened_by, {"ON_BATTERY"})
        self.now += 5
        self.policy.update(sample(self.now, lid_closed=False))
        self.assertEqual(self.policy.deadline_shortened_by, set())
        self.update(ac=True, discharging=False, cpu_busy=.1)
        self.assertEqual(self.policy.deadline, self.now + 240)
        self.assertEqual(self.policy.snapshot()["deadline_shortened_by"], [])

    def test_battery_critical_requires_sustained_samples(self):
        self.assertIsNone(self.update(battery=5))
        self.assertEqual(self.update(2, battery=5), "BATTERY_CRITICAL")

    def test_upower_critical_warning_protects_even_with_high_percentage(self):
        self.update(warning_level=4)
        self.assertEqual(self.update(2, warning_level=4), "BATTERY_CRITICAL")

    def test_each_missing_dependency_has_bounded_grace(self):
        for changes in ({"sensors": []}, {"dependencies_ok": False}, {"stack_ready": False},
                        {"ac": None}, {"battery": None}, {"lid_closed": None}):
            with self.subTest(changes=changes):
                now = [100.]
                policy = SafetyPolicy(lambda: now[0])
                policy.arm(sample())
                policy.update(sample(lid_closed=True, **{k:v for k,v in changes.items() if k != 'lid_closed'}) if 'lid_closed' not in changes else sample(lid_closed=None))
                now[0] += 15
                self.assertEqual(policy.update(replace(sample(now[0], lid_closed=True), **changes)), "TELEMETRY_LOST")

    def test_sensor_disappearance_is_not_masked_by_another_cpu(self):
        self.policy.required_sensors.add("another-cpu")
        self.update()
        self.assertEqual(self.update(15), "TELEMETRY_LOST")

    def test_thermal_spike_does_not_trigger(self):
        hot = [Sensor("cpu", "cpu", 102, 85, 95, 100), sample().sensors[1]]
        self.assertIsNone(self.update(sensors=hot))
        self.assertIsNone(self.update(1))
        self.assertEqual(self.policy.thermal_level, 0)

    def test_thermal_emergency_intervenes_before_hardware_cutoff_on_ac(self):
        hot = [Sensor("cpu", "cpu", 101, 85, 95, 100), sample().sensors[1]]
        self.update(sensors=hot)
        self.assertEqual(self.update(2, sensors=hot), "THERMAL_EMERGENCY")

    def test_warning_hysteresis_and_cooldown(self):
        warm = [Sensor("cpu", "cpu", 86, 85, 95, 100), sample().sensors[1]]
        self.update(sensors=warm)
        self.update(10, sensors=warm)
        self.assertEqual(self.policy.thermal_level, 1)
        self.update(1, sensors=[replace(warm[0], celsius=83), warm[1]])
        self.assertEqual(self.policy.thermal_level, 1)
        self.update(1)
        self.update(15)
        self.assertEqual(self.policy.thermal_level, 0)

    def test_telemetry_recovery_does_not_reset_closed_timer(self):
        self.update(dependencies_ok=False)
        original = self.policy.deadline
        self.update(10)
        self.assertEqual(self.policy.deadline, original)
        self.assertIsNone(self.policy.missing_since)

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

    def test_explicit_rearm_after_protection_starts_a_fresh_episode(self):
        self.update()
        self.assertEqual(self.update(240), "CLOSED_LID_DEADLINE")
        self.policy.arm(sample(self.now))  # Fresh observed open lid.
        self.update(1)
        self.assertTrue(self.policy.armed)
        self.assertEqual(self.policy.closed_since, self.now)
        self.assertEqual(self.policy.deadline, self.now + 240)


class DecisionDispatchTests(unittest.TestCase):
    def test_policy_and_daemon_create_one_decision_with_original_evidence(self):
        from input_lock_daemon import InputLockDaemon

        now = [100.]
        policy = SafetyPolicy(lambda: now[0])
        policy.arm(sample())
        policy.update(sample(now[0], lid_closed=True, ac=False, discharging=True))
        now[0] += 120
        reason = policy.update(sample(now[0], lid_closed=True, ac=False, discharging=True))
        self.assertEqual(reason, "CLOSED_LID_DEADLINE")
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
            self.assertAlmostEqual(chassis.high, 68.05)
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
        self.assertEqual(run.call_count,120)


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
        self.assertEqual(p.deadline,340.)


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
