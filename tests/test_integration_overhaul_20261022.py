"""Integration of the 2026-10-22 overhaul branches (A safety, B daemon,
C tray, D cli/installers): the interfaces between them, end to end.

Simulations only: temporary directories, fake clocks, fake systemctl output
and mocks. Nothing touches real devices, logind, systemd, UPower or dialogs.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import runpy  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

# These tests assert English output; the CLI/tray now follow the user's language (U7).
os.environ["INPUT_LOCK_UI_LANGUAGE"] = "en"

from test_daemon_logic import daemon_module  # noqa: E402  (installs evdev/pyudev stubs if needed)
from test_daemon_overhaul_20261022 import Clock, armed_daemon, base_daemon, drain, make_sample  # noqa: E402
from test_remediation_linux import daemon_fixture  # noqa: E402
import input_lock_cli as cli  # noqa: E402
import input_lock_common as common  # noqa: E402
import input_lock_safety as safety  # noqa: E402
from input_lock_lid_policy import UPOWER_DROPIN_CONTENT, UPowerPolicyController  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
LOGGER = "input-lock-daemon"
_ENVIRONMENT = mock.patch.dict(os.environ)


def setUpModule():
    # Never let a test talk to a real service manager's notify socket.
    _ENVIRONMENT.start()
    os.environ.pop("NOTIFY_SOCKET", None)


def tearDownModule():
    _ENVIRONMENT.stop()


# --------------------------------------------------------------- item 1

class ReceiptFixture:
    """Temporary root-owned and legacy receipt locations for the daemon."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "input-lock").mkdir()
        (base / "input-lock-protection").mkdir()
        self.breadcrumb = base / "input-lock" / "safety.json"
        self.root = base / "input-lock-protection" / safety.RESULT_NAME
        self.legacy = self.breadcrumb.with_name(safety.RESULT_NAME)
        self.patches = [mock.patch.object(daemon_module, "ROOT_RECEIPT", self.root),
                        mock.patch.object(daemon_module, "BREADCRUMB", self.breadcrumb)]
        for patch in self.patches:
            patch.start()

    def close(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def write(path: Path, result: str, generation: int, boot: str = "boot-x", **extra) -> None:
        status = {safety.DEFERRED_OS_SLEEP: "DEFERRED"}.get(result, "CANCELLED")
        path.write_text(json.dumps({"schema": 2, "boot_id": boot, "generation": generation,
                                    "result": result, "detail": extra.pop("detail", ""),
                                    "transition_status": status, **extra}))


def pending_daemon(generation: int = 7):
    daemon = base_daemon()
    daemon.boot_id = "boot-x"
    daemon.safety_receipt_error = False
    daemon.safety.decide("THERMAL_EMERGENCY")
    daemon.protective_result = {"reason": "THERMAL_EMERGENCY", "generation": generation,
                                "result": "delegated-to-inputlock"}
    return daemon


class ProtectiveReceiptLocationTests(unittest.TestCase):
    """1(a): root-owned receipt first, legacy location as the fallback."""

    def setUp(self):
        self.files = ReceiptFixture()

    def tearDown(self):
        self.files.close()

    def test_daemon_uses_the_safety_module_constants(self):
        # (The fixture patches the daemon's name; the safety module's is real.)
        self.assertEqual(safety.ROOT_RECEIPT, Path("/var/lib/input-lock-protection/safety-action-result.json"))
        self.assertEqual(daemon_module.RESULT_NAME, safety.RESULT_NAME)
        self.assertEqual(safety.BREADCRUMB.with_name(safety.RESULT_NAME),
                         Path("/var/lib/input-lock/safety-action-result.json"))
        self.assertEqual(daemon_module.DEFERRED_OS_SLEEP, safety.DEFERRED_OS_SLEEP)

    def test_root_receipt_is_preferred(self):
        daemon = pending_daemon()
        self.files.write(self.files.root, "cancelled-lid-open", 7)
        self.files.write(self.files.legacy, "suspend-requested", 7)
        daemon._safety_tick()
        self.assertEqual(daemon.protective_result["result"], "cancelled-lid-open")
        self.assertEqual(daemon.safety.state, "SAFE_NORMAL")

    def test_legacy_location_is_the_fallback(self):
        daemon = pending_daemon()
        self.files.write(self.files.legacy, "cancelled-before-dispatch", 7)
        daemon._safety_tick()
        self.assertEqual(daemon.protective_result["result"], "cancelled-before-dispatch")
        self.assertEqual(daemon.safety.state, "SAFE_NORMAL")

    def test_root_receipt_of_another_generation_or_boot_is_skipped(self):
        daemon = pending_daemon()
        self.files.write(self.files.root, "protection-failed", 6)
        self.files.write(self.files.legacy, "cancelled-lid-open", 7)
        daemon._safety_tick()
        self.assertEqual(daemon.protective_result["result"], "cancelled-lid-open")
        daemon = pending_daemon()
        self.files.write(self.files.root, "protection-failed", 7, boot="old-boot")
        self.files.legacy.unlink()
        daemon._safety_tick()
        self.assertEqual(daemon.protective_result["result"], "delegated-to-inputlock")
        self.assertEqual(daemon.safety.state, "PROTECTIVE_ACTION_PENDING")

    def test_unreadable_root_receipt_falls_back_and_otherwise_warns_once(self):
        daemon = pending_daemon()
        self.files.root.write_text("{not json")
        self.files.write(self.files.legacy, "cancelled-lid-open", 7)
        daemon._safety_tick()
        self.assertEqual(daemon.safety.state, "SAFE_NORMAL")
        daemon = pending_daemon()
        self.files.legacy.unlink()
        with self.assertLogs(LOGGER, "WARNING") as logs:
            daemon._safety_tick()
            daemon._safety_tick()
        self.assertEqual(sum("PROTECTIVE_RESULT_UNAVAILABLE" in line for line in logs.output), 1)
        self.assertEqual(daemon.safety.state, "PROTECTIVE_ACTION_PENDING")


class DeferredReceiptTests(unittest.TestCase):
    """1(b): deferred-os-sleep-in-progress releases the protective decision."""

    def setUp(self):
        self.files = ReceiptFixture()

    def tearDown(self):
        self.files.close()

    def test_deferred_result_releases_like_a_cancellation(self):
        daemon = pending_daemon()
        self.files.write(self.files.root, safety.DEFERRED_OS_SLEEP, 7,
                         detail="systemd-suspend.service ran 12s ago (result failed)")
        with self.assertLogs(LOGGER, "WARNING") as logs:
            daemon._safety_tick()
        self.assertEqual(daemon.safety.state, "SAFE_NORMAL")
        self.assertEqual(daemon.safety.reason, "PROTECTIVE_ACTION_DEFERRED")
        self.assertFalse(daemon.safety.armed)
        self.assertEqual(daemon.protective_result["transition_status"], "DEFERRED")
        self.assertIn("event=PROTECTIVE_ACTION_DEFERRED", "\n".join(logs.output))
        daemon._write_safety.assert_any_call(force=True)

    def test_transition_status_alone_is_enough(self):
        daemon = pending_daemon()
        self.files.root.write_text(json.dumps({"boot_id": "boot-x", "generation": 7,
                                               "result": "deferred-other", "transition_status": "DEFERRED"}))
        daemon._safety_tick()
        self.assertEqual(daemon.safety.state, "SAFE_NORMAL")

    def test_end_to_end_decision_deferred_during_a_sleep_window_then_normal(self):
        clock = Clock(100.0)
        daemon = armed_daemon(clock)
        daemon.boot_id = "boot-x"
        daemon.lid_is_closed = True
        daemon.safety_sample.lid_closed = True
        with mock.patch.object(daemon_module.time, "monotonic", clock), \
                mock.patch.object(daemon_module, "request_service") as request, \
                mock.patch.object(daemon_module.threading, "Thread") as thread:
            daemon._start_protection("THERMAL_EMERGENCY")
            thread.assert_called_once()
            self.assertEqual(daemon.safety.state, "PROTECTIVE_ACTION_PENDING")
            self.assertFalse(daemon.ignore_lid_close)
            generation = daemon.protective_result["generation"]
            # logind starts preparing a sleep; the helper finds it busy.
            drain(daemon, "prepare-sleep")
            self.files.write(self.files.root, safety.DEFERRED_OS_SLEEP, generation,
                             detail="logind is already preparing for sleep")
            daemon._safety_tick()
            self.assertEqual(daemon.safety.state, "SAFE_NORMAL")
            self.assertIsNone(daemon.deferred_protection)
            # After resume and the grace period, supervision runs normally:
            # nothing is armed, so nothing is requested or re-armed.
            clock.now = 150.0
            drain(daemon, "resume-sleep")
            clock.now = 150.0 + daemon_module.CONTROLLER_TIMEOUT + 1
            daemon.safety_sample = make_sample(clock.now, True, cpu=120.0)
            daemon.last_power_at = daemon.last_logind_at = clock.now
            daemon._handle_timers()
            daemon._safety_tick()
        request.assert_not_called()
        thread.assert_called_once()
        self.assertEqual(daemon.safety.state, "SAFE_NORMAL")
        self.assertFalse(daemon.ignore_lid_close)
        self.assertFalse(daemon.safety.armed)


class SafetySleepWindowHookTests(unittest.TestCase):
    """1(c)/(d): the daemon tells SafetyPolicy about the sleep window."""

    def test_logind_signal_opens_and_closes_the_policy_window(self):
        clock = Clock(100.0)
        daemon = base_daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "prepare-sleep")
            self.assertEqual(daemon.safety.sleep_window_since, 100.0)
            state = daemon._state_payload()
            self.assertTrue(state["safety"]["os_sleep_in_progress"])
            self.assertTrue(state["system_sleep_in_progress"])
            drain(daemon, "resume-sleep")
        self.assertIsNone(daemon.safety.sleep_window_since)
        self.assertFalse(daemon._state_payload()["safety"]["os_sleep_in_progress"])

    def test_sleep_hook_ipc_paths_use_the_same_hook(self):
        daemon = base_daemon(Clock(50.0))
        daemon._send_and_close = mock.Mock()
        daemon.safety.note_sleep_window = mock.Mock(wraps=daemon.safety.note_sleep_window)
        daemon._handle_request(9, {"cmd": "prepare-sleep"})
        self.assertTrue(daemon._send_and_close.call_args.args[1]["state"]["safety"]["os_sleep_in_progress"])
        daemon._handle_request(9, {"cmd": "resume-sleep"})
        self.assertEqual([c.args for c in daemon.safety.note_sleep_window.call_args_list], [(True,), (False,)])

    def test_window_timeout_also_closes_the_policy_window(self):
        clock = Clock(100.0)
        daemon = base_daemon(clock)
        with mock.patch.object(daemon_module.time, "monotonic", clock):
            drain(daemon, "prepare-sleep")
            clock.now += daemon_module.SLEEP_WINDOW_MAX_SECONDS
            daemon._handle_timers()
        self.assertIsNone(daemon.safety.sleep_window_since)

    def test_policy_status_window_is_bounded(self):
        clock = Clock(0.0)
        policy = safety.SafetyPolicy(clock=clock)
        policy.note_sleep_window(True)
        policy.note_sleep_window(True)  # idempotent, keeps the first start
        self.assertEqual(policy.sleep_window_since, 0.0)
        self.assertTrue(policy.snapshot()["os_sleep_in_progress"])
        clock.now = safety.SLEEP_WINDOW_MAX_SECONDS + 1
        self.assertFalse(policy.snapshot()["os_sleep_in_progress"])

    def test_missing_categories_reach_status_and_cli(self):
        daemon = base_daemon()
        daemon.safety.category_missing = {"power": 1.0, "chassis": 2.0}
        state = daemon._state_payload()
        self.assertEqual(state["safety"]["missing_categories"], ["chassis", "power"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_status(state)
        self.assertIn("Missing telemetry: chassis, power", out.getvalue())
        self.assertNotIn("OS sleep:", out.getvalue())
        state["system_sleep_in_progress"] = True
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.print_status(state)
        self.assertIn("OS sleep:         in progress", out.getvalue())


# --------------------------------------------------------------- item 2

class PowerHelperStopPathTests(unittest.TestCase):
    """S6: ExecStopPost never waits for a UPower restart under TimeoutStopSec=5s."""

    def controller(self, root: Path, **kwargs):
        config = root / "UPower.conf"
        config.write_text("[UPower]\nCriticalPowerAction=PowerOff\n")
        return UPowerPolicyController(config_path=config, dropin_dir=root, **kwargs)

    def test_no_wait_enqueues_restart_and_keeps_the_marker_for_the_next_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            restart, nowait = mock.Mock(), mock.Mock()
            active, runtime = mock.Mock(return_value=True), mock.Mock(return_value="PowerOff")
            controller = self.controller(root, restart=restart, restart_nowait=nowait,
                                         service_active=active, runtime_action=runtime)
            controller.dropin_path.write_text(UPOWER_DROPIN_CONTENT)
            result = controller.disable(wait=False)
            nowait.assert_called_once_with()
            restart.assert_not_called()
            active.assert_not_called()
            runtime.assert_not_called()
            self.assertFalse(controller.dropin_path.exists())
            self.assertTrue(result["restart_scheduled"])
            self.assertTrue((root / ".input-lock-restore-pending.json").exists())
            # The next ExecStartPre waits, verifies and clears the marker.
            self.assertTrue(controller.disable()["normal_os_policy"])
            restart.assert_called_once_with()
            self.assertFalse((root / ".input-lock-restore-pending.json").exists())

    def test_no_wait_without_a_legacy_override_does_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            nowait = mock.Mock()
            controller = self.controller(Path(directory), restart_nowait=nowait)
            self.assertFalse(controller.disable(wait=False)["restoration_required"])
            nowait.assert_not_called()

    def test_scheduled_restart_uses_no_block(self):
        with mock.patch("input_lock_lid_policy.subprocess.run") as run:
            UPowerPolicyController._schedule_upower_restart()
        command = run.call_args.args[0]
        self.assertEqual(command, ["systemctl", "restart", "--no-block", "upower.service"])
        self.assertLess(run.call_args.kwargs["timeout"], 5)

    def run_helper(self, *argv):
        controller = mock.Mock()
        controller.disable.return_value = {"restoration_required": False}
        with mock.patch.object(sys, "argv", ["input-lock-power-helper", *argv]), \
                mock.patch("os.geteuid", return_value=0), \
                mock.patch("input_lock_lid_policy.UPowerPolicyController", return_value=controller), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stop:
                runpy.run_path(str(REPO / "src" / "input-lock-power-helper"), run_name="__main__")
        self.assertEqual(stop.exception.code, 0)
        return controller

    def test_helper_flag(self):
        self.run_helper("disable", "--no-block").disable.assert_called_once_with(wait=False)
        self.run_helper("disable").disable.assert_called_once_with(wait=True)

    def test_unit_stop_path_is_non_blocking_and_within_the_probe_range(self):
        text = (REPO / "systemd" / "input-lock.service").read_text()
        directives = unit_directives(text)
        self.assertEqual(directives["ExecStopPost"],
                         ["+/usr/lib/input-lock/input-lock-power-helper disable --no-block"])
        self.assertEqual(directives["ExecStartPre"], ["+/usr/lib/input-lock/input-lock-power-helper disable"])
        stop = safety._seconds(directives["TimeoutStopSec"][-1])
        self.assertTrue(0 < stop <= 30)


# --------------------------------------------------------------- item 6

def unit_directives(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), []).append(value.strip())
    return values


def show_timespan(text: str) -> str:
    """Render a unit timespan the way "systemctl show" prints it."""
    seconds = int(safety.parse_usec(text) // 1_000_000)
    minutes, rest = divmod(seconds, 60)
    parts = ([f"{minutes}min"] if minutes else []) + ([f"{rest}s"] if rest or not minutes else [])
    return " ".join(parts)


def show_exec(commands: list[str]) -> str:
    rendered = []
    for command in commands:
        argv = command.lstrip("+-@!:")
        rendered.append(f"{{ path={argv.split()[0]} ; argv[]={argv} ; ignore_errors=no ; "
                        "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }")
    return " ".join(rendered)


def show_properties(text: str) -> dict[str, str]:
    """systemctl show-style properties for a unit, from its unit-file text."""
    unit = unit_directives(text)

    def last(key, default=""):
        return unit.get(key, [default])[-1]
    return {
        "LoadState": "loaded", "ActiveState": "inactive", "Result": "success",
        "ExecStart": show_exec(unit.get("ExecStart", [])),
        "ExecStopPost": show_exec(unit.get("ExecStopPost", [])),
        "OnFailure": " ".join(unit.get("OnFailure", [])),
        "WatchdogUSec": show_timespan(last("WatchdogSec", "0")),
        "Restart": last("Restart", "no"), "DropInPaths": "", "User": last("User"),
        "Type": last("Type", "simple"),
        "TimeoutStartUSec": show_timespan(last("TimeoutStartSec", "90s")),
        "TimeoutStopUSec": show_timespan(last("TimeoutStopSec", "90s")),
        "KillMode": last("KillMode", "control-group"),
        "InvocationID": "", "InactiveExitTimestampMonotonic": "0",
    }


SUSPEND_UNIT = """[Service]
Type=oneshot
ExecStart=/usr/lib/systemd/systemd-sleep suspend
"""


class ReadinessProbeAgainstMergedUnitsTests(unittest.TestCase):
    """The probe's range checks accept exactly the units this branch installs."""

    def setUp(self):
        self.units = {name: (REPO / "systemd" / name).read_text()
                      for name in ("input-lock.service", "input-lock-protect.service",
                                   "input-lock-failsafe.service")}
        self.units["systemd-suspend.service"] = SUSPEND_UNIT
        self.tmp = tempfile.TemporaryDirectory()
        self.latch = mock.patch.object(safety, "FAILURE_LATCH", Path(self.tmp.name) / "failure.json")
        self.latch.start()

    def tearDown(self):
        self.latch.stop()
        self.tmp.cleanup()

    def report(self, overrides=None):
        overrides = overrides or {}

        def run(command, **_kwargs):
            self.assertEqual(command[:2], ["/usr/bin/systemctl", "show"])
            props = show_properties(self.units[command[2]])
            props.update(overrides.get(command[2], {}))
            return SimpleNamespace(returncode=0, stdout="\n".join(f"{k}={v}" for k, v in props.items()) + "\n")
        status = lambda: {"supported": True, "blocked": False, "preparing": False,  # noqa: E731
                          "inputlock_holds_lid": False}
        return safety.protection_report(run=run, status=status, counter=lambda: 3)

    def test_merged_units_are_ready(self):
        self.assertEqual(self.report(), {"errors": [], "info": []})

    def test_runtime_watchdog_extension_is_accepted(self):
        self.assertEqual(self.report({"input-lock.service": {"WatchdogUSec": "5min"}})["errors"], [])

    def test_contract_changes_are_refused(self):
        for unit, props in (("input-lock.service", {"TimeoutStopUSec": "1min"}),
                            ("input-lock.service", {"WatchdogUSec": "6min"}),
                            ("input-lock.service", {"Restart": "on-failure"}),
                            ("input-lock-protect.service", {"TimeoutStartUSec": "2min 45s"}),
                            ("input-lock-failsafe.service", {"ExecStopPost": ""})):
            with self.subTest(unit=unit, props=props):
                self.assertTrue(self.report({unit: props})["errors"])

    def test_unit_timings_match_the_safety_constants(self):
        for name in ("input-lock-protect.service", "input-lock-failsafe.service"):
            timeout = safety._seconds(unit_directives(self.units[name])["TimeoutStartSec"][-1])
            self.assertEqual(timeout, 330)
            self.assertGreaterEqual(timeout, safety.OBSERVE_BUDGET_SECONDS + 60)
        self.assertEqual(safety.OBSERVE_BUDGET_SECONDS, 240)
        daemon_unit = unit_directives(self.units["input-lock.service"])
        self.assertEqual(daemon_unit["Type"], ["notify"])
        self.assertEqual(daemon_unit["NotifyAccess"], ["main"])
        self.assertEqual(daemon_unit["WatchdogSec"], ["20s"])
        self.assertEqual(daemon_module.WATCHDOG_NORMAL_USEC, 20_000_000)
        self.assertEqual(daemon_module.WATCHDOG_SLEEP_USEC, 300_000_000)


class ReadyNotificationTests(unittest.TestCase):
    """B1: READY=1 only after socket, device scan and the first state file."""

    def test_ready_after_setup_and_before_background_threads(self):
        order = []
        daemon = daemon_fixture()
        daemon.running = False
        daemon.wakeup_read = mock.Mock()
        for name in ("_install_signal_handlers", "_reset_lid_ignore_on_startup", "_setup_server",
                     "_setup_udev", "_rescan_devices", "_write_state", "_start_logind_monitor", "_cleanup"):
            setattr(daemon, name, mock.Mock(side_effect=lambda *_a, _n=name, **_k: order.append(_n)))
        with mock.patch.object(daemon_module, "sd_notify", side_effect=order.append), \
                mock.patch.object(daemon_module.threading, "Thread"):
            self.assertEqual(daemon.run(), 0)
        ready = order.index("READY=1")
        for step in ("_setup_server", "_rescan_devices", "_write_state"):
            self.assertLess(order.index(step), ready, step)
        self.assertGreater(order.index("_start_logind_monitor"), ready)

    def test_clients_retry_and_install_waits_for_the_socket(self):
        import errno
        self.assertEqual(cli._RETRYABLE_ERRNOS, frozenset({errno.ENOENT, errno.ECONNREFUSED}))
        self.assertEqual(cli.CONNECT_RETRY_SECONDS, 5.0)
        install = (REPO / "install.sh").read_text()
        start = install.index("systemctl start input-lock.service\n    wait_for_daemon")
        self.assertGreater(start, install.index("wait_for_daemon() {"))
        self.assertIn('[[ -S "$SOCKET_PATH" ]]', install)


class SleepResumeChainTests(unittest.TestCase):
    """Interface 4: hook 'pre'/'post' -> CLI -> daemon IPC."""

    def test_hook_cli_and_daemon_agree(self):
        hook = (REPO / "integration" / "input-lock-system-sleep").read_text()
        branches = dict(re.findall(r"^\s*(pre|post)\)\s*\n\s*/usr/bin/input-lock ([a-z-]+)", hook, re.M))
        self.assertEqual(branches, {"pre": "prepare-sleep", "post": "resume-sleep"})
        daemon = base_daemon()
        daemon._send_and_close = mock.Mock()
        for command in branches.values():
            self.assertIn(command, cli.INTERNAL_COMMANDS)
            # The hook runs as root; the CLI refuses these for anyone else.
            with (mock.patch.object(cli, "_request", return_value={"ok": True}) as request,
                  mock.patch.object(cli.os, "geteuid", return_value=0)):
                self.assertEqual(cli.main([command]), 0)
            self.assertEqual(request.call_args.args[0], {"cmd": command})
            self.assertTrue(request.call_args.kwargs["config_fallback"])
            daemon._handle_request(9, {"cmd": command})
            self.assertTrue(daemon._send_and_close.call_args.args[1]["ok"], command)


class ProtocolAndHotkeyTests(unittest.TestCase):
    def test_daemon_protocol_matches_the_cli(self):
        self.assertEqual(daemon_module.PROTOCOL, common.PROTOCOL_VERSION)
        daemon = base_daemon()
        daemon._send_and_close = mock.Mock()
        for request in ({"cmd": "status"}, {"cmd": "ping"}):
            daemon._handle_request(9, request)
            response = daemon._send_and_close.call_args.args[1]
            self.assertEqual(response["protocol"], common.PROTOCOL_VERSION)
            with mock.patch.object(cli, "_protocol_warned", False):
                error = io.StringIO()
                with contextlib.redirect_stderr(error):
                    cli._check_protocol(response)
                self.assertEqual(error.getvalue(), "")
        self.assertEqual(daemon._state_payload()["protocol"], common.PROTOCOL_VERSION)

    def test_set_hotkey_uses_the_shared_canonical_validation(self):
        daemon = base_daemon()
        daemon._send_and_close = mock.Mock()
        daemon._set_primary_hotkey = mock.Mock()
        daemon._handle_request(9, {"cmd": "set-hotkey", "hotkey": "alt+ctrl+k"})
        daemon._set_primary_hotkey.assert_called_once_with("CTRL+ALT+K")
        for weak in ("SHIFT+A", "CTRL+ALT+F21", "A+B"):
            daemon._handle_request(9, {"cmd": "set-hotkey", "hotkey": weak})
            self.assertFalse(daemon._send_and_close.call_args.args[1]["ok"], weak)
        daemon._set_primary_hotkey.assert_called_once()


class LidDisableKeyMappingTests(unittest.TestCase):
    """Interface 3: every key the daemon publishes reads as plain words."""

    def published_keys(self):
        keys = set(daemon_module._PROTECTION_LID_KEYS.values()) | set(daemon_module._LID_DISABLE_KEYS.values())
        # Reasons passed through unchanged by lid_disable_key(). The user's own
        # "lid-ignore-disabled" is never notified, so it needs no text.
        for raw in ("auto-unlock-timer", "daemon-shutdown", "controller-disconnected",
                    "safety-journal-failed", "protective-suspend:THERMAL_HIGH_RISK"):
            keys.add(daemon_module.lid_disable_key(raw))
        return keys

    def test_no_published_key_falls_back_to_the_generic_text(self):
        import input_lock_notifier as notifier
        for key in sorted(self.published_keys()):
            with self.subTest(key=key):
                text = notifier.describe_lid_disable(key, {})
                self.assertNotIn("turned it off (reason:", text)
                self.assertNotIn("without giving a reason", text)

    def test_tray_unlock_action_matches_the_normalised_key(self):
        from test_tray_overhaul_20261022 import bare_tray, ARMED, disarmed, event
        import input_lock_notifier as notifier
        key = daemon_module.lid_disable_key("ipc-emergency-unlock")
        self.assertEqual(key, "emergency-unlock")
        tray = bare_tray(**ARMED)
        tray.connected = True
        tray.cooling_verified = True
        tray._mark_tray_unlock()
        tray._handle_message(event(disarmed(last_lid_disable={
            "reason": key, "requested_by_user": False, "at": 1.0}), "ipc-emergency-unlock"))
        tray._notify.assert_called_once()
        self.assertEqual(tray._notify.call_args.args[1], notifier.TRAY_UNLOCK_LID_NOTE)
        self.assertNotIn("critical", tray._notify.call_args.kwargs)


# --------------------------------------------------------------- item 4

class DoctorProtectionTests(unittest.TestCase):
    def checks(self, report):
        lines = []
        cli.protection_checks(lambda *row: lines.append(row), report=report)
        return lines

    def test_ready(self):
        self.assertEqual([row[0] for row in self.checks(lambda: {"errors": [], "info": []})], ["PASS"])

    def test_stale_latch_and_drop_ins_are_information(self):
        rows = self.checks(lambda: {"errors": [], "info": [
            "failure latch from a previous boot (informational; acknowledge to archive it)"]})
        self.assertEqual(rows[0][0], "INFO")
        self.assertIn("previous boot", rows[0][2])
        self.assertEqual(rows[-1][0], "PASS")

    def test_active_latch_fails_with_the_recovery_command(self):
        rows = self.checks(lambda: {"errors": [safety.LATCH_ERROR], "info": []})
        self.assertEqual(rows, [("FAIL", "Protection failure latch", safety.LATCH_ERROR + "; " + cli.RECOVERY_HINT)])
        self.assertIn("acknowledge-failure", rows[0][2])

    def test_transient_unreadiness_is_a_warning(self):
        rows = self.checks(lambda: {"errors": ["ordinary suspend unavailable, blocked, or already in progress"],
                                    "info": []})
        self.assertEqual(rows[0][0], "WARNING")

    def test_probe_failure_is_a_warning(self):
        def broken():
            raise RuntimeError("no system bus")
        self.assertEqual(self.checks(broken)[0][0], "WARNING")

    def test_doctor_reads_a_real_stale_latch_through_protection_report(self):
        with tempfile.TemporaryDirectory() as directory:
            latch = Path(directory) / "failure.json"
            latch.write_text(json.dumps({"error": "x", "boot_id": "old", "suspend_success": 1}))
            with mock.patch.object(safety, "FAILURE_LATCH", latch), \
                    mock.patch.object(safety, "boot_id", return_value="new"):
                rows = self.checks(lambda: safety.protection_report(
                    run=mock.Mock(side_effect=RuntimeError("stop after latch")),
                    status=lambda: {"supported": True, "blocked": False, "preparing": False}, counter=lambda: 1))
        self.assertEqual(rows[0][0], "INFO")
        self.assertIn("previous boot", rows[0][2])


# --------------------------------------------------------------- item 5

def bash_array(text: str, name: str) -> list[str]:
    match = re.search(rf"^{name}=\(\n(.*?)^\)", text, re.M | re.S)
    assert match, name
    return [line.strip() for line in match.group(1).splitlines()
            if line.strip() and not line.strip().startswith("#")]


class InstallPayloadTests(unittest.TestCase):
    def setUp(self):
        self.install = (REPO / "install.sh").read_text()
        self.fixed = bash_array(self.install, "PAYLOAD_FIXED")
        self.modules = bash_array(self.install, "LIB_MODULES")
        self.programs = bash_array(self.install, "LIB_PROGRAMS")
        self.python_programs = bash_array(self.install, "PYTHON_PROGRAMS")
        self.managed = set(self.modules) | set(self.programs) | {"locklock_core"}

    def test_every_runtime_import_is_installed(self):
        sources = [REPO / "src" / name for name in self.modules + self.programs] + [REPO / "src" / "input-lock"]
        pending, seen = list(sources), set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            for module in re.findall(r"^\s*(?:from|import)\s+(input_lock_\w+|locklock_\w+|aag_\w+)",
                                     path.read_text(), re.M):
                if module == "locklock_core":
                    continue
                self.assertIn(module + ".py", self.modules, f"{path.name} imports {module}")
                pending.append(REPO / "src" / (module + ".py"))
        self.assertIn('"$PROJECT_DIR"/src/locklock_core/*.py', self.install)
        self.assertIn('install -m 0644 "$STAGE/src/locklock_core/"*.py /usr/lib/input-lock/locklock_core/',
                      self.install)

    def test_staged_payload_contains_every_installed_file(self):
        for name in self.modules + self.programs:
            self.assertIn("src/" + name, self.fixed)
        for name in self.programs + ["input-lock"]:
            if (REPO / "src" / name).read_text().startswith("#!/usr/bin/python3"):
                self.assertIn("src/" + name, self.python_programs)
        for directory in ("systemd", "integration", "gnome-extension"):
            for path in sorted((REPO / directory).iterdir()):
                self.assertIn(f"{directory}/{path.name}", self.fixed)
        for path in sorted((REPO / "gnome-extension").iterdir()):
            self.assertIn(f'"$STAGE/gnome-extension/{path.name}"', self.install)
            self.assertIn(f'"$PROJECT_DIR/gnome-extension/{path.name}"', self.install)

    def test_every_referenced_library_path_is_installed_and_never_pruned(self):
        referenced = set()
        files = [*(REPO / "systemd").iterdir(), REPO / "integration" / "input-lock-system-sleep",
                 *(REPO / "src").glob("input_lock_*.py"), *(REPO / "src").glob("input-lock*"),
                 REPO / "config" / "90-input-lock.rules"]
        for path in files:
            referenced |= set(re.findall(r"/usr/lib/input-lock/([A-Za-z0-9_][A-Za-z0-9_.@-]*)", path.read_text()))
        self.assertTrue(referenced)
        self.assertEqual(referenced - self.managed, set())
        # prune_unmanaged keeps exactly LIB_MODULES, LIB_PROGRAMS and locklock_core.
        prune = self.install[self.install.index("prune_unmanaged() {"):]
        self.assertIn('local -A managed=([locklock_core]=1)', prune)
        self.assertIn('for name in "${LIB_MODULES[@]}" "${LIB_PROGRAMS[@]}"; do', prune)
        # The hook and the units call /usr/bin/input-lock, installed from src/input-lock.
        self.assertIn('install -m 0755 "$STAGE/src/input-lock" /usr/bin/input-lock', self.install)


if __name__ == "__main__":
    unittest.main()
