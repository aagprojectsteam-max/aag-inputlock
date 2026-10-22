#!/usr/bin/python3
"""Command-line client for Input Lock."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from input_lock_common import (
    DEFAULT_CONFIG_PATH,
    ProtocolError,
    VERSION,
    load_config,
    response_ok,
    send_request,
)


def _request(payload: dict[str, object], timeout: float = 5.0) -> dict[str, object]:
    config = load_config()
    return response_ok(send_request(payload, socket_path=config.socket_path, timeout=timeout))


def _state_from(response: dict[str, object]) -> dict[str, object]:
    state = response.get("state")
    if not isinstance(state, dict):
        raise ProtocolError("daemon response does not contain state")
    return state


def _yes_no(value: object, yes: str = "Locked", no: str = "Unlocked") -> str:
    return yes if value is True else no


RECOVERY_HINT = ("review 'journalctl -u input-lock-failsafe.service', then run: "
                 "sudo /usr/bin/python3 /usr/lib/input-lock/input_lock_safety.py acknowledge-failure")


def lid_readiness(state: dict[str, object]) -> tuple[bool, str]:
    """Why lid-ignore can or cannot be armed right now, from daemon state."""
    safety = state.get("safety", {})
    sample = safety.get("sample") if isinstance(safety, dict) else None
    if not isinstance(sample, dict):
        return False, "safety telemetry not reported yet"
    protection = sample.get("protection_errors") or []
    if protection:
        reason = "; ".join(str(item) for item in protection)
        if "previous emergency suspend failed" in reason:
            reason += "; " + RECOVERY_HINT
        return False, reason
    if sample.get("errors"):
        return False, "telemetry errors: " + "; ".join(str(item) for item in sample["errors"])
    if sample.get("stack_ready") is not True:
        return False, "native suspend readiness unavailable"
    if sample.get("dependencies_ok") is not True:
        return False, "power/logind supervision is not fresh"
    return True, "ready"


def print_status(state: dict[str, object]) -> None:
    locked = state.get("locked", {})
    if not isinstance(locked, dict):
        locked = {}
    print("Input Lock Status")
    print("-----------------")
    print(f"Keyboard:    {_yes_no(locked.get('keyboard'))}")
    print(f"Mouse:       {_yes_no(locked.get('mouse'))}")
    print(f"Touchpad:    {_yes_no(locked.get('touchpad'))}")
    print(f"Touchscreen: {_yes_no(locked.get('touchscreen'))}")
    print(f"Active devices:   {state.get('active_devices', '?')}")
    print(f"Grabbed devices:  {state.get('grabbed_devices', '?')}")
    print(f"Primary hotkey:   {state.get('primary_hotkey', '?')}")
    print(f"Emergency hotkey: {state.get('emergency_hotkey', '?')}")
    print(
        "Lid close:       "
        + ("Keep running" if state.get("ignore_lid_close") is True else "Normal action")
    )
    print(
        "Lid inhibitor:   "
        + ("Active" if state.get("lid_inhibitor_active") is True else "Inactive")
    )
    print(
        "Lid supervision:  "
        + (
            "Verified"
            if state.get("lid_policy_healthy") is True
            else "Safety unavailable"
            if state.get("ignore_lid_close") is True
            else "Normal policy"
        )
    )
    safety = state.get("safety", {})
    if isinstance(safety, dict):
        print(f"Safety state:     {safety.get('state', 'unknown')}")
        print(f"Measured thermal: {safety.get('thermal', 'UNKNOWN')}")
        print(f"Lid protection:   {safety.get('thermal_protection', 'UNKNOWN')}")
        print(f"Safety reason:    {safety.get('reason', 'unknown')}")
    ready, why = lid_readiness(state)
    print("Lid-ignore ready: " + ("yes" if ready else "no: " + why))
    print(f"Lid switches:     {state.get('lid_switch_devices', '?')}")
    print(f"Auto-unlock:      {state.get('auto_unlock', '?')}")
    print(f"Daemon:           {state.get('daemon', 'unknown')}")
    incomplete = state.get("incomplete_devices", [])
    if isinstance(incomplete, list) and incomplete:
        print("WARNING: requested lock is incomplete for:")
        for path in incomplete:
            print(f"  - {path}")


def print_devices(devices: object) -> None:
    if not isinstance(devices, list):
        raise ProtocolError("daemon response does not contain a device list")
    if not devices:
        print("No lockable keyboard, mouse, touchpad, or touchscreen was detected.")
        return
    for item in devices:
        if not isinstance(item, dict):
            continue
        flags: list[str] = []
        if item.get("grabbed") is True:
            flags.append("GRABBED")
        if item.get("excluded") is True:
            flags.append("EXCLUDED")
        suffix = f" [{' '.join(flags)}]" if flags else ""
        classes = ", ".join(str(part) for part in item.get("classes", []))
        print(f"{item.get('path')}  {item.get('name')}{suffix}")
        print(f"  Classes: {classes or '-'}")
        print(f"  Vendor:Product: {item.get('vendor_product', '-')}")
        for link in item.get("by_id", []):
            print(f"  Stable path: {link}")
        if item.get("exclusion_reason"):
            print(f"  Exclusion: {item.get('exclusion_reason')}")


def _run(command: Sequence[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def _gsettings_get(schema_path: str, key: str) -> str | None:
    if shutil.which("gsettings") is None:
        return None
    result = _run(["gsettings", "get", schema_path, key])
    return result.stdout.strip() if result.returncode == 0 else None


def run_doctor() -> int:
    checks: list[tuple[str, str, str]] = []

    def add(level: str, name: str, detail: str) -> None:
        checks.append((level, name, detail))

    os_release = _read_os_release()
    os_name = os_release.get("PRETTY_NAME", platform.platform())
    if os_release.get("ID") == "ubuntu" and os_release.get("VERSION_ID") == "26.04":
        add("PASS", "Operating system", os_name)
    else:
        add("WARNING", "Operating system", f"Designed for Ubuntu 26.04; found {os_name}")

    session_type = os.environ.get("XDG_SESSION_TYPE", "unknown")
    add("PASS" if session_type == "wayland" else "WARNING", "Session", session_type)

    if shutil.which("gnome-shell"):
        result = _run(["gnome-shell", "--version"])
        detail = result.stdout.strip() or result.stderr.strip()
        add("PASS" if result.returncode == 0 else "WARNING", "GNOME Shell", detail)
    else:
        add("WARNING", "GNOME Shell", "gnome-shell command not found")

    try:
        config = load_config(DEFAULT_CONFIG_PATH)
        add("PASS", "Configuration", str(DEFAULT_CONFIG_PATH))
    except Exception as exc:
        config = None
        add("FAIL", "Configuration", str(exc))

    required_modules = ("evdev", "pyudev", "dbus", "gi")
    missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
    if missing:
        add("FAIL", "Python dependencies", "missing: " + ", ".join(missing))
    else:
        add("PASS", "Python dependencies", "all required modules available")

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3, Gtk
        del AyatanaAppIndicator3, Gtk  # Import capability probe; no UI is created.

        add("PASS", "Tray dependencies", "GTK 3 and Ayatana AppIndicator available")
    except Exception as exc:
        add("FAIL", "Tray dependencies", str(exc))

    service = _run(["systemctl", "is-active", "input-lock.service"])
    service_active = service.returncode == 0 and service.stdout.strip() == "active"
    add("PASS" if service_active else "FAIL", "System service", service.stdout.strip() or "inactive")

    daemon_state: dict[str, object] | None = None
    try:
        daemon_state = _state_from(_request({"cmd": "status"}, timeout=2.0))
        add("PASS", "Daemon protocol", f"version {daemon_state.get('version', '?')}")
        if daemon_state.get("incomplete_devices"):
            add("WARNING", "Lock completeness", "one or more devices are not grabbed")
        else:
            add("PASS", "Lock completeness", "no incomplete lock currently reported")
        count = int(daemon_state.get("active_devices", 0))
        add("PASS" if count else "FAIL", "Input detection", f"{count} managed event nodes")
        lid_count = int(daemon_state.get("lid_switch_devices", 0))
        add(
            "PASS" if lid_count else "WARNING",
            "Lid switch detection",
            f"{lid_count} lid switch event node(s)",
        )
    except Exception as exc:
        add("FAIL", "Daemon protocol", str(exc))

    if shutil.which("notify-send"):
        add("PASS", "Notifications", "notify-send is installed")
    else:
        add("WARNING", "Notifications", "notify-send is missing; locking still works")
    add(
        "PASS" if os.environ.get("DBUS_SESSION_BUS_ADDRESS") else "WARNING",
        "Session D-Bus",
        "available" if os.environ.get("DBUS_SESSION_BUS_ADDRESS") else "not present in this shell",
    )

    tray_service = _run(["systemctl", "--user", "is-active", "input-lock-notifier.service"])
    tray_active = tray_service.returncode == 0 and tray_service.stdout.strip() == "active"
    add(
        "PASS" if tray_active else "WARNING",
        "Tray service",
        tray_service.stdout.strip() or tray_service.stderr.strip() or "inactive",
    )

    shortcut_root = "org.gnome.settings-daemon.plugins.media-keys"
    paths_text = _gsettings_get(shortcut_root, "custom-keybindings")
    primary_path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock/"
    emergency_path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock-emergency/"
    if paths_text is None:
        add("WARNING", "GNOME shortcuts", "gsettings unavailable outside the graphical session")
    else:
        try:
            from input_lock_gnome import parse_paths
            paths = parse_paths(paths_text)
        except Exception:
            paths = []
        missing_paths = [path for path in (primary_path, emergency_path) if path not in paths]
        if missing_paths:
            add("WARNING", "GNOME shortcuts", "one or both fallback bindings are absent")
        else:
            add("PASS", "GNOME shortcuts", "primary and emergency fallback bindings installed")

    if daemon_state is not None and config is not None:
        try:
            devices = _request({"cmd": "list-devices"}, timeout=2.0).get("devices", [])
            excluded_keyboards = [
                item
                for item in devices
                if isinstance(item, dict)
                and item.get("excluded") is True
                and "keyboard" in item.get("classes", [])
            ]
            if excluded_keyboards:
                add("PASS", "Emergency device", f"{len(excluded_keyboards)} excluded keyboard node(s)")
            else:
                add("WARNING", "Emergency device", "none configured; hotkeys and auto-unlock remain available")
        except Exception as exc:
            add("WARNING", "Emergency device", str(exc))
        add(
            "PASS" if config.auto_unlock_seconds > 0 else "WARNING",
            "Default auto-unlock",
            f"{config.auto_unlock_seconds}s" if config.auto_unlock_seconds else "disabled (enable for first test)",
        )
        safety = daemon_state.get("safety", {})
        sample = safety.get("sample") if isinstance(safety, dict) else None
        ready, why = lid_readiness(daemon_state)
        add("PASS" if ready else "WARNING", "Supervised lid readiness",
            "native protection and telemetry available" if ready else "lid-ignore cannot be armed: " + why)
        if daemon_state.get("ignore_lid_close") is True:
            healthy = ready and daemon_state.get("lid_inhibitor_active") is True and daemon_state.get("lid_controller_connected") is True
            add("PASS" if healthy else "FAIL", "Lid supervision", str(safety.get("state", "unknown")))
        add("INFO", "Critical battery policy", "LockLock does not suppress UPower; platform critical-action acceptance is separate")

    print("Input Lock Doctor")
    print("=================")
    for level, name, detail in checks:
        print(f"[{level:7}] {name}: {detail}")
    failures = sum(level == "FAIL" for level, _name, _detail in checks)
    warnings = sum(level == "WARNING" for level, _name, _detail in checks)
    print("-----------------")
    print(f"Summary: {len(checks) - failures - warnings} PASS, {warnings} WARNING, {failures} FAIL")
    return 1 if failures else 0


def emergency_unlock() -> int:
    try:
        response = _request({"cmd": "emergency-unlock"}, timeout=2.0)
        print_status(_state_from(response))
        return 0
    except Exception as exc:
        if os.geteuid() != 0:
            print(f"Daemon unlock failed: {exc}", file=sys.stderr)
            print("Recovery: sudo systemctl stop input-lock.service", file=sys.stderr)
            return 2
        print(f"Daemon did not respond ({exc}); stopping the service to close all grabs.", file=sys.stderr)
        result = subprocess.run(
            ["systemctl", "stop", "input-lock.service"], check=False
        )
        if result.returncode == 0:
            print("Input Lock service stopped; kernel file descriptors were released.")
            return 0
        return result.returncode or 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="input-lock",
        description="Safely lock and unlock local evdev input devices.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="show lock state")
    status.add_argument("--json", action="store_true", dest="as_json")
    subparsers.add_parser("list-devices", help="show classified input devices")
    for action in ("lock", "unlock", "toggle"):
        action_parser = subparsers.add_parser(action, help=f"{action} an input class")
        action_parser.add_argument(
            "mode",
            choices=(
                "all",
                "all-input",
                "keyboard",
                "mouse",
                "touchpad",
                "touchscreen",
            ),
        )
        action_parser.add_argument(
            "--timeout",
            type=int,
            default=None,
            metavar="SECONDS",
            help="auto-unlock after 0..86400 seconds (0 disables)",
        )
    subparsers.add_parser("emergency-unlock", help="release every input class")
    change = subparsers.add_parser("set-hotkey", help="change the primary shortcut")
    change.add_argument("hotkey", help="for example CTRL+ALT+Z")
    lid = subparsers.add_parser("lid-ignore", help="ignore or restore lid-close events")
    lid.add_argument("state", choices=("on", "off"))
    subparsers.add_parser("doctor", help="run installation and safety checks")
    logs = subparsers.add_parser("logs", help="show recent daemon journal")
    logs.add_argument("-n", "--lines", type=int, default=200)
    subparsers.add_parser("reload", help="validate and reload configuration")
    # Internal fixed commands used by GNOME and the notifier service.
    subparsers.add_parser("gnome-hotkey", help=argparse.SUPPRESS)
    subparsers.add_parser("gnome-emergency", help=argparse.SUPPRESS)
    subparsers.add_parser("session-ended", help=argparse.SUPPRESS)
    subparsers.add_parser("prepare-sleep", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            state = _state_from(_request({"cmd": "status"}))
            if args.as_json:
                print(json.dumps(state, ensure_ascii=False, indent=2))
            else:
                print_status(state)
            return 0
        if args.command == "list-devices":
            print_devices(_request({"cmd": "list-devices"}).get("devices"))
            return 0
        if args.command in {"lock", "unlock", "toggle"}:
            if args.timeout is not None and not 0 <= args.timeout <= 86_400:
                raise ProtocolError("--timeout must be between 0 and 86400")
            payload: dict[str, object] = {"cmd": args.command, "mode": args.mode}
            if args.timeout is not None:
                payload["timeout"] = args.timeout
            print_status(_state_from(_request(payload)))
            return 0
        if args.command == "emergency-unlock":
            return emergency_unlock()
        if args.command == "set-hotkey":
            old_state = _state_from(_request({"cmd": "status"}))
            old_hotkey = str(old_state.get("primary_hotkey", "CTRL+ALT+Z"))
            from input_lock_gnome import set_primary

            if set_primary(args.hotkey) != 0:
                raise ProtocolError("the shortcut conflicts with another GNOME binding")
            try:
                response = _request({"cmd": "set-hotkey", "hotkey": args.hotkey})
            except Exception:
                try:
                    set_primary(old_hotkey)
                except Exception:
                    pass
                raise
            print_status(_state_from(response))
            return 0
        if args.command == "lid-ignore":
            enabled = args.state == "on"
            state = _state_from(_request({"cmd": "status"}))
            response = _request({"cmd": "set-lid-ignore", "enabled": enabled,
                                 "generation": state.get("lid_generation")})
            print_status(_state_from(response))
            return 0
        if args.command == "doctor":
            return run_doctor()
        if args.command == "logs":
            lines = min(max(args.lines, 1), 5000)
            return subprocess.run(
                ["journalctl", "--no-pager", "-u", "input-lock.service", "-n", str(lines)],
                check=False,
            ).returncode
        if args.command == "reload":
            response = _request({"cmd": "reload"})
            print(response.get("message", "reload scheduled"))
            return 0
        if args.command in {
            "gnome-hotkey",
            "gnome-emergency",
            "session-ended",
            "prepare-sleep",
        }:
            _request({"cmd": args.command}, timeout=2.0)
            return 0
    except Exception as exc:
        print(f"input-lock: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
