#!/usr/bin/python3
"""GNOME fallback bindings overhaul 2026-10-22 (interface 2, U6)."""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import input_lock_common  # noqa: E402
import input_lock_gnome as gnome  # noqa: E402


class CanonicalBindingTests(unittest.TestCase):
    def test_primary_control_case_and_order_are_equivalent(self):
        same = ["<Control><Alt>z", "<Primary><Alt>z", "<Alt><Ctrl>Z", "<control><ALT>z"]
        self.assertEqual({gnome.canonical_binding(value) for value in same}, {"<Control><Alt>z"})
        self.assertEqual(gnome.canonical_binding("<Shift><Primary><Alt>f12"),
                         "<Control><Alt><Shift>F12")
        self.assertNotEqual(gnome.canonical_binding("<Control><Alt>z"),
                            gnome.canonical_binding("<Control><Alt><Shift>z"))

    def test_find_collisions_uses_canonical_comparison(self):
        bindings = {"/a/": "<Primary><Alt>Z", "/b/": "<Super>e", "/c/": "<Shift><Control><Alt>F12"}
        with (mock.patch.object(gnome, "paths", return_value=[*bindings, gnome.PRIMARY_PATH]),
              mock.patch.object(gnome, "binding_for", side_effect=bindings.__getitem__)):
            found = gnome.find_collisions("<Control><Alt>z", "<Control><Alt><Shift>F12")
        self.assertEqual(sorted(path for path, _binding in found), ["/a/", "/c/"])


class EmergencyBindingFromConfigTests(unittest.TestCase):
    def test_configured_emergency_hotkey_is_used(self):
        config = SimpleNamespace(emergency_hotkey="CTRL+ALT+SHIFT+F11")
        with mock.patch.object(input_lock_common, "load_config", return_value=config):
            self.assertEqual(gnome.emergency_binding(), "<Control><Alt><Shift>F11")

    def test_unusable_config_falls_back_to_f12(self):
        for effect in (input_lock_common.ConfigError("bad"), OSError("unreadable")):
            with (mock.patch.object(input_lock_common, "load_config", side_effect=effect),
                  redirect_stderr(io.StringIO())):
                self.assertEqual(gnome.emergency_binding(), "<Control><Alt><Shift>F12")
        config = SimpleNamespace(emergency_hotkey="A+B")  # unsafe: not 2 modifiers
        with (mock.patch.object(input_lock_common, "load_config", return_value=config),
              redirect_stderr(io.StringIO())):
            self.assertEqual(gnome.emergency_binding(), gnome.EMERGENCY_BINDING)

    def test_install_writes_the_configured_emergency_binding(self):
        writes = []

        def run(*arguments):
            if arguments[0] == "set":
                writes.append(arguments)
            return "@as []"

        with (mock.patch.object(gnome, "run", side_effect=run),
              mock.patch.object(gnome, "paths", return_value=[]),
              mock.patch.object(gnome, "daemon_primary_hotkey", return_value="CTRL+ALT+Z"),
              mock.patch.object(gnome, "emergency_binding", return_value="<Control><Alt><Shift>F11"),
              redirect_stderr(io.StringIO()), mock.patch("sys.stdout", new=io.StringIO())):
            self.assertEqual(gnome.install(), 0)
        bindings = {args[1]: args[3] for args in writes if args[2] == "binding"}
        self.assertEqual(bindings[gnome.schema(gnome.EMERGENCY_PATH)], "'<Control><Alt><Shift>F11'")
        self.assertEqual(bindings[gnome.schema(gnome.PRIMARY_PATH)], "'<Control><Alt>z'")


class SetPrimaryTests(unittest.TestCase):
    def test_set_primary_errors_when_bindings_are_not_installed(self):
        with (mock.patch.object(gnome, "paths", return_value=[]),
              mock.patch.object(gnome, "emergency_binding", return_value=gnome.EMERGENCY_BINDING),
              mock.patch.object(gnome, "run") as run):
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                gnome.set_primary("CTRL+ALT+K")
        run.assert_not_called()

    def test_set_primary_refuses_the_configured_emergency_binding(self):
        with mock.patch.object(gnome, "emergency_binding", return_value="<Control><Alt><Shift>F11"):
            with self.assertRaisesRegex(ValueError, "differ"):
                gnome.set_primary("SHIFT+CTRL+ALT+F11")

    def test_set_primary_writes_when_registered(self):
        with (mock.patch.object(gnome, "paths", return_value=[gnome.PRIMARY_PATH]),
              mock.patch.object(gnome, "emergency_binding", return_value=gnome.EMERGENCY_BINDING),
              mock.patch.object(gnome, "find_collisions", return_value=[]),
              mock.patch.object(gnome, "run") as run,
              mock.patch("sys.stdout", new=io.StringIO())):
            self.assertEqual(gnome.set_primary("CTRL+ALT+K"), 0)
        run.assert_called_once_with("set", gnome.schema(gnome.PRIMARY_PATH), "binding",
                                    "'<Control><Alt>k'")


class ExtensionSourceTests(unittest.TestCase):
    SOURCE = Path(__file__).resolve().parents[1] / "gnome-extension" / "extension.js"

    def test_no_pointer_polling_and_watches_are_removed_on_disable(self):
        text = self.SOURCE.read_text()
        self.assertNotIn("POINTER_POLL_INTERVAL_MS", text)
        self.assertNotIn("get_pointer", text)
        self.assertIn("get_core_idle_monitor", text)
        self.assertIn("add_idle_watch", text)
        self.assertIn("add_user_active_watch", text)
        disable = text[text.index("    disable() {"):text.index("    _readSettings()")]
        self.assertIn("_stopWatching()", disable)
        self.assertIn("_showCursor()", disable)
        stop = text[text.index("    _stopWatching() {"):text.index("    _hideCursor()")]
        self.assertIn("remove_watch(this._idleWatchId)", stop)
        self.assertIn("remove_watch(this._activeWatchId)", stop)
        self.assertIn("_stopRecheck()", stop)


if __name__ == "__main__":
    unittest.main()
