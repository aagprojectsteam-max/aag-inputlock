"""Historical adapter tests. This module is no longer used by InputLock protection."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import aag_sleep_transaction_compat as compat
import input_lock_safety as safety


class AAGAdapterContractTests(unittest.TestCase):
    def test_adapter_graph_requires_every_expected_token(self):
        good = {
            "systemd-suspend.service": (
                "aag-storage-sleep-v2.service aag-suspend-failure-failsafe.service "
                "/usr/lib/systemd/systemd-sleep suspend aag-sleep-transaction native-pre "
                "aag-sleep-transaction native-post"
            ),
            "aag-storage-sleep-v2.service": (
                "aag-ugreen-sleep-guard-v2 pre aag-sleep-transaction begin "
                "aag-sleep-transaction verify-release"
            ),
            "aag-suspend-failure-failsafe.service": (
                "aag-sleep-transaction failure-recover --what=sleep:handle-lid-switch"
            ),
            "aag-t700-resume-check.service": (
                "aag-sleep-transaction resume-verify-device "
                "aag-sleep-transaction resume-verify-terminal"
            ),
            "input-lock-protect.service": "input_lock_safety.py",
        }

        def run(argv, **_kwargs):
            unit = argv[2]
            return SimpleNamespace(returncode=0, stdout="LoadState=loaded\n" + good[unit])

        self.assertTrue(compat.stack_ready(run))
        broken = dict(good)
        broken["aag-t700-resume-check.service"] = "aag-sleep-transaction resume-verify-device"

        def bad_run(argv, **_kwargs):
            unit = argv[2]
            return SimpleNamespace(returncode=0, stdout="LoadState=loaded\n" + broken[unit])

        self.assertFalse(compat.stack_ready(bad_run))


if __name__ == "__main__":
    unittest.main()
