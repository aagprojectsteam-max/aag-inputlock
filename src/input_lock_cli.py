#!/usr/bin/python3
"""Command-line client for Input Lock."""

from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from input_lock_common import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_SOCKET_PATH,
    PROTOCOL_VERSION,
    ConfigError,
    ProtocolError,
    VERSION,
    canonical_hotkey,
    load_config,
    response_ok,
    send_request,
)

SERVICE_NAME = "input-lock.service"
# How long a command waits for the daemon socket while the unit is starting.
CONNECT_RETRY_SECONDS = 5.0
CONNECT_RETRY_INTERVAL = 0.25
_RETRYABLE_ERRNOS = frozenset({errno.ENOENT, errno.ECONNREFUSED})
# Internal commands (GNOME bindings, sleep hook, notifier stop) must never
# depend on parsing the configuration: they fall back to the default socket.
INTERNAL_COMMANDS = (
    "gnome-hotkey",
    "gnome-emergency",
    "session-ended",
    "prepare-sleep",
    "resume-sleep",
)

_protocol_warned = False


def _unit_state() -> str:
    """Return ``systemctl is-active`` for the daemon unit ("unknown" on error)."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _socket_path(*, fallback: bool) -> Path:
    """The configured socket; with ``fallback`` a config error uses the default."""
    if not fallback:
        return load_config().socket_path
    try:
        return load_config().socket_path
    except Exception as exc:
        print(
            f"input-lock: configuration cannot be parsed ({exc}); "
            f"using the default socket {DEFAULT_SOCKET_PATH}",
            file=sys.stderr,
        )
        return DEFAULT_SOCKET_PATH


def _retryable(exc: BaseException) -> bool:
    cause = exc.__cause__ if isinstance(exc, ProtocolError) else exc
    return isinstance(cause, OSError) and cause.errno in _RETRYABLE_ERRNOS


def _send_with_retry(
    payload: dict[str, object],
    socket_path: Path,
    timeout: float,
    retry_seconds: float,
    *,
    unit_state: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, object]:
    """Send a request, retrying a missing/refusing socket while the unit starts.

    The daemon creates its socket shortly after systemd starts it, so right
    after ``systemctl restart`` (or during an install) the first connection may
    see ENOENT or ECONNREFUSED. Retry only while the unit is activating or
    active; otherwise fail at once with a message naming the socket and unit.
    """

    unit_state = unit_state or _unit_state
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + max(retry_seconds, 0.0)
    while True:
        try:
            return send_request(payload, socket_path=socket_path, timeout=timeout)
        except ProtocolError as exc:
            if not _retryable(exc):
                raise
            state = unit_state()
            if state in {"active", "activating", "reloading"} and clock() < deadline:
                sleep(CONNECT_RETRY_INTERVAL)
                continue
            raise ProtocolError(
                f"cannot connect to the Input Lock daemon at {socket_path} "
                f"({exc}; {SERVICE_NAME} is {state}). "
                f"Check: systemctl status {SERVICE_NAME}"
            ) from exc


def _check_protocol(response: dict[str, object]) -> None:
    """Warn once per process when the daemon speaks another protocol version."""
    global _protocol_warned
    if _protocol_warned:
        return
    state = response.get("state")
    if "protocol" in response:
        reported = response.get("protocol")
    elif isinstance(state, dict):
        reported = state.get("protocol")
    else:
        return  # Responses without a state snapshot carry no version.
    if reported == PROTOCOL_VERSION:
        return
    _protocol_warned = True
    if reported is None:
        detail = "does not report a protocol version (it is probably older than this CLI)"
    else:
        detail = f"reports protocol {reported!r}"
    print(
        f"input-lock: warning: the running daemon {detail}; this CLI uses protocol "
        f"{PROTOCOL_VERSION}. Restart {SERVICE_NAME} after an update.",
        file=sys.stderr,
    )


def _request(
    payload: dict[str, object],
    timeout: float = 5.0,
    *,
    config_fallback: bool = False,
    retry_seconds: float = CONNECT_RETRY_SECONDS,
    check_protocol: bool = True,
) -> dict[str, object]:
    socket_path = _socket_path(fallback=config_fallback)
    response = _send_with_retry(payload, socket_path, timeout, retry_seconds)
    if check_protocol:
        _check_protocol(response)
    return response_ok(response)


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
    """Why lid-ignore can or cannot be armed right now, from daemon state.

    A daemon that reports its own verdict (``lid_ready`` plus an optional
    ``lid_ready_reason``) is authoritative; older daemons are interpreted
    from the safety sample.
    """
    daemon_ready = state.get("lid_ready")
    if isinstance(daemon_ready, bool):
        if daemon_ready:
            return True, "ready"
        reason = str(state.get("lid_ready_reason") or "the daemon reports lid-ignore is not ready")
        if "previous emergency suspend failed" in reason and RECOVERY_HINT not in reason:
            reason += "; " + RECOVERY_HINT
        return False, reason
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
    last_disable = state.get("last_lid_disable")
    if isinstance(last_disable, dict) and last_disable.get("reason"):
        who = "by user" if last_disable.get("requested_by_user") is True else "automatic"
        print(f"Last lid-ignore off: {last_disable.get('reason')} ({who})")
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


_GNOME_MODIFIERS = {
    "control": "CTRL",
    "ctrl": "CTRL",
    "primary": "CTRL",
    "alt": "ALT",
    "shift": "SHIFT",
    "super": "SUPER",
    "meta": "SUPER",
}


def gnome_binding_matches(binding: str, hotkey: str) -> bool:
    """True when a GNOME accelerator such as ``<Control><Alt><Shift>F12`` is ``hotkey``."""
    text = binding.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    modifiers: list[str] = []
    while text.startswith("<"):
        end = text.find(">")
        if end < 0:
            return False
        name = _GNOME_MODIFIERS.get(text[1:end].lower())
        if name is None:
            return False
        modifiers.append(name)
        text = text[end + 1:]
    if not text:
        return False
    try:
        return canonical_hotkey("+".join([*modifiers, text])) == canonical_hotkey(hotkey)
    except ConfigError:
        return False


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
        add("PASS", "Emergency hotkey", config.emergency_hotkey)
    except Exception as exc:
        config = None
        add("FAIL", "Configuration", str(exc))
        if "EMERGENCY_HOTKEY" in str(exc):
            add("FAIL", "Emergency hotkey",
                "invalid or equal to HOTKEY; the emergency unlock shortcut may not work")

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
        daemon_state = _state_from(
            _request({"cmd": "status"}, timeout=2.0, check_protocol=False)
        )
        protocol = daemon_state.get("protocol")
        if protocol == PROTOCOL_VERSION:
            add("PASS", "Daemon protocol",
                f"version {daemon_state.get('version', '?')}, protocol {protocol}")
        else:
            add("WARNING", "Daemon protocol",
                f"daemon version {daemon_state.get('version', '?')} reports protocol "
                f"{protocol if protocol is not None else 'none'}; this CLI uses "
                f"{PROTOCOL_VERSION}. Restart {SERVICE_NAME}.")
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
        if config is not None and emergency_path in paths:
            binding = _gsettings_get(
                "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:" + emergency_path,
                "binding",
            )
            if binding is None or not gnome_binding_matches(binding, config.emergency_hotkey):
                add("WARNING", "GNOME emergency binding",
                    f"{binding or 'unreadable'} does not match EMERGENCY_HOTKEY="
                    f"{config.emergency_hotkey}; run the update while logged in")
            else:
                add("PASS", "GNOME emergency binding", f"matches {config.emergency_hotkey}")

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
        # Never depend on a parseable configuration for the emergency path.
        response = _request(
            {"cmd": "emergency-unlock"}, timeout=2.0, config_fallback=True, retry_seconds=2.0
        )
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
    # Internal fixed commands used by GNOME, the sleep hook and the notifier
    # service. Without help= argparse leaves them out of the command list, and
    # the explicit metavar keeps them out of the usage line.
    public = [name for name in subparsers.choices]
    subparsers.metavar = "{" + ",".join(public) + "}"
    for name in INTERNAL_COMMANDS:
        subparsers.add_parser(name)
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
            hotkey = canonical_hotkey(args.hotkey)
            old_state = _state_from(_request({"cmd": "status"}))
            old_hotkey = str(old_state.get("primary_hotkey", "CTRL+ALT+Z"))
            from input_lock_gnome import set_primary

            if set_primary(hotkey) != 0:
                raise ProtocolError("the shortcut conflicts with another GNOME binding")
            try:
                response = _request({"cmd": "set-hotkey", "hotkey": hotkey})
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
        if args.command in INTERNAL_COMMANDS:
            # Called by GNOME, the sleep hook and the notifier unit: fall back
            # to the default socket on a config error and keep retries short.
            _request(
                {"cmd": args.command},
                timeout=2.0,
                config_fallback=True,
                retry_seconds=1.0,
                check_protocol=False,
            )
            return 0
    except Exception as exc:
        print(f"input-lock: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
