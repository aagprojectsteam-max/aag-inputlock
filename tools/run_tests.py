#!/usr/bin/env python3
"""Explicit public test groups; no host-specific production evidence."""
import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
logging.disable(logging.CRITICAL)

group = sys.argv[1] if len(sys.argv) > 1 else "all"
shared = [
    "test_core",
    "test_version_consistency",
    "test_windows_state",
    "test_windows_power",
    "test_windows_remediation",
]
linux = [
    "test_common",
    "test_daemon_logic",
    "test_gnome",
    "test_notifier_settings",
    "test_lid_policy",
    "test_remediation_linux",
    "test_safety",
    "test_native_protection",
    "test_aag_adapter_contract",
    "test_thermal_status",
]
groups = {
    "shared": shared,
    "linux": linux,
    "windows-native": ["test_windows_native"],
    "all": shared + linux,
}
if group not in groups:
    raise SystemExit("Choose shared, linux, windows-native or all")
suite = unittest.defaultTestLoader.loadTestsFromNames(groups[group])
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
