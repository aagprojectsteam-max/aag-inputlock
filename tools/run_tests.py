#!/usr/bin/env python3
"""Run the public test groups; no host-specific production evidence.

The shared and Windows-native groups are explicit because they must import
without Linux dependencies. Every other ``tests/test_*.py`` module is
discovered automatically into the Linux group, so a new test file can never be
skipped silently.
"""
from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

SHARED = [
    "test_core",
    "test_version_consistency",
    "test_windows_state",
    "test_windows_power",
    "test_windows_remediation",
]
WINDOWS_NATIVE = ["test_windows_native"]


def discovered() -> list[str]:
    """Every test module under tests/, as importable module names."""
    return sorted(path.stem for path in TESTS.glob("test_*.py"))


def groups() -> dict[str, list[str]]:
    every = discovered()
    explicit = set(SHARED) | set(WINDOWS_NATIVE)
    linux = [name for name in every if name not in explicit]
    return {
        "shared": list(SHARED),
        "linux": linux,
        "windows-native": list(WINDOWS_NATIVE),
        # Windows-native tests skip themselves outside Windows.
        "all": every,
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    group = args[0] if args else "all"
    available = groups()
    if group not in available:
        raise SystemExit("Choose shared, linux, windows-native or all")
    sys.path[:0] = [str(ROOT / "src"), str(TESTS)]
    logging.disable(logging.CRITICAL)
    suite = unittest.defaultTestLoader.loadTestsFromNames(available[group])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
