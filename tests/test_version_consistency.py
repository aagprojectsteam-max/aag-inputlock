"""Prevent drift between human, runtime and packaging version declarations."""
import re
import tomllib
import unittest
from pathlib import Path

from locklock_core import VERSION

ROOT = Path(__file__).resolve().parents[1]


class VersionConsistencyTests(unittest.TestCase):
    def test_version_file_runtime_and_pyproject_match(self):
        public = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        self.assertEqual(public, VERSION)
        match = re.fullmatch(r"(\d+\.\d+\.\d+)-beta\.(\d+)", public)
        self.assertIsNotNone(match)
        self.assertEqual(project, f"{match.group(1)}b{match.group(2)}")

    def test_config_header_names_current_version(self):
        public = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        header = (ROOT / "config" / "input-lock.conf").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(header, f"# AAG LockLock {public} configuration")

    def test_readme_names_current_version(self):
        public = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertIn(f"# AAG LockLock {public}", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_mit_license_is_declared_and_present(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["license"], "MIT")
        self.assertEqual(project["license-files"], ["LICENSE"])
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 AAG Projects Team", text)


if __name__ == "__main__":
    unittest.main()
