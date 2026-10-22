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
import input_lock_i18n as i18n
from input_lock_i18n import N_, _, ngettext
from input_lock_messages import daemon_text, describe_lid_disable, protection_text, thermal_text

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
# Sent only by the system-sleep hook, which runs as root; the daemon refuses
# them from every other peer (review S-R1), so a user who types them gets a
# clear explanation instead of a protocol error.
ROOT_ONLY_COMMANDS = ("prepare-sleep", "resume-sleep")

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
            _("input-lock: configuration cannot be parsed ({error}); "
              "using the default socket {path}").format(error=exc, path=DEFAULT_SOCKET_PATH),
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
                _("cannot connect to the Input Lock daemon at {path} "
                  "({error}; {unit} is {state}). "
                  "Check: systemctl status {unit}").format(
                    path=socket_path, error=exc, unit=SERVICE_NAME, state=state)
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
        message = _("input-lock: warning: the running daemon does not report a protocol version "
                    "(it is probably older than this CLI); this CLI uses protocol {protocol}. "
                    "Restart {unit} after an update.")
    else:
        message = _("input-lock: warning: the running daemon reports protocol {reported}; this CLI "
                    "uses protocol {protocol}. Restart {unit} after an update.")
    print(
        message.format(reported=repr(reported), protocol=PROTOCOL_VERSION, unit=SERVICE_NAME),
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
        raise ProtocolError(_("daemon response does not contain state"))
    return state


def _yes_no(value: object, yes: str = N_("Locked"), no: str = N_("Unlocked")) -> str:
    return _(yes) if value is True else _(no)


# English on purpose: readiness reasons are data; daemon_text() translates them.
RECOVERY_HINT = N_("review 'journalctl -u input-lock-failsafe.service', then run: "
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
    title = _("Input Lock Status")
    print(title)
    print("-" * len(title))
    print(_("Keyboard:    {state}").format(state=_yes_no(locked.get("keyboard"))))
    print(_("Mouse:       {state}").format(state=_yes_no(locked.get("mouse"))))
    print(_("Touchpad:    {state}").format(state=_yes_no(locked.get("touchpad"))))
    print(_("Touchscreen: {state}").format(state=_yes_no(locked.get("touchscreen"))))
    print(_("Active devices:   {value}").format(value=state.get("active_devices", "?")))
    print(_("Grabbed devices:  {value}").format(value=state.get("grabbed_devices", "?")))
    print(_("Primary hotkey:   {value}").format(value=state.get("primary_hotkey", "?")))
    print(_("Emergency hotkey: {value}").format(value=state.get("emergency_hotkey", "?")))
    print(_("Lid close:       {value}").format(
        value=_("Keep running") if state.get("ignore_lid_close") is True else _("Normal action")))
    print(_("Lid inhibitor:   {value}").format(
        value=_("Active") if state.get("lid_inhibitor_active") is True else _("Inactive")))
    print(_("Lid supervision:  {value}").format(
        value=(
            _("Verified")
            if state.get("lid_policy_healthy") is True
            else _("Safety unavailable")
            if state.get("ignore_lid_close") is True
            else _("Normal policy")
        )))
    safety = state.get("safety", {})
    if isinstance(safety, dict):
        # State and reason codes are identifiers and stay as published.
        print(_("Safety state:     {value}").format(value=safety.get("state", _("unknown"))))
        print(_("Measured thermal: {value}").format(value=thermal_text(safety.get("thermal", "UNKNOWN"))))
        print(_("Lid protection:   {value}").format(
            value=protection_text(safety.get("thermal_protection", "UNKNOWN"))))
        print(_("Safety reason:    {value}").format(value=safety.get("reason", _("unknown"))))
        missing = safety.get("missing_categories")
        if isinstance(missing, list) and missing:
            print(_("Missing telemetry: {value}").format(value=", ".join(str(item) for item in missing)))
    sleeping = state.get("system_sleep_in_progress") is True or (
        isinstance(safety, dict) and safety.get("os_sleep_in_progress") is True)
    if sleeping:
        print(_("OS sleep:         in progress (no protective action meanwhile)"))
    ready, why = lid_readiness(state)
    if ready:
        print(_("Lid-ignore ready: yes"))
    else:
        print(_("Lid-ignore ready: no: {reason}").format(reason=daemon_text(why)))
    last_disable = state.get("last_lid_disable")
    if isinstance(last_disable, dict) and last_disable.get("reason"):
        by_user = last_disable.get("requested_by_user") is True
        # user_initiated: another user action (emergency shortcut, settings
        # change) turned it off as a side effect (review U-R5).
        if by_user:
            who = _("by user")
        elif last_disable.get("user_initiated") is True:
            who = _("side effect of a user action")
        else:
            who = _("automatic")
        print(_("Last lid-ignore off: {reason} ({who})").format(
            reason=last_disable.get("reason"), who=who))
        if not by_user:
            print("  " + describe_lid_disable(str(last_disable.get("reason")), state))
    print(_("Lid switches:     {value}").format(value=state.get("lid_switch_devices", "?")))
    print(_("Auto-unlock:      {value}").format(value=state.get("auto_unlock", "?")))
    print(_("Daemon:           {value}").format(value=state.get("daemon", _("unknown"))))
    incomplete = state.get("incomplete_devices", [])
    if isinstance(incomplete, list) and incomplete:
        print(_("WARNING: requested lock is incomplete for:"))
        for path in incomplete:
            print(f"  - {path}")


def print_devices(devices: object) -> None:
    if not isinstance(devices, list):
        raise ProtocolError(_("daemon response does not contain a device list"))
    if not devices:
        print(_("No lockable keyboard, mouse, touchpad, or touchscreen was detected."))
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
        print(_("  Classes: {value}").format(value=classes or "-"))
        print(_("  Vendor:Product: {value}").format(value=item.get("vendor_product", "-")))
        for link in item.get("by_id", []):
            print(_("  Stable path: {value}").format(value=link))
        if item.get("exclusion_reason"):
            print(_("  Exclusion: {value}").format(value=item.get("exclusion_reason")))


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
    found = _binding_hotkey(binding)
    if found is None:
        return False
    try:
        return found == canonical_hotkey(hotkey)
    except ConfigError:
        return False


def _binding_hotkey(binding: str) -> str | None:
    """Canonical hotkey text of a GNOME accelerator, or None if unsupported."""
    text = binding.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    modifiers: list[str] = []
    while text.startswith("<"):
        end = text.find(">")
        if end < 0:
            return None
        name = _GNOME_MODIFIERS.get(text[1:end].lower())
        if name is None:
            return None
        modifiers.append(name)
        text = text[end + 1:]
    try:
        return canonical_hotkey("+".join([*modifiers, text])) if text else None
    except ConfigError:
        return None


def expected_emergency_hotkey(configured: str) -> str:
    """The emergency hotkey the GNOME fallback binding should carry.

    Newer input_lock_gnome modules expose ``emergency_binding()``, which
    builds the binding from EMERGENCY_HOTKEY (F12 fallback); prefer it so
    doctor and the installer agree. Otherwise use the configured value.
    """
    try:
        import input_lock_gnome
    except Exception:
        return configured
    builder = getattr(input_lock_gnome, "emergency_binding", None)
    if not callable(builder):
        return configured
    for args in ((), (configured,)):
        try:
            binding = builder(*args)
        except TypeError:
            continue
        except Exception:
            return configured
        if isinstance(binding, str):
            return _binding_hotkey(binding) or configured
    return configured


def protection_checks(
    add: Callable[[str, str, str], None],
    report: Callable[[], dict[str, object]] | None = None,
) -> None:
    """Doctor lines from input_lock_safety.protection_report().

    The report is a read-only capability check (unit contracts, logind
    support, the failure latch). A stale latch (from an earlier boot, or
    followed by a successful suspend) and unit drop-ins are information; an
    active latch blocks lid-ignore until an operator acknowledges it.
    """
    try:
        if report is None:
            from input_lock_safety import protection_report as report
        result = report()
    except Exception as exc:
        add("WARNING", _("Native protection"),
            _("readiness probe unavailable: {error}").format(error=exc))
        return
    for item in result.get("info", []) or []:
        add("INFO", _("Native protection"), daemon_text(item))
    errors = [str(item) for item in result.get("errors", []) or []]
    if not errors:
        add("PASS", _("Native protection"), _("ordinary suspend and the protect/failsafe units are ready"))
    elif any("previous emergency suspend failed" in item for item in errors):
        add("FAIL", _("Protection failure latch"), daemon_text("; ".join(errors) + "; " + RECOVERY_HINT))
    else:
        add("WARNING", _("Native protection"), daemon_text("; ".join(errors)))


def run_doctor() -> int:
    checks: list[tuple[str, str, str]] = []

    def add(level: str, name: str, detail: str) -> None:
        checks.append((level, name, detail))

    os_release = _read_os_release()
    os_name = os_release.get("PRETTY_NAME", platform.platform())
    if os_release.get("ID") == "ubuntu" and os_release.get("VERSION_ID") == "26.04":
        add("PASS", _("Operating system"), os_name)
    else:
        add("WARNING", _("Operating system"),
            _("Designed for Ubuntu 26.04; found {name}").format(name=os_name))

    session_type = os.environ.get("XDG_SESSION_TYPE", "unknown")
    add("PASS" if session_type == "wayland" else "WARNING", _("Session"), session_type)

    if shutil.which("gnome-shell"):
        result = _run(["gnome-shell", "--version"])
        detail = result.stdout.strip() or result.stderr.strip()
        add("PASS" if result.returncode == 0 else "WARNING", _("GNOME Shell"), detail)
    else:
        add("WARNING", _("GNOME Shell"), _("gnome-shell command not found"))

    try:
        config = load_config(DEFAULT_CONFIG_PATH)
        add("PASS", _("Configuration"), str(DEFAULT_CONFIG_PATH))
        add("PASS", _("Emergency hotkey"), config.emergency_hotkey)
    except Exception as exc:
        config = None
        add("FAIL", _("Configuration"), daemon_text(exc))
        if "EMERGENCY_HOTKEY" in str(exc):
            add("FAIL", _("Emergency hotkey"),
                _("invalid or equal to HOTKEY; the emergency unlock shortcut may not work"))

    required_modules = ("evdev", "pyudev", "dbus", "gi")
    missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
    if missing:
        add("FAIL", _("Python dependencies"), _("missing: {modules}").format(modules=", ".join(missing)))
    else:
        add("PASS", _("Python dependencies"), _("all required modules available"))

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3, Gtk
        del AyatanaAppIndicator3, Gtk  # Import capability probe; no UI is created.

        add("PASS", _("Tray dependencies"), _("GTK 3 and Ayatana AppIndicator available"))
    except Exception as exc:
        add("FAIL", _("Tray dependencies"), str(exc))

    service = _run(["systemctl", "is-active", "input-lock.service"])
    service_active = service.returncode == 0 and service.stdout.strip() == "active"
    add("PASS" if service_active else "FAIL", _("System service"), service.stdout.strip() or "inactive")

    daemon_state: dict[str, object] | None = None
    try:
        daemon_state = _state_from(
            _request({"cmd": "status"}, timeout=2.0, check_protocol=False)
        )
        protocol = daemon_state.get("protocol")
        if protocol == PROTOCOL_VERSION:
            add("PASS", _("Daemon protocol"),
                _("version {version}, protocol {protocol}").format(
                    version=daemon_state.get("version", "?"), protocol=protocol))
        else:
            add("WARNING", _("Daemon protocol"),
                _("daemon version {version} reports protocol {protocol}; this CLI uses "
                  "{expected}. Restart {unit}.").format(
                    version=daemon_state.get("version", "?"),
                    protocol=protocol if protocol is not None else _("none"),
                    expected=PROTOCOL_VERSION, unit=SERVICE_NAME))
        if daemon_state.get("incomplete_devices"):
            add("WARNING", _("Lock completeness"), _("one or more devices are not grabbed"))
        else:
            add("PASS", _("Lock completeness"), _("no incomplete lock currently reported"))
        count = int(daemon_state.get("active_devices", 0))
        add("PASS" if count else "FAIL", _("Input detection"),
            ngettext("{count} managed event nodes", "{count} managed event nodes", count).format(count=count))
        lid_count = int(daemon_state.get("lid_switch_devices", 0))
        add(
            "PASS" if lid_count else "WARNING",
            _("Lid switch detection"),
            ngettext("{count} lid switch event node(s)", "{count} lid switch event node(s)",
                     lid_count).format(count=lid_count),
        )
    except Exception as exc:
        add("FAIL", _("Daemon protocol"), daemon_text(exc))

    if shutil.which("notify-send"):
        add("PASS", _("Notifications"), _("notify-send is installed"))
    else:
        add("WARNING", _("Notifications"), _("notify-send is missing; locking still works"))
    add(
        "PASS" if os.environ.get("DBUS_SESSION_BUS_ADDRESS") else "WARNING",
        _("Session D-Bus"),
        _("available") if os.environ.get("DBUS_SESSION_BUS_ADDRESS") else _("not present in this shell"),
    )

    tray_service = _run(["systemctl", "--user", "is-active", "input-lock-notifier.service"])
    tray_active = tray_service.returncode == 0 and tray_service.stdout.strip() == "active"
    add(
        "PASS" if tray_active else "WARNING",
        _("Tray service"),
        tray_service.stdout.strip() or tray_service.stderr.strip() or "inactive",
    )

    shortcut_root = "org.gnome.settings-daemon.plugins.media-keys"
    paths_text = _gsettings_get(shortcut_root, "custom-keybindings")
    primary_path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock/"
    emergency_path = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/input-lock-emergency/"
    if paths_text is None:
        add("WARNING", _("GNOME shortcuts"), _("gsettings unavailable outside the graphical session"))
    else:
        try:
            from input_lock_gnome import parse_paths
            paths = parse_paths(paths_text)
        except Exception:
            paths = []
        missing_paths = [path for path in (primary_path, emergency_path) if path not in paths]
        if missing_paths:
            add("WARNING", _("GNOME shortcuts"), _("one or both fallback bindings are absent"))
        else:
            add("PASS", _("GNOME shortcuts"), _("primary and emergency fallback bindings installed"))
        if config is not None and emergency_path in paths:
            binding = _gsettings_get(
                "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:" + emergency_path,
                "binding",
            )
            expected = expected_emergency_hotkey(config.emergency_hotkey)
            if binding is None or not gnome_binding_matches(binding, expected):
                add("WARNING", _("GNOME emergency binding"),
                    _("{binding} does not match EMERGENCY_HOTKEY={hotkey}; re-run "
                      "'python3 /usr/lib/input-lock/input_lock_gnome.py install' "
                      "(or ./update.sh) while logged in").format(
                        binding=binding or _("unreadable"), hotkey=config.emergency_hotkey))
            else:
                add("PASS", _("GNOME emergency binding"), _("matches {hotkey}").format(hotkey=expected))

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
                add("PASS", _("Emergency device"),
                    ngettext("{count} excluded keyboard node(s)", "{count} excluded keyboard node(s)",
                             len(excluded_keyboards)).format(count=len(excluded_keyboards)))
            else:
                add("WARNING", _("Emergency device"),
                    _("none configured; hotkeys and auto-unlock remain available"))
        except Exception as exc:
            add("WARNING", _("Emergency device"), daemon_text(exc))
        add(
            "PASS" if config.auto_unlock_seconds > 0 else "WARNING",
            _("Default auto-unlock"),
            _("{seconds}s").format(seconds=config.auto_unlock_seconds) if config.auto_unlock_seconds
            else _("disabled (enable for first test)"),
        )
        safety = daemon_state.get("safety", {})
        sample = safety.get("sample") if isinstance(safety, dict) else None
        ready, why = lid_readiness(daemon_state)
        add("PASS" if ready else "WARNING", _("Supervised lid readiness"),
            _("native protection and telemetry available") if ready
            else _("lid-ignore cannot be armed: {reason}").format(reason=daemon_text(why)))
        if daemon_state.get("ignore_lid_close") is True:
            healthy = ready and daemon_state.get("lid_inhibitor_active") is True and daemon_state.get("lid_controller_connected") is True
            add("PASS" if healthy else "FAIL", _("Lid supervision"), str(safety.get("state", "unknown")))
        add("INFO", _("Critical battery policy"),
            _("LockLock does not suppress UPower; platform critical-action acceptance is separate"))

    protection_checks(add)

    # The [PASS]/[WARNING]/[FAIL]/[INFO] level tags stay in English so the
    # output can be grepped and compared in bug reports in any language.
    title = _("Input Lock Doctor")
    print(title)
    print("=" * len(title))
    for level, name, detail in checks:
        print(f"[{level:7}] {name}: {detail}")
    failures = sum(level == "FAIL" for level, _name, _detail in checks)
    warnings = sum(level == "WARNING" for level, _name, _detail in checks)
    print("-" * len(title))
    print(_("Summary: {passed} PASS, {warnings} WARNING, {failures} FAIL").format(
        passed=len(checks) - failures - warnings, warnings=warnings, failures=failures))
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
            print(_("Daemon unlock failed: {error}").format(error=daemon_text(exc)), file=sys.stderr)
            print(_("Recovery: {command}").format(command="sudo systemctl stop input-lock.service"),
                  file=sys.stderr)
            return 2
        print(_("Daemon did not respond ({error}); stopping the service to close all grabs.").format(
            error=daemon_text(exc)), file=sys.stderr)
        result = subprocess.run(
            ["systemctl", "stop", "input-lock.service"], check=False
        )
        if result.returncode == 0:
            print(_("Input Lock service stopped; kernel file descriptors were released."))
            return 0
        return result.returncode or 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="input-lock",
        description=_("Safely lock and unlock local evdev input devices."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help=_("show lock state"))
    status.add_argument("--json", action="store_true", dest="as_json")
    subparsers.add_parser("list-devices", help=_("show classified input devices"))
    action_help = {
        "lock": N_("lock an input class"),
        "unlock": N_("unlock an input class"),
        "toggle": N_("toggle an input class"),
    }
    for action in ("lock", "unlock", "toggle"):
        action_parser = subparsers.add_parser(action, help=_(action_help[action]))
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
            help=_("auto-unlock after 0..86400 seconds (0 disables)"),
        )
    subparsers.add_parser("emergency-unlock", help=_("release every input class"))
    change = subparsers.add_parser("set-hotkey", help=_("change the primary shortcut"))
    change.add_argument("hotkey", help=_("for example {shortcut}").format(shortcut="CTRL+ALT+Z"))
    lid = subparsers.add_parser("lid-ignore", help=_("ignore or restore lid-close events"))
    lid.add_argument("state", choices=("on", "off"))
    subparsers.add_parser("doctor", help=_("run installation and safety checks"))
    logs = subparsers.add_parser("logs", help=_("show recent daemon journal"))
    logs.add_argument("-n", "--lines", type=int, default=200)
    subparsers.add_parser("reload", help=_("validate and reload configuration"))
    # Internal fixed commands used by GNOME, the sleep hook and the notifier
    # service. Without help= argparse leaves them out of the command list, and
    # the explicit metavar keeps them out of the usage line.
    public = [name for name in subparsers.choices]
    subparsers.metavar = "{" + ",".join(public) + "}"
    for name in INTERNAL_COMMANDS:
        subparsers.add_parser(name)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Before the parser: its help texts are translated when it is built.
    # setup() never raises; on any problem the CLI simply stays in English.
    i18n.setup()
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
                raise ProtocolError(_("--timeout must be between 0 and 86400"))
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
                raise ProtocolError(_("the shortcut conflicts with another GNOME binding"))
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
            print(daemon_text(response.get("message", "reload scheduled")))
            return 0
        if args.command in ROOT_ONLY_COMMANDS and os.geteuid() != 0:
            raise ProtocolError(_(
                "{name} is used only by the system sleep hook, which runs as root; "
                "it cannot be run by hand").format(name=args.command))
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
        print(_("input-lock: {error}").format(error=daemon_text(exc)), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
