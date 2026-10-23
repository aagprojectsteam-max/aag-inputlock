"""Lid-ignore recovery (2026-10-22): spike-tolerant arming, latch acknowledgement, readiness text.

Simulations only: fake clocks, temporary directories, injected systemctl.
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import input_lock_safety as safety  # noqa: E402
from input_lock_cli import lid_readiness
from input_lock_safety import SafetyPolicy, Sample, Sensor


def sample(at, cpu=60.0, **changes):
    return replace(Sample(at, False, [Sensor("cpu", "cpu", cpu, 85, 95, 100),
                                      Sensor("chassis", "chassis", 40, 65, 70, 73)],
                          True, 80., False, 1, .1, True, True), **changes)


class SpikeTolerantArmingTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.
        self.policy = SafetyPolicy(lambda: self.now)

    def feed(self, *temps):
        for temp in temps:
            self.now += 2
            self.policy.update(sample(self.now, temp))

    def test_single_transient_spike_does_not_refuse_arming(self):
        # Observed on the EliteBook: 87, 91, 94, then one 101C sample.
        self.feed(87, 91, 94)
        self.now += 2
        self.policy.arm(sample(self.now, 101))
        self.assertTrue(self.policy.armed)

    def test_sustained_heat_still_refuses(self):
        self.feed(96, 97, 98)
        self.now += 2
        with self.assertRaisesRegex(ValueError, r"too high.*cpu 9\dC"):
            self.policy.arm(sample(self.now, 97))
        self.assertFalse(self.policy.armed)

    def test_without_history_the_raw_reading_is_used(self):
        with self.assertRaisesRegex(ValueError, "too high"):
            self.policy.arm(sample(self.now, 96))

    def test_extreme_single_reading_refuses_even_with_cool_history(self):
        self.feed(60, 60, 60, 60)
        self.now += 2
        with self.assertRaisesRegex(ValueError, "cpu 105C"):
            self.policy.arm(sample(self.now, 105))

    def test_old_samples_fall_out_of_the_window(self):
        self.feed(60, 60, 60)          # cool, but about to be stale
        self.now += 30
        self.feed(97, 98)              # only two recent samples
        self.now += 2
        with self.assertRaisesRegex(ValueError, "too high"):
            self.policy.arm(sample(self.now, 99))

    def test_closed_lid_supervision_needs_sustained_single_source_heat(self):
        # Section F: three samples of one source are not enough; one source
        # at emergency + 1 C must persist for 10 s (review A1).
        self.feed(60, 60, 60)
        self.now += 2
        self.policy.arm(sample(self.now))
        decision = None
        for _ in range(3):
            self.now += 1.5
            decision = self.policy.update(sample(self.now, 101, lid_closed=True)) or decision
        self.assertIsNone(decision)
        for _ in range(6):
            self.now += 2
            decision = self.policy.update(sample(self.now, 101, lid_closed=True)) or decision
        self.assertEqual(decision, "THERMAL_EMERGENCY")


def fake_systemctl(states):
    def run(argv, **_):
        unit = argv[2]
        return SimpleNamespace(returncode=0, stdout=f"ActiveState={states.get(unit, 'inactive')}\n")
    return run


class AcknowledgeFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.latch = base / "protection" / "failure.json"
        self.latch.parent.mkdir()
        self.data = json.dumps({"error": "recovery unit failed: exit-code", "observed_at": 1.0}).encode()
        self.latch.write_bytes(self.data)
        self.archive = base / "ARCHIVE" / "InputLock"
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.uid = os.getuid()

    def tearDown(self):
        self.tmp.cleanup()

    def ack(self, states=None, euid=0):
        # The test user stands in for root as the required owner (S5: the
        # owner is now checked with fstat on the opened descriptor).
        return safety.acknowledge_failure(run=fake_systemctl(states or {}), latch=self.latch,
                                          archive=self.archive, euid=lambda: euid, now=lambda: 0,
                                          expected_uid=self.uid)

    def test_archives_verifies_and_retains_the_failure(self):
        result = self.ack()
        self.assertTrue(result["acknowledged"])
        self.assertFalse(self.latch.exists())
        retained = self.latch.with_name(f"failure-acknowledged-{self.digest}.json")
        self.assertEqual(retained.read_bytes(), self.data)
        saved = Path(result["archive"])
        self.assertEqual(saved.read_bytes(), self.data)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(result["error"], "recovery unit failed: exit-code")

    def test_second_identical_failure_is_kept_under_a_distinct_name(self):
        self.ack()
        self.latch.write_bytes(self.data)
        result = self.ack()
        self.assertTrue(result["retained_as"].endswith("-19700101T000000Z.json"))
        self.assertEqual(len(list(self.latch.parent.glob("failure-acknowledged-*"))), 2)

    def test_refuses_without_root(self):
        with self.assertRaises(PermissionError):
            self.ack(euid=1000)
        self.assertTrue(self.latch.exists())

    def test_refuses_while_a_protection_or_suspend_job_runs(self):
        for unit in (safety.PROTECT_UNIT, safety.FAILSAFE_UNIT, "systemd-suspend.service"):
            with self.assertRaisesRegex(RuntimeError, "activating"):
                self.ack({unit: "activating"})
            self.assertTrue(self.latch.exists())

    def test_missing_latch_is_reported_not_invented(self):
        self.latch.unlink()
        with self.assertRaises(FileNotFoundError):
            self.ack()
        self.assertFalse(self.archive.exists())

    def test_symlinked_latch_is_refused(self):
        target = self.latch.with_name("elsewhere.json")
        self.latch.rename(target)
        self.latch.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            self.ack()

    def test_acknowledged_latch_clears_readiness(self):
        self.ack()
        with unittest.mock.patch.object(safety, "FAILURE_LATCH", self.latch):
            errors = safety.protection_errors(status=lambda: {"supported": False})
        self.assertNotIn(safety.LATCH_ERROR, errors)


class ReadinessTextTests(unittest.TestCase):
    def state(self, **sample_fields):
        base = {"stack_ready": True, "dependencies_ok": True, "errors": [], "protection_errors": []}
        base.update(sample_fields)
        return {"safety": {"sample": base}}

    def test_ready(self):
        self.assertEqual(lid_readiness(self.state()), (True, "ready"))

    def test_latch_reason_includes_the_recovery_command(self):
        ready, why = lid_readiness(self.state(stack_ready=False, protection_errors=[safety.LATCH_ERROR]))
        self.assertFalse(ready)
        self.assertIn("previous emergency suspend failed", why)
        self.assertIn("acknowledge-failure", why)

    def test_other_reasons_are_reported_verbatim(self):
        self.assertEqual(lid_readiness(self.state(dependencies_ok=False))[1],
                         "power/logind supervision is not fresh")
        self.assertEqual(lid_readiness({})[1], "safety telemetry not reported yet")


if __name__ == "__main__":
    unittest.main()
