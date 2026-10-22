"""CLI / common overhaul (2026-10-22): L1, L5, B1, V1, F7, D2, T1.

No daemon, no systemd and no real sleep: sockets live in temporary
directories and clocks, unit states and sleeps are injected.
"""
from __future__ import annotations

import contextlib
import errno
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import input_lock_cli as cli  # noqa: E402
from input_lock_common import (  # noqa: E402
    DEFAULT_SOCKET_PATH,
    PROTOCOL_VERSION,
    ConfigError,
    ProtocolError,
    canonical_hotkey,
    load_config,
)
from locklock_core import VERSION  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

BASE = """\
AUTHORIZED_UID=1000
AUTHORIZED_USER=tester
"""


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class CanonicalHotkeyTests(unittest.TestCase):
    def test_valid_shortcuts_are_canonicalised(self) -> None:
        for raw, expected in (
            ("CTRL+ALT+Z", "CTRL+ALT+Z"),
            ("alt+control+z", "CTRL+ALT+Z"),
            ("shift+alt+ctrl+f12", "CTRL+ALT+SHIFT+F12"),
            ("META+ALT+1", "ALT+SUPER+1"),
            (" ctrl + alt + key_x ", "CTRL+ALT+X"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_hotkey(raw), expected)

    def test_invalid_shortcuts_raise_config_error(self) -> None:
        for raw in (
            "CTRL+ALT+SHIFT+F21",  # review L1: accepted before, key does not exist
            "A+B",  # no modifiers
            "SHIFT+A",  # review L4: capital A would toggle the lock
            "CTRL+Z",  # one modifier
            "CTRL+ALT+ESC",
            "CTRL+ALT+Z+X",
            "CTRL+ALT+ALT+Z",
            "CTRL+ALT",
            "",
        ):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                canonical_hotkey(raw)


class LoadConfigHotkeyTests(unittest.TestCase):
    def load(self, extra: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input-lock.conf"
            path.write_text(BASE + extra, encoding="utf-8")
            return load_config(path)

    def test_defaults_and_canonical_values(self) -> None:
        config = self.load("")
        self.assertEqual((config.hotkey, config.emergency_hotkey), ("CTRL+ALT+Z", "CTRL+ALT+SHIFT+F12"))
        config = self.load("HOTKEY=alt+ctrl+x\nEMERGENCY_HOTKEY=shift+ctrl+alt+f11\n")
        self.assertEqual((config.hotkey, config.emergency_hotkey), ("CTRL+ALT+X", "CTRL+ALT+SHIFT+F11"))

    def test_invalid_emergency_hotkey_is_rejected_by_name(self) -> None:
        for value in ("CTRL+ALT+SHIFT+F21", "A+B", "SHIFT+F12"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigError, "EMERGENCY_HOTKEY"):
                    self.load(f"EMERGENCY_HOTKEY={value}\n")

    def test_invalid_primary_hotkey_is_rejected_by_name(self) -> None:
        with self.assertRaisesRegex(ConfigError, "^HOTKEY"):
            self.load("HOTKEY=SHIFT+A\n")

    def test_hotkeys_must_differ_canonically(self) -> None:
        with self.assertRaisesRegex(ConfigError, "must be different"):
            self.load("HOTKEY=ALT+CTRL+Z\nEMERGENCY_HOTKEY=ctrl+alt+z\n")


class SocketRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "control.sock"

    def test_missing_socket_is_retried_while_activating_then_named_clearly(self) -> None:
        clock = FakeClock()
        states: list[str] = []

        def unit_state() -> str:
            states.append("activating")
            return "activating"

        with self.assertRaises(ProtocolError) as caught:
            cli._send_with_retry({"cmd": "status"}, self.path, 1.0, 5.0,
                                 unit_state=unit_state, sleep=clock.sleep, clock=clock)
        message = str(caught.exception)
        self.assertIn(str(self.path), message)
        self.assertIn("systemctl status input-lock.service", message)
        self.assertGreaterEqual(sum(clock.sleeps), 5.0)
        self.assertLessEqual(sum(clock.sleeps), 5.0 + cli.CONNECT_RETRY_INTERVAL)
        self.assertGreater(len(states), 10)

    def test_inactive_unit_fails_at_once(self) -> None:
        clock = FakeClock()
        with self.assertRaisesRegex(ProtocolError, "is inactive"):
            cli._send_with_retry({"cmd": "status"}, self.path, 1.0, 5.0,
                                 unit_state=lambda: "inactive", sleep=clock.sleep, clock=clock)
        self.assertEqual(clock.sleeps, [])

    def test_connection_refused_is_retryable(self) -> None:
        bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(bound.close)
        bound.bind(str(self.path))  # bound but not listening -> ECONNREFUSED
        clock = FakeClock()
        with self.assertRaises(ProtocolError) as caught:
            cli._send_with_retry({"cmd": "status"}, self.path, 1.0, 1.0,
                                 unit_state=lambda: "active", sleep=clock.sleep, clock=clock)
        cause = caught.exception.__cause__
        self.assertIsInstance(cause.__cause__, OSError)
        self.assertEqual(cause.__cause__.errno, errno.ECONNREFUSED)
        self.assertTrue(clock.sleeps)

    def test_socket_that_appears_late_is_reached(self) -> None:
        def serve() -> None:
            time.sleep(0.4)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(self.path))
            server.listen(1)
            connection, _ = server.accept()
            with connection, server:
                connection.recv(4096)
                connection.sendall(b'{"ok":true,"state":{"protocol":3}}\n')

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        response = cli._send_with_retry({"cmd": "status"}, self.path, 2.0, 5.0,
                                        unit_state=lambda: "activating")
        thread.join(2)
        self.assertEqual(response["state"], {"protocol": 3})

    def test_other_protocol_errors_are_not_retried(self) -> None:
        clock = FakeClock()
        with mock.patch.object(cli, "send_request", side_effect=ProtocolError("daemon closed")):
            with self.assertRaisesRegex(ProtocolError, "daemon closed"):
                cli._send_with_retry({"cmd": "status"}, self.path, 1.0, 5.0,
                                     unit_state=lambda: "active", sleep=clock.sleep, clock=clock)
        self.assertEqual(clock.sleeps, [])


class ConfigFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        broken = Path(directory.name) / "broken.conf"
        broken.write_text(BASE + "EMERGENCY_HOTKEY=A+B\n", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"INPUT_LOCK_CONFIG": str(broken)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, argv, response):
        sent = []

        def fake(payload, socket_path, timeout, retry_seconds):
            sent.append((payload, socket_path, retry_seconds))
            return response

        stderr = io.StringIO()
        with (mock.patch.object(cli, "_send_with_retry", side_effect=fake),
              contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO())):
            code = cli.main(argv)
        return code, sent, stderr.getvalue()

    def test_internal_commands_use_the_default_socket(self) -> None:
        for command in ("gnome-emergency", "gnome-hotkey", "session-ended",
                        "prepare-sleep", "resume-sleep"):
            with self.subTest(command=command):
                code, sent, stderr = self.run_main([command], {"ok": True})
                self.assertEqual(code, 0)
                self.assertEqual(sent, [({"cmd": command}, DEFAULT_SOCKET_PATH, 1.0)])
                self.assertIn("default socket", stderr)
                self.assertIn("EMERGENCY_HOTKEY", stderr)

    def test_emergency_unlock_uses_the_default_socket(self) -> None:
        code, sent, stderr = self.run_main(
            ["emergency-unlock"], {"ok": True, "state": {"protocol": PROTOCOL_VERSION}})
        self.assertEqual(code, 0)
        self.assertEqual(sent[0][:2], ({"cmd": "emergency-unlock"}, DEFAULT_SOCKET_PATH))
        self.assertIn("default socket", stderr)

    def test_ordinary_commands_still_report_the_config_error(self) -> None:
        code, sent, stderr = self.run_main(["status"], {"ok": True, "state": {}})
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])
        self.assertIn("EMERGENCY_HOTKEY", stderr)


class ProtocolWarningTests(unittest.TestCase):
    def setUp(self) -> None:
        cli._protocol_warned = False
        self.addCleanup(setattr, cli, "_protocol_warned", False)

    def request_twice(self, response):
        stderr = io.StringIO()
        with (mock.patch.object(cli, "_socket_path", return_value=Path("/fixture.sock")),
              mock.patch.object(cli, "_send_with_retry", return_value=response),
              contextlib.redirect_stderr(stderr)):
            cli._request({"cmd": "status"})
            cli._request({"cmd": "status"})
        return stderr.getvalue()

    def test_missing_protocol_warns_once(self) -> None:
        output = self.request_twice({"ok": True, "state": {"version": "2.0.0-beta.4"}})
        self.assertEqual(output.count("warning"), 1)
        self.assertIn("does not report a protocol version", output)

    def test_different_protocol_warns_with_value(self) -> None:
        output = self.request_twice({"ok": True, "protocol": 2, "state": {"protocol": 2}})
        self.assertEqual(output.count("warning"), 1)
        self.assertIn("protocol 2", output)

    def test_matching_protocol_and_stateless_responses_are_silent(self) -> None:
        self.assertEqual(self.request_twice({"ok": True, "state": {"protocol": PROTOCOL_VERSION}}), "")
        self.assertEqual(self.request_twice({"ok": True, "devices": []}), "")

    def test_resume_sleep_is_a_hidden_command(self) -> None:
        with mock.patch.object(cli, "_request", return_value={"ok": True}) as request:
            self.assertEqual(cli.main(["resume-sleep"]), 0)
        self.assertEqual(request.call_args.args[0], {"cmd": "resume-sleep"})
        self.assertNotIn("resume-sleep", cli.build_parser().format_help())


class ReadinessAndBindingTests(unittest.TestCase):
    def test_daemon_lid_ready_verdict_is_authoritative(self) -> None:
        self.assertEqual(cli.lid_readiness({"lid_ready": True}), (True, "ready"))
        ready, why = cli.lid_readiness({"lid_ready": False, "lid_ready_reason": "lid is closed"})
        self.assertEqual((ready, why), (False, "lid is closed"))
        ready, why = cli.lid_readiness(
            {"lid_ready": False, "lid_ready_reason": "previous emergency suspend failed"})
        self.assertIn("acknowledge-failure", why)
        # Older daemons without the field still use the safety sample.
        self.assertEqual(cli.lid_readiness({}), (False, "safety telemetry not reported yet"))

    def test_gnome_binding_comparison(self) -> None:
        self.assertTrue(cli.gnome_binding_matches("'<Control><Alt><Shift>F12'", "CTRL+ALT+SHIFT+F12"))
        self.assertTrue(cli.gnome_binding_matches("<Shift><Alt><Primary>f12", "CTRL+ALT+SHIFT+F12"))
        self.assertTrue(cli.gnome_binding_matches("'<Control><Alt>z'", "CTRL+ALT+Z"))
        self.assertFalse(cli.gnome_binding_matches("'<Control><Alt><Shift>F12'", "CTRL+ALT+SHIFT+F11"))
        self.assertFalse(cli.gnome_binding_matches("''", "CTRL+ALT+SHIFT+F12"))
        self.assertFalse(cli.gnome_binding_matches("'<Hyper>F12'", "CTRL+ALT+SHIFT+F12"))


class HarnessTests(unittest.TestCase):
    def load_runner(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("run_tests_fixture", ROOT / "tools" / "run_tests.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # main() runs only under __main__
        return module

    def test_every_test_module_belongs_to_a_group(self) -> None:
        runner = self.load_runner()
        files = sorted(path.stem for path in (ROOT / "tests").glob("test_*.py"))
        groups = runner.groups()
        self.assertEqual(sorted(groups["all"]), files)
        covered = set(groups["shared"]) | set(groups["linux"]) | set(groups["windows-native"])
        self.assertEqual(covered, set(files))
        for name in ("test_lid_recovery_20261022", "test_hotplug_race",
                     "test_lid_ignore_suspend", "test_cli_overhaul_20261022"):
            self.assertIn(name, groups["linux"])

    def test_source_entry_point_prefers_the_checkout(self) -> None:
        result = subprocess.run([sys.executable, "-S", str(ROOT / "src" / "input-lock"), "--version"],
                                capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"input-lock {VERSION}")
        for name in ("input-lock", "input-lock-daemon"):
            text = (ROOT / "src" / name).read_text(encoding="utf-8")
            self.assertIn('(here / "input_lock_common.py").is_file()', text)


if __name__ == "__main__":
    unittest.main()
