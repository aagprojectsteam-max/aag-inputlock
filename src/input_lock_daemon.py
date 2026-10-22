#!/usr/bin/python3
"""Privileged, event-driven input lock daemon for Linux evdev.

The daemon opens selected evdev nodes read-only while unlocked.  During a lock
it uses EVIOCGRAB, so the same process remains the sole event recipient and can
recognize the unlock chords.  It never stores, transmits, or logs key content.
"""

from __future__ import annotations

import errno
import fnmatch
import json
import logging
import os
import queue
import secrets
import selectors
import signal
import socket
import stat
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Final

import evdev
import pyudev
from input_lock_safety import (BREADCRUMB, SafetyPolicy, Sample, Sensor, Telemetry, atomic_json, protection_errors, request_service)
from evdev import ecodes
from locklock_core.allowed_keys import ALLOWED_KEY_CATALOG, validate_allowed_keys

from input_lock_common import (
    ConfigError,
    MAX_MESSAGE_BYTES,
    VERSION,
    load_config,
)


LOG = logging.getLogger("input-lock-daemon")
PEERCRED_FORMAT: Final = "3i"
PEERCRED_SIZE: Final = struct.calcsize(PEERCRED_FORMAT)
LOCKABLE_KINDS: Final = frozenset({"keyboard", "mouse", "touchpad", "touchscreen"})
BASE_ALL_KINDS: Final = frozenset({"keyboard", "mouse", "touchpad"})
ALL_INPUT_KINDS: Final = LOCKABLE_KINDS
RELEASE_WAIT_SECONDS: Final = 2.0
RELEASE_POLL_SECONDS: Final = 0.01
USER_SETTINGS_PATH: Final = Path("/var/lib/input-lock/user-settings.json")
MAX_CLIENTS: Final = 64
MAX_SUBSCRIBERS: Final = 8
REQUEST_TIMEOUT: Final = 5.0
CONTROLLER_TIMEOUT: Final = 35.0
RESTART_REQUIRED_CONFIG: Final = ("authorized_uid", "authorized_user", "socket_path", "state_path", "default_mode", "lock_touchscreen")


class TransitionError(RuntimeError):
    """A requested lock transition could not be completed transactionally."""


@dataclass(frozen=True, slots=True)
class HotkeySpec:
    text: str
    groups: tuple[frozenset[int], ...]

    @property
    def all_codes(self) -> frozenset[int]:
        codes: set[int] = set()
        for group in self.groups:
            codes.update(group)
        return frozenset(codes)

    def matches(self, pressed: set[int]) -> bool:
        return all(bool(group & pressed) for group in self.groups)

    @property
    def signature(self) -> frozenset[frozenset[int]]:
        return frozenset(self.groups)


@dataclass(slots=True)
class DeviceRecord:
    path: str
    device: evdev.InputDevice
    name: str
    kinds: frozenset[str]
    vendor_product: str
    by_id: tuple[str, ...]
    excluded: bool
    exclusion_reason: str
    pressed: set[int] = field(default_factory=set)
    grabbed: bool = False
    forwarded_allowed: set[int] = field(default_factory=set)
    synchronizing: bool = False

    @property
    def lockable(self) -> bool:
        return bool(self.kinds & LOCKABLE_KINDS)

    def wants_grab(self, locked_kinds: set[str]) -> bool:
        return not self.excluded and bool(self.kinds & locked_kinds)

    def wants_managed_grab(
        self, locked_kinds: set[str], ignore_lid_close: bool
    ) -> bool:
        return self.wants_grab(locked_kinds) or (
            not self.excluded and ignore_lid_close and "lid-switch" in self.kinds
        )


@dataclass(slots=True)
class ClientState:
    sock: socket.socket
    uid: int
    buffer: bytearray = field(default_factory=bytearray)
    subscribed: bool = False
    deadline: float = field(default_factory=lambda: time.monotonic() + REQUEST_TIMEOUT)


def session_is_eligible(properties: dict[str, object]) -> bool:
    """Only an active local graphical user on a real seat may control locking."""
    seat = properties.get("Seat", ())
    return (
        bool(properties.get("Active")) and not bool(properties.get("Remote", True))
        and not bool(properties.get("LockedHint", False))
        and properties.get("Class") == "user"
        and properties.get("Type") in {"wayland", "x11"}
        and isinstance(seat, (tuple, list)) and len(seat) == 2 and bool(seat[0])
    )


@dataclass(slots=True)
class PendingHotkey:
    action: str
    mode: str
    release_codes: frozenset[int]
    source: str
    created_at: float


def _key_code(name: str) -> int:
    value = ecodes.ecodes.get(name)
    if not isinstance(value, int):
        raise ConfigError(f"unsupported key name: {name}")
    return value


def parse_hotkey(text: str) -> HotkeySpec:
    aliases: dict[str, frozenset[int]] = {
        "CTRL": frozenset({_key_code("KEY_LEFTCTRL"), _key_code("KEY_RIGHTCTRL")}),
        "CONTROL": frozenset({_key_code("KEY_LEFTCTRL"), _key_code("KEY_RIGHTCTRL")}),
        "ALT": frozenset({_key_code("KEY_LEFTALT"), _key_code("KEY_RIGHTALT")}),
        "SHIFT": frozenset({_key_code("KEY_LEFTSHIFT"), _key_code("KEY_RIGHTSHIFT")}),
        "SUPER": frozenset({_key_code("KEY_LEFTMETA"), _key_code("KEY_RIGHTMETA")}),
        "META": frozenset({_key_code("KEY_LEFTMETA"), _key_code("KEY_RIGHTMETA")}),
    }
    tokens = [token.strip().upper() for token in text.split("+") if token.strip()]
    if len(tokens) < 2 or len(tokens) != len(set(tokens)):
        raise ConfigError(f"invalid hotkey: {text}")
    groups: list[frozenset[int]] = []
    for token in tokens:
        if token in aliases:
            groups.append(aliases[token])
            continue
        key_name = token if token.startswith("KEY_") else f"KEY_{token}"
        groups.append(frozenset({_key_code(key_name)}))
    return HotkeySpec(text="+".join(tokens), groups=tuple(groups))


class LogindMonitor(threading.Thread):
    """Listen for suspend and authorized-session deactivation via system D-Bus."""

    def __init__(self, authorized_uid: int, callback: Callable[[str], None]) -> None:
        super().__init__(name="input-lock-logind", daemon=True)
        self.authorized_uid = authorized_uid
        self.callback = callback
        self._loop: object | None = None
        self._authorized_paths: set[str] = set()
        self._authorized_active_paths: set[str] = set()

    def run(self) -> None:
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib

            loop_adapter = DBusGMainLoop(set_as_default=False)
            bus = dbus.SystemBus(mainloop=loop_adapter)
            manager_object = bus.get_object(
                "org.freedesktop.login1", "/org/freedesktop/login1", follow_name_owner_changes=True
            )
            manager = dbus.Interface(manager_object, "org.freedesktop.login1.Manager")
            upower_object = bus.get_object(
                "org.freedesktop.UPower",
                "/org/freedesktop/UPower",
                follow_name_owner_changes=True,
            )
            upower_properties = dbus.Interface(
                upower_object, "org.freedesktop.DBus.Properties"
            )
            battery_object = bus.get_object(
                "org.freedesktop.UPower",
                "/org/freedesktop/UPower/devices/DisplayDevice",
                follow_name_owner_changes=True,
            )
            battery_properties = dbus.Interface(
                battery_object, "org.freedesktop.DBus.Properties"
            )

            session_error_reported = False
            power_error_reported = False

            def refresh_sessions(*_args: object) -> None:
                nonlocal session_error_reported
                paths: set[str] = set()
                active_paths: set[str] = set()
                try:
                    for row in manager.ListSessions(timeout=3):
                        if int(row[1]) == self.authorized_uid:
                            path = str(row[4])
                            paths.add(path)
                            session_object = bus.get_object(
                                "org.freedesktop.login1", path
                            )
                            properties = dbus.Interface(
                                session_object, "org.freedesktop.DBus.Properties"
                            )
                            values = dict(properties.GetAll("org.freedesktop.login1.Session", timeout=3))
                            if session_is_eligible(values):
                                active_paths.add(path)
                except Exception as exc:  # D-Bus failures must remove eligibility.
                    if not session_error_reported:
                        LOG.warning("event=LOGIND_UNAVAILABLE error=%s", exc)
                    session_error_reported = True
                    active_paths.clear()
                if active_paths:
                    if session_error_reported:
                        LOG.info("event=LOGIND_RESTORED")
                    session_error_reported = False
                    self.callback("logind-healthy")
                self._authorized_paths = paths
                self._authorized_active_paths = active_paths
                self.callback("session-active" if active_paths else "session-inactive")

            def prepare_for_sleep(starting: object) -> None:
                if bool(starting):
                    self.callback("prepare-sleep")

            def refresh_power(*_args: object) -> None:
                nonlocal power_error_reported
                try:
                    values = dict(upower_properties.GetAll("org.freedesktop.UPower", timeout=3))
                    battery = dict(battery_properties.GetAll("org.freedesktop.UPower.Device", timeout=3))
                    action = str(dbus.Interface(upower_object, "org.freedesktop.UPower").GetCriticalAction(timeout=3))
                    if power_error_reported:
                        LOG.info("event=UPOWER_RESTORED")
                    power_error_reported = False
                    self.callback("power-state:" + json.dumps({
                        "on_battery": bool(values["OnBattery"]),
                        "lid_closed": bool(values["LidIsClosed"]),
                        "percentage": float(battery["Percentage"]),
                        "warning_level": int(battery["WarningLevel"]),
                        "critical_action": action,
                    }))
                except Exception as exc:
                    self.callback("power-unavailable")
                    if not power_error_reported:
                        LOG.warning("event=UPOWER_UNAVAILABLE error=%s", exc)
                    power_error_reported = True

            def properties_changed(
                interface: object,
                changed: object,
                *signal_args: object,
                path: object | None = None,
                **_signal_kwargs: object,
            ) -> None:
                # dbus-python has emitted both two- and three-positional-argument
                # variants of PropertiesChanged in supported Ubuntu releases.
                # The invalidated-properties array is irrelevant here, so accept
                # either shape instead of allowing a callback TypeError to stop
                # session monitoring.
                del signal_args
                interface_text = str(interface)
                if interface_text in {
                    "org.freedesktop.UPower",
                    "org.freedesktop.UPower.Device",
                }:
                    refresh_power()
                    return
                if interface_text != "org.freedesktop.login1.Session":
                    return
                path_text = str(path or "")
                if path_text in self._authorized_paths:
                    refresh_sessions()

            refresh_sessions()
            refresh_power()
            # Revalidate complete session properties even after invalidated
            # signals or a logind owner change. Failure removes eligibility.
            GLib.timeout_add_seconds(5, lambda: (refresh_sessions(), refresh_power(), True)[2])
            bus.add_signal_receiver(
                prepare_for_sleep,
                signal_name="PrepareForSleep",
                dbus_interface="org.freedesktop.login1.Manager",
            )
            bus.add_signal_receiver(
                properties_changed,
                signal_name="PropertiesChanged",
                dbus_interface="org.freedesktop.DBus.Properties",
                path_keyword="path",
            )
            bus.add_signal_receiver(
                refresh_sessions,
                signal_name="SessionNew",
                dbus_interface="org.freedesktop.login1.Manager",
            )
            bus.add_signal_receiver(
                refresh_sessions,
                signal_name="SessionRemoved",
                dbus_interface="org.freedesktop.login1.Manager",
            )
            self._loop = GLib.MainLoop()
            self._loop.run()
        except Exception as exc:
            self.callback("session-inactive")
            LOG.warning("logind monitor unavailable; sleep hook remains active: %s", exc)


class InputLockDaemon:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.emergency_hotkey = parse_hotkey(self.config.emergency_hotkey)
        configured_primary = parse_hotkey(self.config.hotkey)
        self.primary_hotkey = self._load_primary_hotkey_override(
            configured_primary, self.emergency_hotkey
        )
        if self.primary_hotkey.signature == self.emergency_hotkey.signature:
            raise ConfigError("primary and emergency hotkeys must be different")
        # Retain user preference separately from effective state. Never
        # reactivate lid suppression after a service/computer restart.
        self._saved_lid_ignore_close = self._load_lid_ignore_setting()
        self.ignore_lid_close = False
        self.controller_fd: int | None = None
        self.lid_generation = secrets.randbits(48)
        self.lid_lease_deadline: float | None = None
        self.release_errors: list[str] = []
        self.lid_inhibitor_fd: int | None = None
        (
            self.lid_power_policy_enabled,
            self.lid_battery_threshold,
        ) = self._load_lid_power_policy_setting()
        # Legacy values are read for migration only. No automatic-sleep or
        # critical-battery overrides are created by this version.
        self.lid_policy_healthy = False
        self.sleep_inhibitor_fd: int | None = None
        self.on_battery = False
        self.lid_is_closed = False
        self.lid_state_from_evdev = False
        self.last_lid_event_at = 0.0
        self.battery_percentage: float | None = None
        configured_kinds = self._mode_kinds(self._default_hotkey_mode())
        self.default_locked_kinds = self._load_lock_kinds_setting(configured_kinds)
        self.idle_lock_enabled, self.idle_lock_seconds = self._load_idle_lock_setting()
        self.allowed_keys = self._load_allowed_keys_setting()
        self.allowed_key_codes = self._allowed_key_codes(self.allowed_keys)
        self.allowed_uinput: evdev.UInput | None = None
        self.allowed_key_down_counts: dict[int, int] = {}
        self.last_input_activity = time.monotonic()
        self.selector = selectors.DefaultSelector()
        self.udev_context = pyudev.Context()
        self.udev_monitor: pyudev.Monitor | None = None
        self.server: socket.socket | None = None
        self.devices: dict[str, DeviceRecord] = {}
        self.clients: dict[int, ClientState] = {}
        self.subscribers: set[int] = set()
        self.locked_kinds: set[str] = set()
        self.auto_unlock_deadline: float | None = None
        self.pending_hotkey: PendingHotkey | None = None
        self.primary_down = False
        self.emergency_down = False
        self.vt_down = False
        self.last_hotkey_at = 0.0
        self.last_action = "startup"
        self.authorized_session_active = False
        self.running = True
        self.reload_requested = False
        self.control_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.wakeup_read, self.wakeup_write = socket.socketpair()
        self.wakeup_read.setblocking(False)
        self.wakeup_write.setblocking(False)
        self.logind_monitor: LogindMonitor | None = None
        self.accept_retry_at: float | None = None
        self.safety = SafetyPolicy()
        self.safety_sample: Sample | None = None
        self.safety_stop = threading.Event()
        self.last_power_at = self.last_logind_at = 0.0
        self.upower_warning: int | None = None
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.daemon_started_at = time.time()
        self.safety_last_write = 0.0
        self.safety_signature = None
        self.safety_receipt_error = False
        self.safety_previous_run = None
        self.protective_result = None
        self.clean_shutdown = False
        self.next_safety_tick = time.monotonic()
        self.last_safety_emit = 0.0


    # ---------------------------- lifecycle ----------------------------

    def run(self) -> int:
        self._install_signal_handlers()
        self._reset_lid_ignore_on_startup()
        self.selector.register(self.wakeup_read, selectors.EVENT_READ, ("wakeup", None))
        self._setup_server()
        self._setup_udev()
        self._rescan_devices()
        if self.allowed_key_codes:
            try:
                self._ensure_allowed_uinput()
            except TransitionError as exc:
                LOG.error("Saved allowed keys are unavailable: %s", exc)
        if self.ignore_lid_close:
            try:
                self._acquire_lid_inhibitor()
            except TransitionError as exc:
                LOG.error("Saved lid-ignore setting could not be activated: %s", exc)
                self.ignore_lid_close = False
        self._write_state()
        self.logind_monitor = LogindMonitor(
            self.config.authorized_uid, self._post_control
        )
        self.logind_monitor.start()
        threading.Thread(target=self._sample_safety, name="input-lock-telemetry", daemon=True).start()
        LOG.info(
            "Input Lock %s started unlocked; authorized UID=%d; devices=%d",
            VERSION,
            self.config.authorized_uid,
            len(self.devices),
        )

        try:
            while self.running:
                timeout = self._next_timeout()
                for key, _mask in self.selector.select(timeout):
                    category, identifier = key.data
                    if category == "server":
                        self._accept_clients()
                    elif category == "client":
                        self._read_client(int(identifier))
                    elif category == "device":
                        self._read_device(str(identifier))
                    elif category == "udev":
                        self._handle_udev()
                    elif category == "wakeup":
                        self._drain_wakeup()
                self._handle_timers()
                if self.reload_requested:
                    self._reload_config()
        except Exception:
            LOG.exception("Fatal daemon error; releasing every grabbed device")
            return 1
        finally:
            self.clean_shutdown = not __import__("sys").exc_info()[0] and not self.running
            self._cleanup()
        return 0

    def _reset_lid_ignore_on_startup(self) -> None:
        """Preserve preference, never restore the effective override automatically."""
        try:
            old = json.loads(BREADCRUMB.read_text())
            if not isinstance(old, dict):
                raise ValueError("invalid safety journal object")
            self.safety_previous_run = {key: old.get(key) for key in
                ("boot_id", "daemon_started_at", "updated_at", "clean_shutdown", "effective", "safety", "protective_result")}
            if not old.get("clean_shutdown", False):
                self.safety.transition("RECOVERY", "UNCLEAN_PREVIOUS_RUN_DETECTED")
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.safety.transition("RECOVERY", "UNREADABLE_PREVIOUS_RUN")
        self._write_safety(force=True)

    def _write_safety(self, *, force: bool = False) -> None:
        snapshot = self.safety.snapshot()
        measured = snapshot["thermal_measurement"]
        measurement_signature = (measured["state"], measured["fresh"], measured["complete"], self.safety.armed)
        if measurement_signature != getattr(self, "thermal_measurement_signature", None):
            LOG.info("event=THERMAL_MEASUREMENT_CHANGED condition=%s protection=%s fresh=%s complete=%s",
                     measured["state"], snapshot["thermal_protection"], measured["fresh"], measured["complete"])
            self.thermal_measurement_signature = measurement_signature
        signature = (self.safety.state, self.safety.reason, self.safety.thermal_level, measurement_signature,
                     self.ignore_lid_close, self._saved_lid_ignore_close, str(self.protective_result))
        now = time.monotonic()
        if not force and signature == self.safety_signature and now - self.safety_last_write < 30:
            return
        atomic_json(BREADCRUMB, {
            "schema": 1, "version": VERSION, "boot_id": self.boot_id,
            "invocation_id": os.environ.get("INVOCATION_ID"),
            "daemon_started_at": self.daemon_started_at, "updated_at": time.time(),
            "requested": self._saved_lid_ignore_close, "effective": self.ignore_lid_close,
            "generation": self.lid_generation, "clean_shutdown": self.clean_shutdown,
            "restrictions_released": (self.lid_inhibitor_fd is None and self.sleep_inhibitor_fd is None
                                      and not any(r.grabbed for r in self.devices.values())),
            "safety": snapshot, "protective_result": self.protective_result,
            "previous_run": self.safety_previous_run,
            "events": self.safety.events[-24:],
        })
        self.safety.events = self.safety.events[-24:]
        self.safety_signature, self.safety_last_write = signature, now

    def _sample_safety(self) -> None:
        telemetry = Telemetry()
        errors, probe_at = ["native protection probe pending"], 0.0
        telemetry_failed = False
        while not self.safety_stop.is_set():
            now = time.monotonic()
            if now - probe_at >= 30:
                errors, probe_at = protection_errors(), now
            try:
                sample = telemetry.read(stack_ready=not errors)
                sample.protection_errors = errors
                if telemetry_failed:
                    LOG.info("event=TELEMETRY_RESTORED source=kernel")
                telemetry_failed = False
                self._post_control("safety-sample:" + json.dumps(__import__("dataclasses").asdict(sample)))
            except Exception:
                if not telemetry_failed:
                    LOG.exception("event=TELEMETRY_LOST source=kernel")
                telemetry_failed = True
            self.safety_stop.wait(2)

    def _safety_tick(self) -> None:
        now = time.monotonic()
        self.next_safety_tick = now + 1
        if self.protective_result is not None:
            try:
                receipt = json.loads(BREADCRUMB.with_name("safety-action-result.json").read_text())
                if not isinstance(receipt, dict):
                    raise ValueError("invalid protective receipt")
                self.safety_receipt_error = False
                if receipt.get("boot_id") == self.boot_id and receipt.get("generation") == self.protective_result.get("generation") and receipt.get("result") != self.protective_result.get("result"):
                    self.protective_result.update(receipt)
                    if receipt["result"] == "suspend-transition-confirmed" and receipt.get("transition_confirmed") is True and not self.ignore_lid_close:
                        self.safety.transition("PROTECTIVE_ACTION_EXECUTED", "PROTECTIVE_ACTION_SUCCEEDED")
                    elif receipt["result"] == "protection-failed" and not self.ignore_lid_close:
                        self.safety.transition("PROTECTIVE_ACTION_FAILED", "OPEN_LID_OPERATOR_RECOVERY_REQUIRED")
                    elif receipt["result"].startswith("cancelled") and not self.ignore_lid_close:
                        self.safety.release()
                    self._write_safety(force=True)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError):
                if not self.safety_receipt_error:
                    LOG.warning("event=PROTECTIVE_RESULT_UNAVAILABLE")
                self.safety_receipt_error = True
        sample = self.safety_sample
        if sample is not None:
            sample.dependencies_ok = now - self.last_power_at <= 12 and now - self.last_logind_at <= 12
            sample.warning_level = self.upower_warning
            if now - sample.at > 12:
                sample.lid_closed = None
            reason = self.safety.update(sample)
            if reason:
                self._start_protection(reason)
        elif self.ignore_lid_close:
            self._start_protection("TELEMETRY_LOST")
        self._write_safety()
        self._write_state()
        if now - self.last_safety_emit >= 5:
            self.last_safety_emit = now
            self._emit_state_event("safety-status")
        # A wedged main event loop loses its watchdog. InputLock recovery stops
        # the daemon before one final ordinary suspend attempt.
        address = os.environ.get("NOTIFY_SOCKET")
        if address:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notify:
                notify.sendto(b"WATCHDOG=1", "\0" + address[1:] if address.startswith("@") else address)

    def _lid_may_be_closed(self) -> bool:
        return self.lid_is_closed or self.safety_sample is None or self.safety_sample.lid_closed is not False

    def _start_protection(self, reason: str) -> None:
        # SafetyPolicy.update() already made the decision and recorded the
        # original evidence. Do not overwrite it or log a second decision.
        if self.safety.armed or self.safety.state != "PROTECTIVE_ACTION_PENDING" or self.safety.reason != reason:
            self.safety.decide(reason)
        self._emergency_unlock("safety-protection", preserve_safety=True)
        if any(r.grabbed for r in self.devices.values()):
            raise TransitionError("device release failed; terminating for InputLock recovery")
        self.protective_result = {"reason": reason, "attempted_at": time.time(),
                                  "generation": self.lid_generation, "result": "pending",
                                  "decision_evidence": self.safety.last_decision}
        generation = self.lid_generation
        self._write_safety(force=True)
        def dispatch() -> None:
            try:
                job = request_service()
                result = {"result": "delegated-to-inputlock", "job": job}
            except Exception as exc:
                result = {"result": "request-failed", "error": str(exc)}
            self._post_control("protective-result:" + json.dumps({**result, "generation": generation}))
        threading.Thread(target=dispatch, name="input-lock-protect-request", daemon=True).start()

    def _install_signal_handlers(self) -> None:
        def stop_handler(signum: int, _frame: object) -> None:
            LOG.info("Received signal %d; safe shutdown requested", signum)
            self.running = False
            self._wake()

        def reload_handler(_signum: int, _frame: object) -> None:
            self.reload_requested = True
            self._wake()

        signal.signal(signal.SIGTERM, stop_handler)
        signal.signal(signal.SIGINT, stop_handler)
        signal.signal(signal.SIGHUP, reload_handler)

    def _setup_server(self) -> None:
        path = self.config.socket_path
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(f"cannot remove stale socket {path}: {exc}") from exc
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.setblocking(False)
        server.bind(str(path))
        os.chmod(path, 0o666)
        server.listen(16)
        self.server = server
        self.selector.register(server, selectors.EVENT_READ, ("server", None))

    def _setup_udev(self) -> None:
        monitor = pyudev.Monitor.from_netlink(self.udev_context)
        monitor.filter_by(subsystem="input")
        monitor.start()
        self.udev_monitor = monitor
        self.selector.register(monitor.fileno(), selectors.EVENT_READ, ("udev", None))

    def _cleanup(self) -> None:
        try:
            preserve = (not self.clean_shutdown and
                        (self.ignore_lid_close or self.safety.state == "PROTECTIVE_ACTION_PENDING"))
            if preserve:
                self.safety.decide("DAEMON_FAILED")
            self._emergency_unlock("daemon-shutdown", preserve_safety=preserve)
            self.last_action = "shutdown-unlocked"
            self._write_state()
            LOG.info("Daemon shutting down; all input devices released")
        except Exception:
            LOG.exception("Cleanup encountered an error; closing descriptors now")

        for fd in list(self.clients):
            self._close_client(fd)
        for path in list(self.devices):
            self._detach_device(path)
        for selectable in (self.server, self.wakeup_read):
            if selectable is not None:
                try:
                    self.selector.unregister(selectable)
                except Exception:
                    pass
                try:
                    selectable.close()
                except OSError:
                    pass
        self.wakeup_write.close()
        self.selector.close()
        self._release_lid_inhibitor()
        self._release_sleep_inhibitor()
        self._close_allowed_uinput()
        self.safety_stop.set()
        self._write_safety(force=True)
        try:
            self.config.socket_path.unlink(missing_ok=True)
        except OSError:
            pass

    # -------------------------- control events -------------------------

    def _post_control(self, event: str) -> None:
        self.control_queue.put(event)
        self._wake()

    def _wake(self) -> None:
        try:
            self.wakeup_write.send(b"x")
        except (BlockingIOError, OSError):
            pass

    def _drain_wakeup(self) -> None:
        try:
            while self.wakeup_read.recv(256):
                pass
        except (BlockingIOError, OSError):
            pass
        while True:
            try:
                event = self.control_queue.get_nowait()
            except queue.Empty:
                break
            if event == "prepare-sleep" and self.config.unlock_on_suspend:
                self._emergency_unlock("system-suspend")
            elif event == "session-active":
                if not self.authorized_session_active:
                    self.last_input_activity = time.monotonic()
                self.authorized_session_active = True
                self._write_state()
            elif event == "session-inactive":
                was_active = self.authorized_session_active
                self.authorized_session_active = False
                if self.ignore_lid_close and self._lid_may_be_closed():
                    self._start_protection("SUPERVISION_LOST")
                elif self.ignore_lid_close:
                    self._disable_lid("session-inactive")
                if self.config.unlock_on_session_switch and was_active:
                    self._emergency_unlock("authorized-session-inactive", preserve_safety=self.safety.state == "PROTECTIVE_ACTION_PENDING")
            elif event == "logind-healthy":
                self.last_logind_at = time.monotonic()
            elif event == "power-unavailable":
                self.last_power_at = 0
            elif event.startswith("safety-sample:"):
                payload = json.loads(event.partition(":")[2])
                payload["sensors"] = [Sensor(**row) for row in payload["sensors"]]
                self.safety_sample = Sample(**payload)
                if self.safety_sample.at < self.last_lid_event_at:
                    self.safety_sample.lid_closed = self.lid_is_closed
            elif event.startswith("protective-result:"):
                result = json.loads(event.partition(":")[2])
                if (self.protective_result is None or self.ignore_lid_close or
                        result.get("generation") != self.protective_result.get("generation")):
                    continue
                if self.protective_result.get("result") in {"pending", "delegated-to-inputlock"}:
                    self.protective_result.update(result)
                self.safety.event("PROTECTIVE_ACTION_FAILED" if result["result"] == "request-failed" else "PROTECTIVE_ACTION_STARTED")
                self._write_safety(force=True)
                if result["result"] == "request-failed" and self.safety.state == "PROTECTIVE_ACTION_PENDING":
                    # Exit failure so systemd invokes InputLock recovery,
                    # independently of the failed D-Bus request.
                    raise TransitionError("protective service dispatch failed")
            elif event.startswith("power-state:"):
                try:
                    payload = json.loads(event.partition(":")[2])
                    self.on_battery = payload["on_battery"]
                    if not self.lid_state_from_evdev:
                        self.lid_is_closed = payload["lid_closed"]
                    self.battery_percentage = payload["percentage"]
                    self.upower_warning = payload["warning_level"]
                    self.last_power_at = time.monotonic() if payload["critical_action"] in {"PowerOff", "Hibernate", "HybridSleep", "Suspend"} else 0
                except (KeyError, ValueError, TypeError):
                    self.last_power_at = 0
                self._write_state()

    def _reload_config(self) -> None:
        self.reload_requested = False
        try:
            new_config = load_config(self.config_path)
            changed = [name for name in RESTART_REQUIRED_CONFIG if getattr(new_config, name) != getattr(self.config, name)]
            if changed:
                raise ConfigError("restart required for: " + ", ".join(changed))
            new_emergency = parse_hotkey(new_config.emergency_hotkey)
            configured_primary = parse_hotkey(new_config.hotkey)
            new_primary = self._load_primary_hotkey_override(
                configured_primary, new_emergency
            )
            if new_primary.signature == new_emergency.signature:
                raise ConfigError("primary and emergency hotkeys must be different")
        except ConfigError as exc:
            self._emergency_unlock("invalid-config-reload")
            LOG.error("Configuration reload rejected; input remains unlocked: %s", exc)
            return

        self._emergency_unlock("configuration-reload")
        self.config = new_config
        self.primary_hotkey = new_primary
        self.emergency_hotkey = new_emergency
        self._rescan_devices(reclassify=True)
        self._write_state()
        LOG.info("Configuration reloaded safely; state is unlocked")

    @staticmethod
    def _load_primary_hotkey_override(
        fallback: HotkeySpec, emergency: HotkeySpec
    ) -> HotkeySpec:
        try:
            payload = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("settings root must be an object")
            value = payload.get("primary_hotkey")
            if not isinstance(value, str):
                raise ValueError("primary_hotkey must be a string")
            candidate = parse_hotkey(value)
            if candidate.signature == emergency.signature:
                raise ValueError("primary hotkey matches the emergency hotkey")
            return candidate
        except FileNotFoundError:
            return fallback
        except (OSError, ValueError, json.JSONDecodeError, ConfigError) as exc:
            LOG.warning(
                "Ignoring invalid user hotkey settings at %s: %s",
                USER_SETTINGS_PATH,
                exc,
            )
            return fallback

    @staticmethod
    def _load_lid_ignore_setting() -> bool:
        try:
            payload = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("settings root must be an object")
            value = payload.get("ignore_lid_close", False)
            if not isinstance(value, bool):
                raise ValueError("ignore_lid_close must be a boolean")
            return value
        except FileNotFoundError:
            return False
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOG.warning(
                "Ignoring invalid lid setting at %s: %s", USER_SETTINGS_PATH, exc
            )
            return False

    @staticmethod
    def _load_lock_kinds_setting(fallback: set[str]) -> set[str]:
        try:
            payload = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("settings root must be an object")
            value = payload.get("lock_kinds")
            if value is None:
                return set(fallback)
            return InputLockDaemon._validated_lock_kinds(value)
        except FileNotFoundError:
            return set(fallback)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOG.warning("Ignoring invalid lock-kind settings: %s", exc)
            return set(fallback)

    @staticmethod
    def _load_idle_lock_setting() -> tuple[bool, int]:
        try:
            payload = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("settings root must be an object")
            enabled = payload.get("idle_lock_enabled", False)
            seconds = payload.get("idle_lock_seconds", 600)
            if not isinstance(enabled, bool):
                raise ValueError("idle_lock_enabled must be a boolean")
            if isinstance(seconds, bool) or not isinstance(seconds, int):
                raise ValueError("idle_lock_seconds must be an integer")
            if not 60 <= seconds <= 86_400:
                raise ValueError("idle_lock_seconds must be between 60 and 86400")
            return enabled, seconds
        except FileNotFoundError:
            return False, 600
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOG.warning("Ignoring invalid idle-lock settings: %s", exc)
            return False, 600

    @staticmethod
    def _load_lid_power_policy_setting() -> tuple[bool, int]:
        try:
            payload = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("settings root must be an object")
            enabled = payload.get("lid_power_policy_enabled", False)
            threshold = payload.get("lid_battery_threshold", 20)
            if not isinstance(enabled, bool):
                raise ValueError("lid_power_policy_enabled must be a boolean")
            if isinstance(threshold, bool) or not isinstance(threshold, int):
                raise ValueError("lid_battery_threshold must be an integer")
            if not 1 <= threshold <= 99:
                raise ValueError("lid_battery_threshold must be between 1 and 99")
            return enabled, threshold
        except FileNotFoundError:
            return False, 20
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOG.warning("Ignoring invalid lid power-policy settings: %s", exc)
            return False, 20

    @staticmethod
    def _load_allowed_keys_setting() -> tuple[str, ...]:
        try:
            payload = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("settings root must be an object")
            return validate_allowed_keys(payload.get("allowed_keys", []))
        except FileNotFoundError:
            return ()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOG.warning("Ignoring invalid allowed-key settings: %s", exc)
            return ()

    @staticmethod
    def _allowed_key_codes(names: tuple[str, ...]) -> set[int]:
        codes: set[int] = set()
        for name in names:
            evdev_name = ALLOWED_KEY_CATALOG[name][1]
            value = ecodes.ecodes.get(evdev_name)
            if not isinstance(value, int):
                raise ConfigError(f"this Linux kernel does not support {evdev_name}")
            codes.add(value)
        return codes

    def _persist_user_settings(
        self,
        *,
        primary_hotkey: HotkeySpec | None = None,
        ignore_lid_close: bool | None = None,
        lock_kinds: set[str] | None = None,
        idle_lock_enabled: bool | None = None,
        idle_lock_seconds: int | None = None,
        lid_power_policy_enabled: bool | None = None,
        lid_battery_threshold: int | None = None,
        allowed_keys: tuple[str, ...] | None = None,
    ) -> None:
        saved_hotkey = primary_hotkey or self.primary_hotkey
        saved_lid_setting = (
            self._saved_lid_ignore_close if ignore_lid_close is None else ignore_lid_close
        )
        USER_SETTINGS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "version": 1,
                "primary_hotkey": saved_hotkey.text,
                "ignore_lid_close": saved_lid_setting,
                "lock_kinds": sorted(
                    self.default_locked_kinds if lock_kinds is None else lock_kinds
                ),
                "idle_lock_enabled": (
                    self.idle_lock_enabled
                    if idle_lock_enabled is None
                    else idle_lock_enabled
                ),
                "idle_lock_seconds": (
                    self.idle_lock_seconds
                    if idle_lock_seconds is None
                    else idle_lock_seconds
                ),
                "lid_power_policy_enabled": (
                    self.lid_power_policy_enabled
                    if lid_power_policy_enabled is None
                    else lid_power_policy_enabled
                ),
                "lid_battery_threshold": (
                    self.lid_battery_threshold
                    if lid_battery_threshold is None
                    else lid_battery_threshold
                ),
                "allowed_keys": list(
                    self.allowed_keys if allowed_keys is None else allowed_keys
                ),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        temporary_name: str | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=".user-settings-",
                dir=str(USER_SETTINGS_PATH.parent),
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, USER_SETTINGS_PATH)
            directory_fd = os.open(USER_SETTINGS_PATH.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
            raise TransitionError(f"cannot save user settings: {exc}") from exc

    def _set_primary_hotkey(self, value: str) -> None:
        candidate = parse_hotkey(value)
        if candidate.signature == self.emergency_hotkey.signature:
            raise TransitionError("primary hotkey must differ from the emergency hotkey")
        if candidate.all_codes & self.allowed_key_codes:
            raise TransitionError("the shortcut key cannot also be an allowed key")
        self._emergency_unlock("hotkey-change")
        self._persist_user_settings(primary_hotkey=candidate)
        self.primary_hotkey = candidate
        self.primary_down = False
        self.pending_hotkey = None
        self.last_action = "hotkey-changed"
        self._write_state()
        self._emit_state_event("hotkey-changed")
        LOG.info("Primary hotkey changed to %s", candidate.text)

    def _set_default_lock_kinds(self, kinds: set[str]) -> None:
        if self.locked_kinds:
            self._emergency_unlock("lock-kinds-change")
        self._persist_user_settings(lock_kinds=kinds)
        self.default_locked_kinds = set(kinds)
        self.last_action = "lock-kinds-changed"
        self._write_state()
        self._emit_state_event("lock-kinds-changed")
        LOG.info("Default lock kinds changed to %s", sorted(kinds))

    def _set_idle_lock(self, enabled: bool, seconds: int) -> None:
        self._persist_user_settings(
            idle_lock_enabled=enabled, idle_lock_seconds=seconds
        )
        self.idle_lock_enabled = enabled
        self.idle_lock_seconds = seconds
        self.last_input_activity = time.monotonic()
        self.last_action = "idle-lock-settings-changed"
        self._write_state()
        self._emit_state_event("idle-lock-settings-changed")
        LOG.info("Idle auto-lock changed: enabled=%s seconds=%d", enabled, seconds)

    def _set_lid_power_policy(self, enabled: bool, threshold: int) -> None:
        raise TransitionError("Linux does not override idle sleep or battery safety; legacy power settings are unsupported")


    def _set_allowed_keys(self, keys: tuple[str, ...]) -> None:
        if "COPILOT" in keys and "F23" in keys:
            raise TransitionError("Copilot and F23 represent the same physical key")
        codes = self._allowed_key_codes(keys)
        reserved = set(
            self.primary_hotkey.all_codes | self.emergency_hotkey.all_codes
        )
        reserved.update(_key_code(f"KEY_F{number}") for number in range(1, 13))
        if codes & reserved:
            raise TransitionError(
                "recovery shortcut keys (including F1-F12) cannot be added"
            )
        if self.locked_kinds:
            self._emergency_unlock("allowed-keys-change")
        previous_keys = self.allowed_keys
        previous_codes = self.allowed_key_codes
        self._close_allowed_uinput()
        self.allowed_keys = keys
        self.allowed_key_codes = codes
        try:
            if codes:
                self._ensure_allowed_uinput()
            self._persist_user_settings(allowed_keys=keys)
        except Exception:
            self._close_allowed_uinput()
            self.allowed_keys = previous_keys
            self.allowed_key_codes = previous_codes
            if previous_codes:
                self._ensure_allowed_uinput()
            raise
        self.last_action = "allowed-keys-changed"
        self._write_state()
        self._emit_state_event(self.last_action)

    def _ensure_allowed_uinput(self) -> None:
        if not self.allowed_key_codes or self.allowed_uinput is not None:
            return
        try:
            capabilities = set(self.allowed_key_codes)
            if "COPILOT" in self.allowed_keys:
                capabilities.update(
                    {
                        _key_code("KEY_LEFTMETA"),
                        _key_code("KEY_LEFTSHIFT"),
                        _key_code("KEY_F23"),
                    }
                )
            self.allowed_uinput = evdev.UInput(
                {ecodes.EV_KEY: sorted(capabilities)},
                name="Input Lock Virtual Allowed Keys",
            )
        except (OSError, PermissionError) as exc:
            raise TransitionError(
                "allowed keys need /dev/uinput; run the LockLock updater and restart"
            ) from exc

    def _release_forwarded_for_record(self, record: DeviceRecord) -> None:
        if self.allowed_uinput is None:
            record.forwarded_allowed.clear()
            return
        changed = False
        for code in tuple(record.forwarded_allowed):
            count = max(0, self.allowed_key_down_counts.get(code, 1) - 1)
            if count:
                self.allowed_key_down_counts[code] = count
            else:
                self.allowed_key_down_counts.pop(code, None)
                self._write_virtual_allowed(code, 0)
                changed = True
        record.forwarded_allowed.clear()
        if changed:
            self.allowed_uinput.syn()

    def _close_allowed_uinput(self) -> None:
        try:
            for record in self.devices.values():
                try:
                    self._release_forwarded_for_record(record)
                except Exception:
                    LOG.exception("Virtual key release failed; closing virtual device")
        finally:
            self.allowed_key_down_counts.clear()
            if self.allowed_uinput is not None:
                try:
                    self.allowed_uinput.close()
                except Exception:
                    LOG.exception("Could not close virtual input device")
                self.allowed_uinput = None


    def _forward_allowed_event(self, record: DeviceRecord, event: object) -> None:
        if (
            self.allowed_uinput is None
            or not record.grabbed
            or "keyboard" not in self.locked_kinds
            or event.code not in self.allowed_key_codes
        ):
            return
        code = int(event.code)
        value = int(event.value)
        if value == 1 and code not in record.forwarded_allowed:
            record.forwarded_allowed.add(code)
            count = self.allowed_key_down_counts.get(code, 0)
            self.allowed_key_down_counts[code] = count + 1
            if count == 0:
                self._write_virtual_allowed(code, 1)
                self.allowed_uinput.syn()
        elif value == 2 and code in record.forwarded_allowed:
            self._write_virtual_allowed(code, 2)
            self.allowed_uinput.syn()
        elif value == 0 and code in record.forwarded_allowed:
            record.forwarded_allowed.remove(code)
            count = max(0, self.allowed_key_down_counts.get(code, 1) - 1)
            if count:
                self.allowed_key_down_counts[code] = count
            else:
                self.allowed_key_down_counts.pop(code, None)
                self._write_virtual_allowed(code, 0)
                self.allowed_uinput.syn()

    def _write_virtual_allowed(self, code: int, value: int) -> None:
        if self.allowed_uinput is None:
            return
        if code == _key_code("KEY_F23") and "COPILOT" in self.allowed_keys:
            if value == 1:
                self.allowed_uinput.write(ecodes.EV_KEY, _key_code("KEY_LEFTMETA"), 1)
                self.allowed_uinput.write(ecodes.EV_KEY, _key_code("KEY_LEFTSHIFT"), 1)
                self.allowed_uinput.write(ecodes.EV_KEY, code, 1)
            elif value == 2:
                self.allowed_uinput.write(ecodes.EV_KEY, code, 2)
            else:
                self.allowed_uinput.write(ecodes.EV_KEY, code, 0)
                self.allowed_uinput.write(ecodes.EV_KEY, _key_code("KEY_LEFTSHIFT"), 0)
                self.allowed_uinput.write(ecodes.EV_KEY, _key_code("KEY_LEFTMETA"), 0)
            return
        self.allowed_uinput.write(ecodes.EV_KEY, code, value)

    def _set_ignore_lid_close(self, enabled: bool, *, generation: int | None = None) -> None:
        if not enabled:
            self._disable_lid("lid-ignore-disabled")
            self._persist_user_settings(ignore_lid_close=False)
            self._saved_lid_ignore_close = False
            self._write_safety(force=True)
            return
        if generation != self.lid_generation:
            raise TransitionError("stale lid request; refresh status before enabling")
        if not self.authorized_session_active or self.controller_fd not in self.clients:
            raise TransitionError("lid-ignore requires an active graphical session and connected tray")
        if enabled == self.ignore_lid_close:
            return
        records = [r for r in self.devices.values() if "lid-switch" in r.kinds and not r.excluded]
        if not records:
            raise TransitionError("no eligible lid switch input device was detected")
        if self.safety_sample is None:
            raise TransitionError("Safety telemetry is not ready")
        self.safety_sample.dependencies_ok = time.monotonic() - self.last_power_at <= 12 and time.monotonic() - self.last_logind_at <= 12
        try:
            self.safety.arm(self.safety_sample)
        except ValueError as exc:
            raise TransitionError(str(exc)) from exc
        acquired = []
        try:
            self._persist_user_settings(ignore_lid_close=True)
            self._saved_lid_ignore_close = True
            self._write_safety(force=True)
            self._acquire_lid_inhibitor()
            for record in records:
                if not record.grabbed:
                    record.device.grab()
                    record.grabbed = True
                    acquired.append(record)
        except Exception:
            self._release_records(acquired)
            self._release_lid_inhibitor()
            self.safety.release("ARM_FAILED")
            raise
        self.ignore_lid_close = True
        self.lid_policy_healthy = True
        self.lid_generation += 1
        self.lid_lease_deadline = self.clients[self.controller_fd].deadline
        try:
            self._write_safety(force=True)
        except OSError:
            self._emergency_unlock("safety-journal-failed")
            raise
        self.last_action = "lid-ignore-enabled"
        self._write_state()
        self._emit_state_event(self.last_action)

    def _disable_lid(self, reason: str) -> None:
        self.ignore_lid_close = False
        self.lid_policy_healthy = False
        self.lid_lease_deadline = None
        self.lid_generation += 1
        self._release_lid_inhibitor()
        self._release_sleep_inhibitor()
        records = [r for r in self.devices.values() if r.grabbed and "lid-switch" in r.kinds and not r.wants_grab(self.locked_kinds)]
        self.release_errors = self._release_records(records)
        self.safety.release()
        self._write_safety()
        self.last_action = reason
        self._write_state()
        self._emit_state_event(reason)


    def _set_lid_policy_health(self, healthy: bool) -> None:
        raise TransitionError("external power-policy repair is no longer supported")


    def _acquire_lid_inhibitor(self) -> None:
        if self.lid_inhibitor_fd is not None:
            return
        try:
            import dbus

            # Use a private synchronous connection. Reusing dbus.SystemBus()
            # here would cache a connection before LogindMonitor attaches its
            # GLib main loop, preventing that monitor from receiving signals.
            bus = dbus.bus.BusConnection(dbus.bus.BUS_SYSTEM)
            manager = dbus.Interface(
                bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1"),
                "org.freedesktop.login1.Manager",
            )
            descriptor = manager.Inhibit(
                "handle-lid-switch",
                "Input Lock",
                "Keep the computer running while its lid is closed",
                "block",
            )
            self.lid_inhibitor_fd = descriptor.take()
            LOG.info("logind lid-switch inhibitor acquired")
        except Exception as exc:
            raise TransitionError(f"cannot acquire logind lid inhibitor: {exc}") from exc

    def _release_lid_inhibitor(self) -> None:
        if self.lid_inhibitor_fd is None:
            return
        try:
            os.close(self.lid_inhibitor_fd)
        except OSError:
            pass
        self.lid_inhibitor_fd = None
        LOG.info("logind lid-switch inhibitor released")

    def _sleep_should_be_inhibited(self) -> bool:
        return False


    def _reconcile_sleep_inhibitor(self) -> None:
        if self._sleep_should_be_inhibited():
            self._acquire_sleep_inhibitor()
        else:
            self._release_sleep_inhibitor()

    def _acquire_sleep_inhibitor(self) -> None:
        raise TransitionError("blocking sleep inhibitors are not supported")


    def _release_sleep_inhibitor(self) -> None:
        if self.sleep_inhibitor_fd is None:
            return
        try:
            os.close(self.sleep_inhibitor_fd)
        except OSError:
            pass
        self.sleep_inhibitor_fd = None
        LOG.info("Fallback logind sleep inhibitor released")

    # ------------------------- device handling -------------------------

    @staticmethod
    def _list_event_devices() -> set[str]:
        """evdev.list_devices(), tolerating nodes that vanish mid-scan.

        evdev lists /dev/input and then stat()s each entry; a device removed in
        between (e.g. a Bluetooth mouse disconnecting on lid close) raises
        FileNotFoundError.  Fall back to a scan that skips such entries.
        """
        try:
            return set(evdev.list_devices())
        except OSError:
            pass
        paths: set[str] = set()
        try:
            names = os.listdir("/dev/input")
        except FileNotFoundError:
            return paths
        for name in names:
            if not name.startswith("event"):
                continue
            path = os.path.join("/dev/input", name)
            try:
                mode = os.stat(path).st_mode
            except OSError:
                continue
            if stat.S_ISCHR(mode) and os.access(path, os.R_OK | os.W_OK):
                paths.add(path)
        return paths

    def _rescan_devices(self, *, reclassify: bool = False) -> None:
        current_paths = self._list_event_devices()
        known_paths = set(self.devices)
        for path in sorted(known_paths - current_paths):
            self._detach_device(path)
        if reclassify:
            for path in sorted(known_paths & current_paths):
                self._detach_device(path)
        for path in sorted(current_paths - set(self.devices)):
            self._attach_device(path)
        self._write_state()

    def _attach_device(self, path: str) -> None:
        try:
            device = evdev.InputDevice(path)
            kinds = self._classify_device(path, device)
            if not kinds:
                device.close()
                return
            by_id = self._by_id_links(path)
            vendor_product = f"{device.info.vendor:04x}:{device.info.product:04x}"
            excluded, reason = self._is_excluded(
                path, by_id, vendor_product, device.name or ""
            )
            try:
                pressed = set(int(code) for code in device.active_keys())
            except OSError:
                pressed = set()
            record = DeviceRecord(
                path=path,
                device=device,
                name=device.name or Path(path).name,
                kinds=kinds,
                vendor_product=vendor_product,
                by_id=by_id,
                excluded=excluded,
                exclusion_reason=reason,
                pressed=pressed,
            )
            self.devices[path] = record
            self.selector.register(device.fd, selectors.EVENT_READ, ("device", path))
            LOG.info(
                "Input device detected: %s (%s), classes=%s%s",
                path,
                record.name,
                ",".join(sorted(kinds)),
                f", excluded={reason}" if excluded else "",
            )
            if record.wants_managed_grab(
                self.locked_kinds, self.ignore_lid_close
            ):
                try:
                    if record.pressed:
                        raise TransitionError("device has a pressed key or button")
                    record.device.grab()
                    record.grabbed = True
                    LOG.info("New input device grabbed while locked: %s", path)
                except (OSError, TransitionError) as exc:
                    # Keeping the new device usable is safer than wedging the seat.
                    LOG.warning(
                        "New device could not be grabbed while locked; it remains an "
                        "emergency input path: %s: %s",
                        path,
                        exc,
                    )
        except (OSError, PermissionError) as exc:
            LOG.warning("Cannot open input device %s: %s", path, exc)

    def _detach_device(self, path: str) -> None:
        record = self.devices.pop(path, None)
        if record is None:
            return
        try:
            self.selector.unregister(record.device.fd)
        except Exception:
            pass
        if record.grabbed:
            try:
                self._release_forwarded_for_record(record)
            except Exception:
                LOG.exception("Virtual key release failed while detaching device")
                self._close_allowed_uinput()
            try:
                record.device.ungrab()
            except OSError:
                pass
            record.grabbed = False
        try:
            record.device.close()
        except OSError:
            pass
        if "lid-switch" in record.kinds:
            self.lid_state_from_evdev = False
            if self.ignore_lid_close:
                self._start_protection("LID_DEVICE_LOST")
        LOG.info("Input device removed: %s (%s)", path, record.name)
        self._maybe_fire_pending_hotkey()

    def _handle_udev(self) -> None:
        monitor = self.udev_monitor
        if monitor is None:
            return
        saw_event = False
        while True:
            device = monitor.poll(timeout=0)
            if device is None:
                break
            saw_event = True
        if saw_event:
            try:
                self._rescan_devices()
            except OSError:
                LOG.warning("Transient error during device rescan; will retry on next udev event", exc_info=True)

    def _read_device(self, path: str) -> None:
        record = self.devices.get(path)
        if record is None:
            return
        try:
            for event in record.device.read():
                if (
                    event.type != ecodes.EV_SYN
                    and bool(record.kinds & LOCKABLE_KINDS)
                    and not self.locked_kinds
                ):
                    self.last_input_activity = time.monotonic()
                if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_DROPPED:
                    record.synchronizing = True
                    try:
                        self._release_forwarded_for_record(record)
                    except OSError:
                        self._close_allowed_uinput()
                    continue
                if record.synchronizing:
                    if event.type != ecodes.EV_SYN or event.code != ecodes.SYN_REPORT:
                        continue
                    record.synchronizing = False
                    try:
                        record.pressed = set(int(code) for code in record.device.active_keys())
                    except OSError:
                        record.pressed.clear()
                    for code in record.pressed & self.allowed_key_codes:
                        from types import SimpleNamespace
                        self._forward_allowed_event(record, SimpleNamespace(code=code, value=1))
                    if "lid-switch" in record.kinds:
                        try:
                            self._handle_lid_switch_event(ecodes.SW_LID in record.device.switches())
                        except OSError:
                            if self.ignore_lid_close:
                                self._start_protection("LID_TELEMETRY_LOST")
                    self._evaluate_hotkeys()
                    continue
                if (
                    event.type == ecodes.EV_SW
                    and event.code == ecodes.SW_LID
                    and "lid-switch" in record.kinds
                ):
                    self._handle_lid_switch_event(bool(event.value))
                    continue
                if event.type != ecodes.EV_KEY:
                    continue
                if event.value == 1:
                    record.pressed.add(int(event.code))
                elif event.value == 0:
                    record.pressed.discard(int(event.code))
                # value=2 is autorepeat and intentionally does not trigger an edge.
                if "keyboard" in record.kinds:
                    self._forward_allowed_event(record, event)
                    self._evaluate_hotkeys()
            self._maybe_fire_pending_hotkey()
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno in {errno.ENODEV, errno.EIO, errno.EBADF}:
                self._detach_device(path)
            else:
                LOG.warning("Read failed for %s: %s", path, exc)

    def _handle_lid_switch_event(self, closed: bool) -> None:
        changed = closed != self.lid_is_closed or not self.lid_state_from_evdev
        self.lid_state_from_evdev = True
        self.lid_is_closed = closed
        self.last_lid_event_at = time.monotonic()
        if self.safety_sample is not None:
            self.safety_sample.lid_closed = closed
            reason = self.safety.update(self.safety_sample)
            if reason:
                self._start_protection(reason)
            self._write_safety()
        try:
            self._reconcile_sleep_inhibitor()
        except TransitionError as exc:
            LOG.error("Conditional sleep prevention unavailable: %s", exc)
        if changed:
            self.last_action = "lid-closed" if closed else "lid-opened"
            self._write_state()
            self._emit_state_event(self.last_action)
            LOG.info("Physical lid state changed: closed=%s", closed)

    def _classify_device(
        self, path: str, device: evdev.InputDevice
    ) -> frozenset[str]:
        kinds: set[str] = set()
        try:
            udev_device = pyudev.Devices.from_device_file(self.udev_context, path)
            props = udev_device.properties
            if props.get("ID_INPUT_KEYBOARD") == "1":
                kinds.add("keyboard")
            if props.get("ID_INPUT_MOUSE") == "1" or props.get("ID_INPUT_TRACKBALL") == "1":
                kinds.add("mouse")
            if props.get("ID_INPUT_POINTINGSTICK") == "1":
                kinds.add("mouse")
            if props.get("ID_INPUT_TOUCHPAD") == "1":
                kinds.add("touchpad")
            if props.get("ID_INPUT_TOUCHSCREEN") == "1":
                kinds.add("touchscreen")
        except (OSError, pyudev.DeviceNotFoundError):
            pass

        # Conservative capability fallback for devices without udev tags.
        try:
            capabilities = device.capabilities(absinfo=False)
            keys = set(capabilities.get(ecodes.EV_KEY, []))
            relative = set(capabilities.get(ecodes.EV_REL, []))
            absolute = set(capabilities.get(ecodes.EV_ABS, []))
            switches = set(capabilities.get(ecodes.EV_SW, []))
            input_props = set(device.input_props())
        except OSError:
            return frozenset(kinds)

        letter_codes = {_key_code(f"KEY_{letter}") for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
        if len(keys & letter_codes) >= 10 and _key_code("KEY_ENTER") in keys:
            kinds.add("keyboard")
        if (
            ecodes.REL_X in relative
            and ecodes.REL_Y in relative
            and ecodes.BTN_LEFT in keys
        ):
            kinds.add("mouse")
        has_abs_pointer = (
            ecodes.ABS_X in absolute
            and ecodes.ABS_Y in absolute
            and (ecodes.BTN_TOUCH in keys or ecodes.BTN_TOOL_FINGER in keys)
        )
        if has_abs_pointer:
            if ecodes.INPUT_PROP_DIRECT in input_props:
                kinds.add("touchscreen")
            elif ecodes.INPUT_PROP_POINTER in input_props:
                kinds.add("touchpad")
        if ecodes.SW_LID in switches:
            kinds.add("lid-switch")
        return frozenset(kinds)

    @staticmethod
    def _by_id_links(path: str) -> tuple[str, ...]:
        target = os.path.realpath(path)
        links: list[str] = []
        directory = Path("/dev/input/by-id")
        if directory.is_dir():
            for link in directory.iterdir():
                try:
                    if os.path.realpath(link) == target:
                        links.append(str(link))
                except OSError:
                    continue
        return tuple(sorted(links))

    def _is_excluded(
        self,
        path: str,
        by_id: tuple[str, ...],
        vendor_product: str,
        name: str,
    ) -> tuple[bool, str]:
        if name.startswith("Input Lock Virtual"):
            return True, "internal virtual device"
        if vendor_product.lower() in self.config.exclude_vendor_product:
            return True, f"vendor:product {vendor_product}"
        candidates = {path, os.path.realpath(path), *by_id}
        for pattern in self.config.exclude_devices:
            if any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates):
                return True, f"path pattern {pattern}"
            resolved_pattern = os.path.realpath(pattern)
            resolved_candidates = {os.path.realpath(candidate) for candidate in candidates}
            if resolved_pattern in resolved_candidates:
                return True, f"path {pattern}"
        return False, ""

    # --------------------------- hotkeys -------------------------------

    def _pressed_keyboard_codes(self) -> set[int]:
        pressed: set[int] = set()
        for record in self.devices.values():
            if "keyboard" in record.kinds:
                pressed.update(record.pressed)
        return pressed

    def _evaluate_hotkeys(self) -> None:
        pressed = self._pressed_keyboard_codes()
        emergency_match = self.emergency_hotkey.matches(pressed)
        primary_match = self.primary_hotkey.matches(pressed)
        vt_match = self._matches_vt_hotkey(pressed)

        if emergency_match and not self.emergency_down:
            self._schedule_hotkey(
                "emergency-unlock",
                "all-input",
                self.emergency_hotkey.all_codes,
                "emergency-hotkey",
            )
        elif (
            primary_match
            and not self.primary_down
            and (self.authorized_session_active or bool(self.locked_kinds))
        ):
            self._schedule_hotkey(
                "toggle-default",
                "all-input",
                self.primary_hotkey.all_codes,
                "primary-hotkey",
            )
        elif (
            vt_match
            and not self.vt_down
            and not emergency_match
            and self.config.unlock_on_vt_hotkey
            and bool(self.locked_kinds)
        ):
            self._schedule_hotkey(
                "emergency-unlock",
                "all-input",
                self._vt_release_codes(pressed),
                "tty-recovery-hotkey",
            )

        self.emergency_down = emergency_match
        self.primary_down = primary_match
        self.vt_down = vt_match

    def _matches_vt_hotkey(self, pressed: set[int]) -> bool:
        ctrl = {_key_code("KEY_LEFTCTRL"), _key_code("KEY_RIGHTCTRL")}
        alt = {_key_code("KEY_LEFTALT"), _key_code("KEY_RIGHTALT")}
        fkeys = {_key_code(f"KEY_F{number}") for number in range(1, 13)}
        return bool(ctrl & pressed) and bool(alt & pressed) and bool(fkeys & pressed)

    def _vt_release_codes(self, pressed: set[int]) -> frozenset[int]:
        ctrl_alt = {
            _key_code("KEY_LEFTCTRL"),
            _key_code("KEY_RIGHTCTRL"),
            _key_code("KEY_LEFTALT"),
            _key_code("KEY_RIGHTALT"),
        }
        fkeys = {_key_code(f"KEY_F{number}") for number in range(1, 13)}
        return frozenset(ctrl_alt | (pressed & fkeys))

    def _schedule_hotkey(
        self,
        action: str,
        mode: str,
        release_codes: frozenset[int],
        source: str,
    ) -> str:
        # A GNOME fallback process may reach the IPC socket before this event
        # loop has drained the same physical key events. Query kernel state so
        # it can never grab a keyboard while Ctrl/Alt/Z is still physically down.
        if source.startswith("gnome-"):
            for record in self.devices.values():
                if "keyboard" not in record.kinds:
                    continue
                try:
                    record.pressed = set(
                        int(code) for code in record.device.active_keys()
                    )
                except OSError as exc:
                    LOG.warning("Could not refresh key state for %s: %s", record.path, exc)
        now = time.monotonic()
        debounce = self.config.debounce_ms / 1000.0
        if now - self.last_hotkey_at < debounce:
            return "ignored-debounce"
        if self.pending_hotkey is not None:
            if action == "emergency-unlock":
                self.pending_hotkey = None
            else:
                return "already-pending"
        self.pending_hotkey = PendingHotkey(
            action=action,
            mode=mode,
            release_codes=release_codes,
            source=source,
            created_at=now,
        )
        self._maybe_fire_pending_hotkey()
        return "scheduled"

    def _maybe_fire_pending_hotkey(self) -> None:
        pending = self.pending_hotkey
        if pending is None:
            return
        if pending.release_codes & self._pressed_keyboard_codes():
            return
        self.pending_hotkey = None
        self.last_hotkey_at = time.monotonic()
        try:
            if pending.action == "emergency-unlock":
                self._emergency_unlock(pending.source)
            elif pending.action == "toggle-default":
                self._toggle_default_kinds(reason=pending.source)
            else:
                self._apply_action(
                    pending.action, pending.mode, timeout=None, reason=pending.source
                )
        except TransitionError as exc:
            LOG.error("Hotkey transition rejected safely: %s", exc)

    def _default_hotkey_mode(self) -> str:
        if self.config.default_mode == "all" and self.config.lock_touchscreen:
            return "all-input"
        return self.config.default_mode

    # ----------------------- locking transitions -----------------------

    @staticmethod
    def _mode_kinds(mode: str) -> set[str]:
        mapping = {
            "keyboard": {"keyboard"},
            "mouse": {"mouse", "touchpad"},
            "touchpad": {"touchpad"},
            "touchscreen": {"touchscreen"},
            "all": set(BASE_ALL_KINDS),
            "all-input": set(ALL_INPUT_KINDS),
        }
        try:
            return set(mapping[mode])
        except KeyError as exc:
            raise TransitionError(f"invalid mode: {mode}") from exc

    def _apply_action(
        self,
        action: str,
        mode: str,
        *,
        timeout: int | None,
        reason: str,
    ) -> None:
        targets = self._mode_kinds(mode)
        new_locked = set(self.locked_kinds)
        if action == "lock":
            new_locked.update(targets)
        elif action == "unlock":
            new_locked.difference_update(targets)
        elif action == "toggle":
            if targets.issubset(new_locked):
                new_locked.difference_update(targets)
            else:
                new_locked.update(targets)
        else:
            raise TransitionError(f"invalid action: {action}")
        self._set_locked_kinds(new_locked, reason=reason, timeout=timeout)

    def _toggle_default_kinds(self, *, reason: str) -> None:
        targets = set(self.default_locked_kinds)
        new_locked = set(self.locked_kinds)
        if new_locked:
            new_locked.clear()
        else:
            new_locked.update(targets)
        self._set_locked_kinds(new_locked, reason=reason, timeout=None)

    @staticmethod
    def _wait_for_input_release(
        records: list[DeviceRecord], *, timeout_seconds: float = RELEASE_WAIT_SECONDS
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            busy: list[DeviceRecord] = []
            for record in records:
                if not (record.kinds & LOCKABLE_KINDS):
                    record.pressed.clear()
                    continue
                try:
                    record.pressed = set(
                        int(code) for code in record.device.active_keys()
                    )
                except OSError as exc:
                    raise TransitionError(
                        f"cannot verify released-key state for {record.path}: {exc}"
                    ) from exc
                if record.pressed:
                    busy.append(record)
            if not busy:
                return

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                names = ", ".join(record.name for record in busy[:3])
                raise TransitionError(
                    "lock refused because a key or button remained pressed on: "
                    + names
                )
            time.sleep(min(RELEASE_POLL_SECONDS, remaining))

    def _set_locked_kinds(
        self, new_locked: set[str], *, reason: str, timeout: int | None
    ) -> None:
        new_locked &= set(LOCKABLE_KINDS)
        before = set(self.locked_kinds)
        if new_locked - before and not self.authorized_session_active:
            raise TransitionError("locking requires the authorized active graphical session")
        if "keyboard" in new_locked - before and self.allowed_key_codes:
            self._ensure_allowed_uinput()
        to_acquire = [
            record
            for record in self.devices.values()
            if not new_locked.issubset(before)
            and record.wants_managed_grab(new_locked, self.ignore_lid_close)
            and not record.grabbed
        ]
        to_release = [
            record
            for record in self.devices.values()
            if record.grabbed
            and not record.wants_managed_grab(new_locked, self.ignore_lid_close)
        ]

        # A command submitted from a terminal reaches this daemon on the
        # Enter-key press, usually a few milliseconds before Enter is released.
        # Wait briefly for that normal release before grabbing. The wait is
        # bounded, performed while every device is still ungrabbed, and a truly
        # held/stuck key or button still causes a safe refusal.
        self._wait_for_input_release(to_acquire)

        acquired: list[DeviceRecord] = []
        try:
            for record in to_acquire:
                record.device.grab()
                record.grabbed = True
                acquired.append(record)
        except OSError as exc:
            self._release_records(list(reversed(acquired)))
            raise TransitionError(
                f"EVIOCGRAB failed; transition rolled back: {exc}"
            ) from exc

        self.release_errors = self._release_records(to_release)

        self.locked_kinds = new_locked
        if not new_locked:
            self.auto_unlock_deadline = None
            self.last_input_activity = time.monotonic()
        elif timeout is not None or not before:
            seconds = self.config.auto_unlock_seconds if timeout is None else timeout
            self.auto_unlock_deadline = (
                time.monotonic() + seconds if seconds and seconds > 0 else None
            )
        self.last_action = reason
        self._write_state()
        self._emit_state_event(reason)

        if before != new_locked:
            if new_locked:
                LOG.info(
                    "Input locked: %s; reason=%s; auto-unlock=%s",
                    ",".join(sorted(new_locked)),
                    reason,
                    self._auto_unlock_label(),
                )
            else:
                LOG.info("Input unlocked: all; reason=%s", reason)

    def _release_all_grabs(self) -> None:
        self._emergency_unlock("release-all")


    def _emergency_unlock(self, reason: str, *, preserve_safety: bool = False) -> None:
        # No acquisition, device reopening, persistence prerequisite, or policy
        # reconciliation may precede release. Each restriction is independent.
        self.pending_hotkey = None
        self.ignore_lid_close = False
        self.lid_policy_healthy = False
        self.lid_lease_deadline = None
        self.lid_generation += 1
        self.locked_kinds.clear()
        self.auto_unlock_deadline = None
        self.release_errors = self._release_records(list(self.devices.values()))
        self._release_lid_inhibitor()
        self._release_sleep_inhibitor()
        self._close_allowed_uinput()
        if not preserve_safety:
            self.safety.release()
            self._write_safety()
        self.last_input_activity = time.monotonic()
        self.last_action = reason
        self._write_state()
        self._emit_state_event(reason)
        if self.release_errors:
            LOG.error("Emergency release completed with errors: %s", self.release_errors)
        else:
            LOG.info("Emergency unlock completed; reason=%s", reason)

    def _release_records(self, records: list[DeviceRecord]) -> list[str]:
        errors = []
        for record in list(records):
            if not record.grabbed:
                continue
            try:
                self._release_forwarded_for_record(record)
            except Exception as exc:
                errors.append(f"{record.path}: virtual release: {exc}")
            try:
                record.device.ungrab()
                record.grabbed = False
            except Exception as exc:
                errors.append(f"{record.path}: ungrab: {exc}")
                try:
                    descriptor = record.device.fd
                    record.device.close()
                    record.grabbed = False
                    self.devices.pop(record.path, None)
                    try:
                        self.selector.unregister(descriptor)
                    except Exception:
                        pass
                except Exception as close_exc:
                    errors.append(f"{record.path}: close: {close_exc}")
        return errors


    # -------------------------- timers/state ---------------------------

    def _next_timeout(self) -> float | None:
        deadlines: list[float] = [self.next_safety_tick]
        if self.accept_retry_at is not None:
            deadlines.append(self.accept_retry_at)
        deadlines.extend(client.deadline for client in self.clients.values())
        if self.lid_lease_deadline is not None:
            deadlines.append(self.lid_lease_deadline)
        if self.auto_unlock_deadline is not None:
            deadlines.append(self.auto_unlock_deadline)
        if (
            self.idle_lock_enabled
            and self.authorized_session_active
            and not self.locked_kinds
        ):
            deadlines.append(self.last_input_activity + self.idle_lock_seconds)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - time.monotonic())

    def _handle_timers(self) -> None:
        now = time.monotonic()
        if now >= self.next_safety_tick:
            self._safety_tick()
        if self.accept_retry_at is not None and now >= self.accept_retry_at:
            self.accept_retry_at = None
            self.selector.register(self.server, selectors.EVENT_READ, ("server", None))
        for fd, client in list(self.clients.items()):
            if now >= client.deadline:
                self._close_client(fd)
        if self.lid_lease_deadline is not None and now >= self.lid_lease_deadline:
            self._disable_lid("controller-lease-expired")
        if (
            self.auto_unlock_deadline is not None
            and time.monotonic() >= self.auto_unlock_deadline
        ):
            self._emergency_unlock("auto-unlock-timer")
        if (
            self.idle_lock_enabled
            and self.authorized_session_active
            and not self.locked_kinds
            and time.monotonic()
            >= self.last_input_activity + self.idle_lock_seconds
        ):
            try:
                self._set_locked_kinds(
                    set(self.default_locked_kinds),
                    reason="idle-auto-lock",
                    timeout=None,
                )
            except TransitionError as exc:
                self.last_input_activity = time.monotonic()
                LOG.warning("Idle auto-lock rejected safely: %s", exc)

    def _auto_unlock_label(self) -> str:
        if self.auto_unlock_deadline is None:
            return "disabled"
        remaining = max(0, round(self.auto_unlock_deadline - time.monotonic()))
        return f"{remaining}s"

    def _state_payload(self) -> dict[str, object]:
        desired_ungrabbed = [
            record.path
            for record in self.devices.values()
            if record.wants_managed_grab(
                self.locked_kinds, self.ignore_lid_close
            )
            and not record.grabbed
        ]
        return {
            "version": VERSION,
            "daemon": "running",
            "locked": {
                "keyboard": "keyboard" in self.locked_kinds,
                "mouse": "mouse" in self.locked_kinds,
                "touchpad": "touchpad" in self.locked_kinds,
                "touchscreen": "touchscreen" in self.locked_kinds,
            },
            "locked_kinds": sorted(self.locked_kinds),
            "default_locked_kinds": sorted(self.default_locked_kinds),
            "idle_lock_enabled": self.idle_lock_enabled,
            "idle_lock_seconds": self.idle_lock_seconds,
            "allowed_keys": list(self.allowed_keys),
            "active_devices": len(self.devices),
            "grabbed_devices": sum(record.grabbed for record in self.devices.values()),
            "incomplete_devices": desired_ungrabbed,
            "primary_hotkey": self.primary_hotkey.text,
            "emergency_hotkey": self.emergency_hotkey.text,
            "ignore_lid_close": self.ignore_lid_close,
            "lid_inhibitor_active": self.lid_inhibitor_fd is not None,
            "lid_policy_healthy": self.lid_policy_healthy,
            "lid_no_suspend_effective": (
                self.ignore_lid_close
                and (
                    self.lid_inhibitor_fd is not None
                    and self.lid_policy_healthy
                )
            ),
            "unsupported_settings": ["lid_power_policy_enabled", "lid_battery_threshold"],
            "power_policy": "normal-os-policy",
            "safety": self.safety.snapshot(),
            "lid_ignore_preferred": self._saved_lid_ignore_close,
            "protective_result": self.protective_result,
            "lid_generation": self.lid_generation,
            "lid_controller_connected": self.controller_fd in self.clients,
            "lid_lease_deadline": self.lid_lease_deadline,
            "release_errors": self.release_errors,
            "sleep_inhibitor_active": self.sleep_inhibitor_fd is not None,
            "on_battery": self.on_battery,
            "lid_is_closed": self.lid_is_closed,
            "battery_percentage": self.battery_percentage,
            "lid_switch_devices": sum(
                "lid-switch" in record.kinds for record in self.devices.values()
            ),
            "auto_unlock": self._auto_unlock_label(),
            "authorized_uid": self.config.authorized_uid,
            "authorized_session_active": self.authorized_session_active,
            "last_action": self.last_action,
            "timestamp": int(time.time()),
        }

    def _write_state(self) -> None:
        state_path = self.config.state_path
        state_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        payload = json.dumps(self._state_payload(), ensure_ascii=False, indent=2) + "\n"
        temporary_name: str | None = None
        try:
            fd, temporary_name = tempfile.mkstemp(
                prefix=".state-", dir=str(state_path.parent), text=True
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o644)
            os.replace(temporary_name, state_path)
        except OSError as exc:
            LOG.warning("Cannot write state file %s: %s", state_path, exc)
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    # ----------------------------- IPC --------------------------------

    def _accept_clients(self) -> None:
        if self.server is None:
            return
        # Bound work per selector turn as well as live connection count.
        for _ in range(MAX_CLIENTS):
            try:
                client, _address = self.server.accept()
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in {errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM}:
                    LOG.error("IPC admission resource limit: %s", exc)
                    self.selector.unregister(self.server)
                    self.accept_retry_at = time.monotonic() + 1
                    return
                raise
            client.setblocking(False)
            try:
                _pid, uid, _gid = struct.unpack(
                    PEERCRED_FORMAT,
                    client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEERCRED_SIZE),
                )
            except OSError:
                client.close()
                continue
            if uid not in {0, self.config.authorized_uid} or len(self.clients) >= MAX_CLIENTS:
                client.close()
                continue
            state = ClientState(sock=client, uid=uid)
            fd = client.fileno()
            self.clients[fd] = state
            self.selector.register(client, selectors.EVENT_READ, ("client", fd))

    def _read_client(self, fd: int) -> None:
        state = self.clients.get(fd)
        if state is None:
            return
        try:
            data = state.sock.recv(4096)
        except BlockingIOError:
            return
        except OSError:
            self._close_client(fd)
            return
        if not data:
            self._close_client(fd)
            return
        state.buffer.extend(data)
        if len(state.buffer) > MAX_MESSAGE_BYTES:
            self._send_and_close(fd, {"ok": False, "error": "request too large"})
            return
        if b"\n" not in state.buffer:
            return
        raw, _separator, remainder = bytes(state.buffer).partition(b"\n")
        if remainder.strip():
            self._send_and_close(fd, {"ok": False, "error": "one request per connection"})
            return
        state.buffer.clear()
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            self._send_and_close(fd, {"ok": False, "error": str(exc)})
            return

        if state.uid not in {0, self.config.authorized_uid}:
            LOG.warning("Rejected control request from unauthorized UID=%d", state.uid)
            self._send_and_close(fd, {"ok": False, "error": "unauthorized UID"})
            return
        self._handle_request(fd, request)

    def _handle_request(self, fd: int, request: dict[str, object]) -> None:
        cmd = request.get("cmd")
        if not isinstance(cmd, str):
            self._send_and_close(fd, {"ok": False, "error": "cmd must be a string"})
            return
        try:
            if cmd == "subscribe":
                if len(self.subscribers) >= MAX_SUBSCRIBERS:
                    raise ValueError("subscriber limit reached")
                state = self.clients[fd]
                if request.get("controller") is True:
                    if self.controller_fd is not None and self.controller_fd != fd:
                        raise ValueError("a tray controller is already connected")
                    self.controller_fd = fd
                state.subscribed = True
                state.deadline = time.monotonic() + CONTROLLER_TIMEOUT
                state.buffer.clear()
                self.subscribers.add(fd)
                self._send(fd, {"ok": True, "subscribed": True, "state": self._state_payload()})
                return
            if cmd == "heartbeat":
                state = self.clients[fd]
                if not state.subscribed:
                    raise ValueError("heartbeat requires subscription")
                state.deadline = time.monotonic() + CONTROLLER_TIMEOUT
                if fd == self.controller_fd and self.ignore_lid_close:
                    self.lid_lease_deadline = state.deadline
                self._send(fd, {"ok": True, "heartbeat": True})
                return
            if cmd == "status":
                response: dict[str, object] = {"ok": True, "state": self._state_payload()}
            elif cmd == "list-devices":
                response = {"ok": True, "devices": self._device_payloads()}
            elif cmd in {"lock", "unlock", "toggle"}:
                mode = self._validated_mode(request.get("mode"))
                timeout = self._validated_timeout(request.get("timeout"))
                self._apply_action(cmd, mode, timeout=timeout, reason=f"ipc-{cmd}")
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "lock-default":
                self._set_locked_kinds(set(self.default_locked_kinds) | self.locked_kinds, reason="ipc-lock-default", timeout=None)
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "emergency-unlock":
                self._emergency_unlock("ipc-emergency-unlock")
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "gnome-hotkey":
                result = self._schedule_hotkey(
                    "toggle-default",
                    "all-input",
                    self.primary_hotkey.all_codes,
                    "gnome-primary-hotkey",
                )
                response = {"ok": True, "scheduled": result, "state": self._state_payload()}
            elif cmd == "gnome-emergency":
                result = self._schedule_hotkey(
                    "emergency-unlock",
                    "all-input",
                    self.emergency_hotkey.all_codes,
                    "gnome-emergency-hotkey",
                )
                response = {"ok": True, "scheduled": result, "state": self._state_payload()}
            elif cmd == "session-ended":
                self.authorized_session_active = False
                self._emergency_unlock("user-session-ended")
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "prepare-sleep":
                if self.config.unlock_on_suspend:
                    self._emergency_unlock("system-sleep-hook")
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "reload":
                self.reload_requested = True
                response = {"ok": True, "message": "reload scheduled"}
            elif cmd == "quit":
                # A clean exit is intentionally not restarted by the systemd unit's
                # Restart=on-failure policy. Cleanup releases every managed grab.
                LOG.info("Authorized Quit request received; shutting down safely")
                response = {"ok": True, "message": "shutdown scheduled"}
                self._send_and_close(fd, response)
                self.running = False
                self._wake()
                return
            elif cmd == "set-hotkey":
                hotkey = request.get("hotkey")
                if (
                    not isinstance(hotkey, str)
                    or not hotkey
                    or len(hotkey) > 80
                    or any(character in hotkey for character in "\x00\r\n")
                ):
                    raise ValueError("hotkey must be a non-empty shortcut string")
                self._set_primary_hotkey(hotkey)
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "set-lid-ignore":
                enabled = request.get("enabled")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                generation = request.get("generation")
                if enabled and (isinstance(generation, bool) or not isinstance(generation, int)):
                    raise ValueError("enable requires the current lid generation")
                self._set_ignore_lid_close(enabled, generation=generation)
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "set-lid-policy-health":
                healthy = request.get("healthy")
                if not isinstance(healthy, bool):
                    raise ValueError("healthy must be a boolean")
                self._set_lid_policy_health(healthy)
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "set-lock-kinds":
                kinds = self._validated_lock_kinds(request.get("kinds"))
                self._set_default_lock_kinds(kinds)
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "set-idle-lock":
                enabled = request.get("enabled")
                seconds = request.get("seconds")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                if isinstance(seconds, bool) or not isinstance(seconds, int):
                    raise ValueError("seconds must be an integer")
                if not 60 <= seconds <= 86_400:
                    raise ValueError("seconds must be between 60 and 86400")
                self._set_idle_lock(enabled, seconds)
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "set-allowed-keys":
                keys = validate_allowed_keys(request.get("keys"))
                self._set_allowed_keys(keys)
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "set-lid-power-policy":
                enabled = request.get("enabled")
                threshold = request.get("threshold")
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                if isinstance(threshold, bool) or not isinstance(threshold, int):
                    raise ValueError("threshold must be an integer")
                if not 1 <= threshold <= 99:
                    raise ValueError("threshold must be between 1 and 99")
                self._set_lid_power_policy(enabled, threshold)
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "toggle-default":
                self._toggle_default_kinds(reason="ipc-toggle-default")
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "ping":
                response = {"ok": True, "version": VERSION}
            else:
                raise ValueError("unsupported command")
            self._send_and_close(fd, response)
        except (TransitionError, ValueError) as exc:
            self._send_and_close(fd, {"ok": False, "error": str(exc)})
        except Exception as exc:
            LOG.exception("Unexpected control request failure for %s", cmd)
            self._send_and_close(fd, {"ok": False, "error": f"internal error: {exc}"})

    @staticmethod
    def _validated_mode(value: object) -> str:
        if not isinstance(value, str) or value not in {
            "all",
            "all-input",
            "keyboard",
            "mouse",
            "touchpad",
            "touchscreen",
        }:
            raise ValueError(
                "mode must be all, all-input, keyboard, mouse, touchpad, or touchscreen"
            )
        return value

    @staticmethod
    def _validated_timeout(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("timeout must be an integer")
        if not 0 <= value <= 86_400:
            raise ValueError("timeout must be between 0 and 86400 seconds")
        return value

    @staticmethod
    def _validated_lock_kinds(value: object) -> set[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("kinds must be a non-empty list")
        if any(not isinstance(item, str) for item in value):
            raise ValueError("each lock kind must be a string")
        kinds = set(value)
        allowed = {"keyboard", "mouse", "touchpad", "touchscreen"}
        if not kinds <= allowed:
            raise ValueError("unsupported lock kind")
        if ("mouse" in kinds) != ("touchpad" in kinds):
            raise ValueError("mouse and touchpad must be configured together")
        return kinds

    def _device_payloads(self) -> list[dict[str, object]]:
        return [
            {
                "path": record.path,
                "name": record.name,
                "classes": sorted(record.kinds),
                "vendor_product": record.vendor_product,
                "by_id": list(record.by_id),
                "excluded": record.excluded,
                "exclusion_reason": record.exclusion_reason,
                "grabbed": record.grabbed,
            }
            for record in sorted(self.devices.values(), key=lambda item: item.path)
        ]

    def _send(self, fd: int, payload: dict[str, object]) -> bool:
        state = self.clients.get(fd)
        if state is None:
            return False
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            state.sock.sendall(encoded)
            return True
        except (BlockingIOError, BrokenPipeError, OSError):
            self._close_client(fd)
            return False

    def _send_and_close(self, fd: int, payload: dict[str, object]) -> None:
        self._send(fd, payload)
        self._close_client(fd)

    def _close_client(self, fd: int) -> None:
        state = self.clients.pop(fd, None)
        self.subscribers.discard(fd)
        if state is None:
            return
        try:
            self.selector.unregister(state.sock)
        except Exception:
            pass
        try:
            state.sock.close()
        except OSError:
            pass
        if fd == self.controller_fd:
            self.controller_fd = None
            if self.ignore_lid_close and self._lid_may_be_closed() and self.running:
                self._start_protection("CONTROLLER_LOST")
            elif self.ignore_lid_close:
                self._disable_lid("controller-disconnected")

    def _emit_state_event(self, reason: str) -> None:
        payload = {
            "type": "event",
            "event": "state-changed",
            "reason": reason,
            "state": self._state_payload(),
        }
        for fd in list(self.subscribers):
            self._send(fd, payload)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    config_path = Path(os.environ.get("INPUT_LOCK_CONFIG", "/etc/input-lock/input-lock.conf"))
    try:
        daemon = InputLockDaemon(config_path)
    except (ConfigError, OSError) as exc:
        LOG.error("Unsafe or invalid startup configuration; no input was locked: %s", exc)
        return 2
    return daemon.run()


if __name__ == "__main__":
    raise SystemExit(main())
