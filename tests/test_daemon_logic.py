#!/usr/bin/python3
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))


def install_test_stubs() -> None:
    if importlib.util.find_spec("evdev") is None:
        evdev = types.ModuleType("evdev")
        ecodes = types.ModuleType("evdev.ecodes")
        names: dict[str, int] = {
            "EV_SYN": 0,
            "EV_KEY": 1,
            "EV_REL": 2,
            "EV_ABS": 3,
            "EV_SW": 5,
            "SYN_DROPPED": 3,
            "SYN_REPORT": 0,
            "KEY_F23": 193,
            "REL_X": 0,
            "REL_Y": 1,
            "ABS_X": 0,
            "ABS_Y": 1,
            "BTN_LEFT": 272,
            "BTN_TOUCH": 330,
            "BTN_TOOL_FINGER": 325,
            "INPUT_PROP_POINTER": 0,
            "INPUT_PROP_DIRECT": 1,
            "SW_LID": 0,
            "KEY_LEFTCTRL": 29,
            "KEY_RIGHTCTRL": 97,
            "KEY_LEFTSHIFT": 42,
            "KEY_RIGHTSHIFT": 54,
            "KEY_LEFTALT": 56,
            "KEY_RIGHTALT": 100,
            "KEY_LEFTMETA": 125,
            "KEY_RIGHTMETA": 126,
            "KEY_ENTER": 28,
        }
        for letter, code in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", (30,48,46,32,18,33,34,35,23,36,37,38,50,49,24,25,16,19,31,20,22,47,17,45,21,44)):
            names[f"KEY_{letter}"] = code
        for number in range(1, 13):
            names[f"KEY_F{number}"] = 58 + number if number <= 10 else 76 + number
        for name, value in names.items():
            setattr(ecodes, name, value)
        ecodes.ecodes = names
        evdev.ecodes = ecodes
        evdev.InputDevice = object
        evdev.list_devices = lambda: []
        sys.modules["evdev"] = evdev
        sys.modules["evdev.ecodes"] = ecodes

    if importlib.util.find_spec("pyudev") is None:
        pyudev = types.ModuleType("pyudev")
        pyudev.Context = object
        pyudev.Monitor = object
        pyudev.Devices = object
        pyudev.DeviceNotFoundError = OSError
        sys.modules["pyudev"] = pyudev


install_test_stubs()

import input_lock_daemon as daemon_module  # noqa: E402
from input_lock_daemon import (  # noqa: E402
    DeviceRecord,
    InputLockDaemon,
    TransitionError,
    parse_hotkey,
)


class HotkeyTests(unittest.TestCase):
    def test_allowed_copilot_setting_is_saved_without_mutating_hotkey_sets(self) -> None:
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        daemon.primary_hotkey = parse_hotkey("CTRL+ALT+Z")
        daemon.emergency_hotkey = parse_hotkey("CTRL+ALT+SHIFT+F12")
        daemon.locked_kinds = set()
        daemon.allowed_keys = ()
        daemon.allowed_key_codes = set()
        daemon._ensure_allowed_uinput = mock.Mock()
        daemon._close_allowed_uinput = mock.Mock()
        daemon._persist_user_settings = mock.Mock()
        daemon._write_state = mock.Mock()
        daemon._emit_state_event = mock.Mock()

        daemon._set_allowed_keys(("COPILOT",))

        self.assertEqual(daemon.allowed_keys, ("COPILOT",))
        daemon._persist_user_settings.assert_called_once_with(
            allowed_keys=("COPILOT",)
        )

    def test_copilot_is_forwarded_as_its_complete_hardware_chord(self) -> None:
        from evdev import ecodes

        class FakeUInput:
            def __init__(self) -> None:
                self.events: list[tuple[int, int, int]] = []

            def write(self, event_type: int, code: int, value: int) -> None:
                self.events.append((event_type, code, value))

        daemon = InputLockDaemon.__new__(InputLockDaemon)
        daemon.allowed_keys = ("COPILOT",)
        daemon.allowed_uinput = FakeUInput()
        daemon._write_virtual_allowed(ecodes.KEY_F23, 1)
        daemon._write_virtual_allowed(ecodes.KEY_F23, 0)

        self.assertEqual(
            daemon.allowed_uinput.events,
            [
                (ecodes.EV_KEY, ecodes.KEY_LEFTMETA, 1),
                (ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1),
                (ecodes.EV_KEY, ecodes.KEY_F23, 1),
                (ecodes.EV_KEY, ecodes.KEY_F23, 0),
                (ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0),
                (ecodes.EV_KEY, ecodes.KEY_LEFTMETA, 0),
            ],
        )

    def test_left_and_right_modifiers_match(self) -> None:
        spec = parse_hotkey("CTRL+ALT+Z")
        from evdev import ecodes

        self.assertTrue(
            spec.matches({ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTALT, ecodes.KEY_Z})
        )
        self.assertFalse(spec.matches({ecodes.KEY_LEFTCTRL, ecodes.KEY_Z}))

    def test_hotkey_signature_ignores_token_order(self) -> None:
        self.assertEqual(
            parse_hotkey("CTRL+ALT+SHIFT+F12").signature,
            parse_hotkey("ALT+SHIFT+CTRL+F12").signature,
        )

    def test_mode_sets(self) -> None:
        self.assertEqual(InputLockDaemon._mode_kinds("keyboard"), {"keyboard"})
        self.assertEqual(InputLockDaemon._mode_kinds("mouse"), {"mouse", "touchpad"})
        self.assertEqual(InputLockDaemon._mode_kinds("touchpad"), {"touchpad"})
        self.assertIn("touchscreen", InputLockDaemon._mode_kinds("all-input"))
        self.assertNotIn("touchscreen", InputLockDaemon._mode_kinds("all"))

    def test_timeout_validation(self) -> None:
        self.assertEqual(InputLockDaemon._validated_timeout(30), 30)
        self.assertIsNone(InputLockDaemon._validated_timeout(None))
        with self.assertRaises(ValueError):
            InputLockDaemon._validated_timeout(True)
        with self.assertRaises(ValueError):
            InputLockDaemon._validated_timeout(86_401)

    def test_settings_lock_kinds_validation(self) -> None:
        self.assertEqual(
            InputLockDaemon._validated_lock_kinds(
                ["keyboard", "mouse", "touchpad", "touchscreen"]
            ),
            {"keyboard", "mouse", "touchpad", "touchscreen"},
        )
        with self.assertRaises(ValueError):
            InputLockDaemon._validated_lock_kinds([])
        with self.assertRaises(ValueError):
            InputLockDaemon._validated_lock_kinds(["mouse"])

    def test_default_settings_toggle_uses_selected_kinds(self) -> None:
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        daemon.default_locked_kinds = {"keyboard", "touchscreen"}
        daemon.locked_kinds = set()
        daemon._set_locked_kinds = mock.Mock()

        daemon._toggle_default_kinds(reason="test")

        daemon._set_locked_kinds.assert_called_once_with(
            {"keyboard", "touchscreen"}, reason="test", timeout=None
        )

    def test_idle_timer_locks_selected_kinds(self) -> None:
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        daemon.clients = {}
        daemon.lid_lease_deadline = None
        daemon.next_safety_tick = float("inf")
        daemon.accept_retry_at = None
        daemon.auto_unlock_deadline = None
        daemon.idle_lock_enabled = True
        daemon.idle_lock_seconds = 600
        daemon.last_input_activity = 100.0
        daemon.authorized_session_active = True
        daemon.locked_kinds = set()
        daemon.default_locked_kinds = {"keyboard", "mouse", "touchpad"}
        daemon._set_locked_kinds = mock.Mock()

        with mock.patch.object(daemon_module.time, "monotonic", return_value=700.0):
            daemon._handle_timers()

        daemon._set_locked_kinds.assert_called_once_with(
            {"keyboard", "mouse", "touchpad"},
            reason="idle-auto-lock",
            timeout=None,
        )

    def test_cli_enter_release_is_waited_for(self) -> None:
        class FakeDevice:
            def __init__(self) -> None:
                self.states = [[28], []]

            def active_keys(self) -> list[int]:
                return self.states.pop(0)

        record = SimpleNamespace(
            path="/dev/input/event3",
            name="AT Translated Set 2 keyboard",
            device=FakeDevice(),
            pressed=set(),
            kinds=frozenset({"keyboard"}),
        )
        with mock.patch.object(daemon_module.time, "sleep") as sleep:
            InputLockDaemon._wait_for_input_release([record], timeout_seconds=1.0)
        sleep.assert_called_once()
        self.assertEqual(record.pressed, set())

    def test_stuck_key_is_still_refused(self) -> None:
        device = SimpleNamespace(active_keys=lambda: [28])
        record = SimpleNamespace(
            path="/dev/input/event3",
            name="AT Translated Set 2 keyboard",
            device=device,
            pressed=set(),
            kinds=frozenset({"keyboard"}),
        )
        with mock.patch.object(
            daemon_module.time, "monotonic", side_effect=[0.0, 1.0]
        ):
            with self.assertRaisesRegex(TransitionError, "remained pressed"):
                InputLockDaemon._wait_for_input_release(
                    [record], timeout_seconds=0.5
                )

    def test_lid_switch_grab_is_independent_from_input_lock(self) -> None:
        record = DeviceRecord(
            path="/dev/input/event0",
            device=SimpleNamespace(),
            name="Lid Switch",
            kinds=frozenset({"lid-switch"}),
            vendor_product="0000:0000",
            by_id=(),
            excluded=False,
            exclusion_reason="",
        )
        self.assertFalse(record.wants_managed_grab(set(), False))
        self.assertTrue(record.wants_managed_grab(set(), True))

    def test_lid_ignore_requires_detected_switch(self):
        from test_remediation_linux import daemon_fixture
        daemon = daemon_fixture()
        daemon.controller_fd = 3
        daemon.clients[3] = SimpleNamespace(deadline=999)
        with self.assertRaisesRegex(TransitionError, "no eligible lid switch"):
            daemon._set_ignore_lid_close(True, generation=0)

    def test_saved_preference_is_separate_from_safe_startup(self):
        from test_remediation_linux import daemon_fixture
        daemon = daemon_fixture()
        daemon._saved_lid_ignore_close = True
        with mock.patch.object(daemon_module, "BREADCRUMB", Path("/nonexistent-locklock-fixture")):
            daemon._reset_lid_ignore_on_startup()
        self.assertFalse(daemon.ignore_lid_close)
        self.assertTrue(daemon._saved_lid_ignore_close)
        daemon._persist_user_settings.assert_not_called()

    def test_sleep_inhibition_and_external_policy_commands_are_gone(self):
        # D1 (2026-10-22): the always-refusing sleep-inhibitor and external
        # power-policy stubs were removed; their IPC commands are unknown now.
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        for name in ("_sleep_should_be_inhibited", "_acquire_sleep_inhibitor",
                     "_set_lid_policy_health", "_set_lid_power_policy"):
            self.assertFalse(hasattr(daemon, name), name)
        daemon._send_and_close = mock.Mock()
        for cmd in ("set-lid-policy-health", "set-lid-power-policy"):
            daemon._handle_request(7, {"cmd": cmd, "healthy": True, "enabled": True, "threshold": 20})
            self.assertEqual(daemon._send_and_close.call_args.args[1],
                             {"ok": False, "error": "unsupported command"})

    def test_evdev_lid_state_takes_priority(self) -> None:
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        daemon.lid_is_closed = False
        daemon.lid_state_from_evdev = False
        daemon.safety_sample = None
        daemon._write_state = mock.Mock()
        daemon._emit_state_event = mock.Mock()

        daemon._handle_lid_switch_event(True)

        self.assertTrue(daemon.lid_is_closed)
        self.assertTrue(daemon.lid_state_from_evdev)
        daemon._emit_state_event.assert_called_once_with("lid-closed")

    def test_quit_request_stops_daemon_cleanly(self) -> None:
        daemon = InputLockDaemon.__new__(InputLockDaemon)
        daemon.running = True
        daemon._send_and_close = mock.Mock()
        daemon._wake = mock.Mock()

        daemon._handle_request(7, {"cmd": "quit"})

        self.assertFalse(daemon.running)
        daemon._send_and_close.assert_called_once_with(
            7, {"ok": True, "message": "shutdown scheduled"}
        )
        daemon._wake.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
