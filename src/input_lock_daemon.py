#!/usr/bin/python3
"""Privileged, event-driven input lock daemon for Linux evdev.

The daemon opens selected evdev nodes read-only while unlocked.  During a lock
it uses EVIOCGRAB, so the same process remains the sole event recipient and can
recognize the unlock chords.  It never stores, transmits, or logs key content.
"""

from __future__ import annotations

import errno
import fcntl
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
from input_lock_safety import (BREADCRUMB, DEFERRED_OS_SLEEP, RESULT_NAME, ROOT_RECEIPT, SafetyPolicy, Sample, Sensor,
                               Telemetry, atomic_json, protection_errors, request_service)
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
PROTOCOL: Final = 3  # == input_lock_common.PROTOCOL_VERSION
# The unit has WatchdogSec=60s (P1-1): longer than one 20 s kernel freezer
# timeout, so a single freeze outside a sleep window is not fatal. Between
# PrepareForSleep(true) and the end of logind's PreparingForSleep the freezer
# may stop this process for 20 s per failed attempt, and one suspend can make
# several attempts, so the watchdog is extended for that time only (A3). It is
# never restored while logind still reports PreparingForSleep=true.
WATCHDOG_NORMAL_USEC: Final = 60_000_000
WATCHDOG_SLEEP_USEC: Final = 300_000_000
# A sleep window that never sees its resume signal is closed after a cap
# derived at runtime (P1-1): InhibitDelayMaxSec + 60 s + 3 x pm_freeze_timeout,
# clamped to [SLEEP_WINDOW_MIN_SECONDS, SLEEP_WINDOW_MAX_SECONDS] (150 s with
# logind's 30 s and the kernel's 20 s). CLOCK_MONOTONIC stops while the machine
# is suspended, so this is awake time spent preparing (or failing) to sleep.
SLEEP_WINDOW_MIN_SECONDS: Final = 150.0
SLEEP_WINDOW_MAX_SECONDS: Final = 300.0
SLEEP_WINDOW_MARGIN_SECONDS: Final = 60.0
SLEEP_WINDOW_FREEZE_ATTEMPTS: Final = 3
# Fallbacks when logind or the kernel value cannot be read.
INHIBIT_DELAY_MAX_FALLBACK_SECONDS: Final = 30.0
PM_FREEZE_TIMEOUT_FALLBACK_SECONDS: Final = 20.0
PM_FREEZE_TIMEOUT_PATH: Final = Path("/sys/power/pm_freeze_timeout")
# A PreparingForSleep=true reading counts as current for this long: longer
# than one freezer stop (20 s) plus the 2 s telemetry interval. Telemetry
# that stops altogether leaves the state unknown (P1-1).
LOGIND_PREPARING_FRESH_SECONDS: Final = 30.0
# Kernel suspend counters. They change after every completed or failed
# attempt, and one suspend may make several failing attempts while logind is
# still preparing, so a change is resume evidence only together with logind
# PreparingForSleep=false (S-R1, P1-1).
SUSPEND_STATS_DIR: Final = Path("/sys/power/suspend_stats")
# EVIOCGSW: the switch-state bitmap (P2-1). python3-evdev 1.9 has no
# InputDevice.switches(), so the daemon reads it with the ioctl itself.
_IOC_READ: Final = 2
SW_LID_BIT: Final = 0x00
SW_BITMAP_BYTES: Final = (0x10 + 1 + 7) // 8  # SW_CNT = SW_MAX + 1 = 0x11 bits
# Distinct telemetry samples with logind PreparingForSleep=false that close the
# window early (S-R1 resume evidence).
SLEEP_RESUME_EVIDENCE_SAMPLES: Final = 2
# The readiness probe runs on its own thread (S-R2). A result older than this
# is treated as "probe pending" for arming; it never affects supervision.
PROTECTION_PROBE_INTERVAL_SECONDS: Final = 30.0
PROTECTION_PROBE_STALE_SECONDS: Final = 150.0
# Consecutive logind D-Bus failures tolerated before acting as session loss (A6).
LOGIND_ERROR_GRACE_SECONDS: Final = 15.0
LOGIND_RESTART_BACKOFF: Final = (2.0, 5.0, 10.0, 30.0, 60.0)
# IPC lock requests are refused this long after a physical emergency unlock (L3).
EMERGENCY_LOCK_COOLDOWN_SECONDS: Final = 60.0
PHYSICAL_EMERGENCY_SOURCES: Final = frozenset(
    {"emergency-hotkey", "gnome-emergency-hotkey", "tty-recovery-hotkey"}
)
# Unsent responses a non-controller client may accumulate before it is dropped.
CLIENT_OUTPUT_LIMIT: Final = 1 << 20


def _ioc(direction: int, kind: str, number: int, size: int) -> int:
    """The kernel's _IOC() request encoding (asm-generic/ioctl.h)."""
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | number


def eviocgsw(length: int) -> int:
    """EVIOCGSW(len) = _IOC(_IOC_READ, 'E', 0x1b, len)."""
    return _ioc(_IOC_READ, "E", 0x1B, length)


def read_lid_switch(device: object) -> bool:
    """The SW_LID state of an evdev device, read with EVIOCGSW (P2-1).

    Raises OSError when the device does not answer and AttributeError when
    the object has no descriptor; callers treat either as "unreadable".
    """
    fd = device.fd
    buffer = bytearray(SW_BITMAP_BYTES)
    fcntl.ioctl(fd, eviocgsw(len(buffer)), buffer, True)
    return bool(buffer[SW_LID_BIT // 8] & (1 << (SW_LID_BIT % 8)))


def _read_seconds(path: Path, scale: float) -> float | None:
    """A positive number from a sysfs file, divided by ``scale``, or None."""
    try:
        value = float(path.read_text().strip()) / scale
    except (OSError, ValueError):
        return None
    return value if 0 < value < 86_400 else None


class _LogindEvent(str):
    """A control event tagged with the LogindMonitor generation that posted
    it, so a retired monitor's late refresh round is dropped (P3-2)."""

    logind_generation: int

    def __new__(cls, text: str, generation: int) -> "_LogindEvent":
        event = super().__new__(cls, text)
        event.logind_generation = generation
        return event


class TransitionError(RuntimeError):
    """A requested lock transition could not be completed transactionally."""


class RecoveryRequired(TransitionError):
    """The daemon must terminate so that systemd runs InputLock recovery.

    A subclass of TransitionError for compatibility, but never swallowed by a
    handler that refuses an ordinary transition: those re-raise it (S-R7).
    """


# Interface 3: the tray maps these keys (or reasons containing thermal,
# battery, telemetry, supervision or sleep) to plain text.
_PROTECTION_LID_KEYS: Final = {
    "CONTROLLER_LOST": "controller-disconnected",
    "SUPERVISION_LOST": "supervision-lost",
    "TELEMETRY_LOST": "telemetry-lost",
    "LID_TELEMETRY_LOST": "telemetry-lid-switch-lost",
    "LID_DEVICE_LOST": "telemetry-lid-switch-lost",
    "BATTERY_CRITICAL": "battery-critical",
    "THERMAL_HIGH_RISK": "thermal-high-risk",
    "THERMAL_EMERGENCY": "thermal-emergency",
    "DAEMON_FAILED": "daemon-shutdown",
}
_LID_DISABLE_KEYS: Final = {
    "emergency-hotkey": "emergency-unlock",
    "gnome-emergency-hotkey": "emergency-unlock",
    "tty-recovery-hotkey": "emergency-unlock",
    "ipc-emergency-unlock": "emergency-unlock",
    "authorized-session-inactive": "session-inactive",
    "user-session-ended": "session-inactive",
    "logind-unavailable": "supervision-logind-unavailable",
    "safety-journal-failed": "supervision-journal-unwritable",
    "hotkey-change": "settings-reset",
    "lock-kinds-change": "settings-reset",
    "allowed-keys-change": "settings-reset",
    "configuration-reload": "settings-reset",
    "invalid-config-reload": "settings-reset",
    "system-suspend": "sleep-input-release-failed",
    "system-sleep-hook": "sleep-input-release-failed",
    "controller-lost": "controller-disconnected",
    "lid-device-lost": "telemetry-lid-switch-lost",
}
# U-R1: the tray did not heartbeat within CONTROLLER_TIMEOUT. A closed or
# broken connection keeps "controller-disconnected".
CONTROLLER_LEASE_EXPIRED_KEY: Final = "controller-lease-expired"
# U-R5 (interface 2): lid-ignore ends because of something the user did. The
# tray shows an informational notice for these, a critical one otherwise.
# Settings reloads are user-initiated only when requested over IPC.
USER_INITIATED_LID_REASONS: Final = frozenset({
    "lid-ignore-disabled",
    "emergency-hotkey",
    "gnome-emergency-hotkey",
    "tty-recovery-hotkey",
    "ipc-emergency-unlock",
    "hotkey-change",
    "lock-kinds-change",
    "allowed-keys-change",
})


def lid_disable_key(reason: str) -> str:
    """Normalize an internal reason to the readable key reported to the tray."""
    text = reason.strip()
    if text.startswith("protective-suspend:"):
        text = text.partition(":")[2]
    if text in _PROTECTION_LID_KEYS:
        return _PROTECTION_LID_KEYS[text]
    text = text.lower().replace("_", "-")
    return _LID_DISABLE_KEYS.get(text, text)


def sd_notify(message: str, environ: dict[str, str] | None = None) -> bool:
    """Send one sd_notify(3) datagram without libsystemd.

    Supports filesystem and abstract-namespace ('@' prefix) sockets. Returns
    False when not supervised by systemd or when the send fails; callers never
    depend on delivery.
    """
    address = (os.environ if environ is None else environ).get("NOTIFY_SOCKET", "")
    if address.startswith("@"):
        address = "\0" + address[1:]
    elif not address.startswith("/"):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as notify:
            notify.sendto(message.encode("utf-8"), address)
        return True
    except OSError as exc:
        LOG.debug("sd_notify %r failed: %s", message, exc)
        return False


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
    pid: int = 0
    # Per-client output queue (B2). A slow reader is never treated as a lost
    # client: unsent bytes wait for EVENT_WRITE, and only the newest state
    # event is kept while older output is still pending.
    outbuf: bytearray = field(default_factory=bytearray)
    pending_state: bytes | None = None
    close_after_flush: bool = False
    watching_write: bool = False
    # Set while _close_client runs, so a failed final flush never re-enters it.
    closing: bool = False


def session_is_eligible(properties: dict[str, object]) -> bool:
    """Only an active local graphical user on a real seat may control locking."""
    return session_is_locked_local(properties, require_unlocked=True)


def session_is_locked_local(properties: dict[str, object], *, require_unlocked: bool = False) -> bool:
    """The active local graphical session of the user, optionally only if unlocked."""
    seat = properties.get("Seat", ())
    locked = bool(properties.get("LockedHint", False))
    return (
        bool(properties.get("Active")) and not bool(properties.get("Remote", True))
        and (not locked if require_unlocked else True)
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
    """Listen for suspend and authorized-session deactivation via system D-Bus.

    Signals arrive on a private connection with a GLib main loop and are only
    dispatched there; the synchronous refresh calls run on a separate thread
    and connection (S-R3). Each run uses fresh connections (S-R5).
    """

    REFRESH_SECONDS = 5.0

    def __init__(self, authorized_uid: int, callback: Callable[[str], None]) -> None:
        super().__init__(name="input-lock-logind", daemon=True)
        self.authorized_uid = authorized_uid
        self.callback = callback
        self._loop: object | None = None
        self._authorized_paths: set[str] = set()
        self._authorized_active_paths: set[str] = set()
        # logind session IDs of the user's local graphical sessions (active or
        # locked), or None before the first successful refresh (L6).
        self.graphical_session_ids: frozenset[str] | None = None
        self._session_error_reported = False
        self._power_error_reported = False
        # logind "sleep" delay inhibitor descriptor (S-R3).
        self._delay_lock = threading.Lock()
        self._delay_fd: int | None = None
        self._delay_error_reported = False
        self._sleeping = False
        # Monotonic time of the last PrepareForSleep(true) (P3-2).
        self._sleep_started_at: float | None = None
        # logind InhibitDelayMaxUSec in seconds, read by the refresh thread
        # for the daemon's sleep-window cap (P1-1); None until read.
        self.inhibit_delay_max_seconds: float | None = None
        self._refresh_wanted = threading.Event()
        self._stopping = threading.Event()

    def refresh_sessions_with(
        self,
        list_sessions: Callable[[], object],
        session_properties: Callable[[str], dict[str, object]],
    ) -> None:
        """Run one complete session refresh and report the result.

        A D-Bus failure is reported only as "logind-error"; it says nothing
        about the session. The daemon acts only after failures persist (A6).
        logind supervision counts as healthy whenever the calls succeed and
        the user has an active or a locked local graphical session (A5).
        """
        paths: set[str] = set()
        active_paths: set[str] = set()
        locked_paths: set[str] = set()
        graphical: set[str] = set()
        try:
            for row in list_sessions():
                if int(row[1]) != self.authorized_uid:
                    continue
                path = str(row[4])
                paths.add(path)
                values = session_properties(path)
                if session_is_eligible(values):
                    active_paths.add(path)
                elif session_is_locked_local(values):
                    locked_paths.add(path)
                else:
                    continue
                graphical.add(str(values.get("Id", row[0])))
        except Exception as exc:
            if not self._session_error_reported:
                LOG.warning("event=LOGIND_UNAVAILABLE error=%s", exc)
            self._session_error_reported = True
            self.callback("logind-error")
            return
        if self._session_error_reported:
            LOG.info("event=LOGIND_RESTORED")
        self._session_error_reported = False
        self._authorized_paths = paths
        self._authorized_active_paths = active_paths
        self.graphical_session_ids = frozenset(graphical)
        if active_paths or locked_paths:
            self.callback("logind-healthy")
        self.callback("session-active" if active_paths else
                      "session-locked" if locked_paths else "session-inactive")

    # ------------------------------------------------ sleep delay lock (S-R3)

    def acquire_delay_inhibitor(self, inhibit: Callable[[], object]) -> bool:
        """Hold a logind "sleep" inhibitor in mode "delay".

        logind then waits (at most InhibitDelayMaxSec, 5 s by default) until
        this monitor has extended the daemon's watchdog in its
        PrepareForSleep(true) handler and released the lock. Delay mode never
        blocks a suspend. Failure is logged and tolerated: the hook and the
        signal still extend the watchdog, only possibly later.
        """
        with self._delay_lock:
            if self._delay_fd is not None or self._sleeping or self._stopping.is_set():
                return self._delay_fd is not None
        try:
            descriptor = inhibit()
            fd = descriptor.take() if hasattr(descriptor, "take") else int(descriptor)
        except Exception as exc:
            if not self._delay_error_reported:
                LOG.warning("event=SLEEP_DELAY_INHIBITOR_UNAVAILABLE error=%s", exc)
            self._delay_error_reported = True
            return False
        with self._delay_lock:
            # A sleep (or a monitor stop) that began during the call must not
            # be delayed by a lock nobody would release.
            if self._delay_fd is None and not self._sleeping and not self._stopping.is_set():
                self._delay_fd, fd = fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            return False
        if self._delay_error_reported:
            LOG.info("event=SLEEP_DELAY_INHIBITOR_RESTORED")
        self._delay_error_reported = False
        return True

    def release_delay_inhibitor(self) -> None:
        with self._delay_lock:
            fd, self._delay_fd = self._delay_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def prepare_for_sleep(self, starting: object) -> None:
        """PrepareForSleep handler on the signal connection; never blocks."""
        if bool(starting):
            self._sleep_started_at = time.monotonic()
            self._sleeping = True
            # Extend the watchdog from this thread at once, before the main
            # loop runs and before the kernel freezer can stop the process,
            # and only then let logind proceed (S-R3).
            sd_notify("WATCHDOG=1")
            sd_notify(f"WATCHDOG_USEC={WATCHDOG_SLEEP_USEC}")
            self.release_delay_inhibitor()
            self.callback("prepare-sleep")
        else:
            self._sleeping = False
            self.callback("resume-sleep")
            # The refresh thread re-acquires the delay lock (off this path).
            self._refresh_wanted.set()

    def sleep_finished(self, observed_at: float) -> None:
        """The daemon saw logind PreparingForSleep=false at ``observed_at``
        (monotonic) after its sleep window closed (P3-2).

        If PrepareForSleep(false) was missed while this monitor kept running,
        _sleeping would stay True and the delay inhibitor would never be
        re-acquired. A reading taken before the latest PrepareForSleep(true)
        says nothing about that newer sleep and is ignored.
        """
        started = self._sleep_started_at
        if started is not None and observed_at <= started:
            return
        if self._sleeping:
            LOG.info("event=SLEEP_DELAY_INHIBITOR_REARM reason=resume-signal-missed")
        self._sleeping = False
        self._refresh_wanted.set()

    def refresh_inhibit_delay_with(self, api: object) -> None:
        """Remember logind's InhibitDelayMaxUSec for the window cap (P1-1)."""
        read = getattr(api, "inhibit_delay_max_usec", None)
        if not callable(read):
            return
        try:
            seconds = int(read()) / 1_000_000
        except Exception:
            return
        if 0 <= seconds <= 3600:
            self.inhibit_delay_max_seconds = seconds

    def request_refresh(self, *_args: object, **_kwargs: object) -> None:
        """Signal handlers only ask for a refresh; the calls run elsewhere."""
        self._refresh_wanted.set()

    def properties_changed(
        self,
        interface: object,
        changed: object = None,
        *signal_args: object,
        path: object | None = None,
        **_signal_kwargs: object,
    ) -> None:
        # dbus-python has emitted both two- and three-positional-argument
        # variants of PropertiesChanged in supported Ubuntu releases. The
        # invalidated-properties array is irrelevant here, so accept either
        # shape instead of allowing a callback TypeError to stop monitoring.
        del changed, signal_args
        interface_text = str(interface)
        if interface_text in {"org.freedesktop.UPower", "org.freedesktop.UPower.Device"}:
            self.request_refresh()
        elif interface_text == "org.freedesktop.login1.Session" and str(path or "") in self._authorized_paths:
            self.request_refresh()

    # ------------------------------------------- refresh thread (S-R3, S-R5)

    def refresh_power_with(self, api: object) -> bool:
        try:
            values = api.power_state()
            if self._power_error_reported:
                LOG.info("event=UPOWER_RESTORED")
            self._power_error_reported = False
            self.callback("power-state:" + json.dumps(values))
            return True
        except Exception as exc:
            self.callback("power-unavailable")
            if not self._power_error_reported:
                LOG.warning("event=UPOWER_UNAVAILABLE error=%s", exc)
            self._power_error_reported = True
            return False

    def refresh_loop(self, connect: Callable[[], object]) -> None:
        """Periodic and on-demand refresh on its own thread and connection.

        ListSessions, GetAll per session and the UPower calls are synchronous
        (up to 3 s each). They run here, on a private connection without a
        main loop, so a PrepareForSleep signal on the monitor's own connection
        is never queued behind them. A failing connection is closed and
        reopened on the next cycle (after a dbus-broker restart, for example).
        """
        api = None
        while not self._stopping.is_set():
            if api is None:
                try:
                    api = connect()
                except Exception as exc:
                    if not self._session_error_reported:
                        LOG.warning("event=LOGIND_UNAVAILABLE error=%s", exc)
                    self._session_error_reported = True
                    self.callback("logind-error")
                    self.callback("power-unavailable")
            if api is not None:
                self.acquire_delay_inhibitor(api.inhibit_sleep_delay)
                if self.inhibit_delay_max_seconds is None:
                    self.refresh_inhibit_delay_with(api)
                self.refresh_sessions_with(api.list_sessions, api.session_properties)
                power_ok = self.refresh_power_with(api)
                if self._session_error_reported or not power_ok:
                    _close_quietly(api)
                    api = None
            self._refresh_wanted.wait(self.REFRESH_SECONDS)
            self._refresh_wanted.clear()
        if api is not None:
            _close_quietly(api)

    def stop(self) -> None:
        self._stopping.set()
        self._refresh_wanted.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass

    def run(self) -> None:
        bus = None
        worker = None
        try:
            import dbus
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib

            loop_adapter = DBusGMainLoop(set_as_default=False)
            # Each monitor run has its own private connection (S-R5). The
            # process-shared SystemBus() would survive a dbus-broker restart
            # as a dead connection and be handed to every later run.
            bus = dbus.bus.BusConnection(dbus.bus.BUS_SYSTEM, mainloop=loop_adapter)
            set_exit = getattr(bus, "set_exit_on_disconnect", None)
            if callable(set_exit):
                set_exit(False)
            self._loop = GLib.MainLoop()

            def disconnected(*_args: object) -> None:
                LOG.warning("event=LOGIND_BUS_DISCONNECTED")
                self.stop()

            bus.call_on_disconnection(disconnected)
            bus.add_signal_receiver(
                self.prepare_for_sleep,
                signal_name="PrepareForSleep",
                dbus_interface="org.freedesktop.login1.Manager",
            )
            bus.add_signal_receiver(
                self.properties_changed,
                signal_name="PropertiesChanged",
                dbus_interface="org.freedesktop.DBus.Properties",
                path_keyword="path",
            )
            for name in ("SessionNew", "SessionRemoved"):
                bus.add_signal_receiver(
                    self.request_refresh,
                    signal_name=name,
                    dbus_interface="org.freedesktop.login1.Manager",
                )
            worker = threading.Thread(
                target=self.refresh_loop, args=(lambda: _LogindBusApi(dbus),),
                name="input-lock-logind-refresh", daemon=True,
            )
            worker.start()
            if not self._stopping.is_set():
                self._loop.run()
            LOG.warning("event=LOGIND_MONITOR_STOPPED")
        except Exception as exc:
            LOG.warning("event=LOGIND_MONITOR_STOPPED error=%s; sleep hook remains active", exc)
        finally:
            self.stop()
            self.release_delay_inhibitor()
            if worker is not None:
                worker.join(1.0)
            if bus is not None:
                _close_quietly(bus)
        # The daemon restarts the monitor with backoff and treats only a
        # sustained outage as session loss.
        self.callback("logind-monitor-stopped")


def _close_quietly(resource: object) -> None:
    try:
        resource.close()
    except Exception:
        pass


class _LogindBusApi:
    """Synchronous logind/UPower calls on one private connection (no main loop)."""

    def __init__(self, dbus: object) -> None:
        self._dbus = dbus
        self.bus = dbus.bus.BusConnection(dbus.bus.BUS_SYSTEM)
        try:
            login = self.bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1", introspect=False)
            self.manager = dbus.Interface(login, "org.freedesktop.login1.Manager")
            upower = self.bus.get_object("org.freedesktop.UPower", "/org/freedesktop/UPower", introspect=False)
            self.upower_properties = dbus.Interface(upower, "org.freedesktop.DBus.Properties")
            self.upower = dbus.Interface(upower, "org.freedesktop.UPower")
            battery = self.bus.get_object(
                "org.freedesktop.UPower", "/org/freedesktop/UPower/devices/DisplayDevice", introspect=False)
            self.battery_properties = dbus.Interface(battery, "org.freedesktop.DBus.Properties")
        except Exception:
            _close_quietly(self.bus)
            raise

    def list_sessions(self) -> object:
        return self.manager.ListSessions(timeout=3)

    def session_properties(self, path: str) -> dict[str, object]:
        properties = self._dbus.Interface(
            self.bus.get_object("org.freedesktop.login1", path, introspect=False),
            "org.freedesktop.DBus.Properties",
        )
        return dict(properties.GetAll("org.freedesktop.login1.Session", timeout=3))

    def power_state(self) -> dict[str, object]:
        values = dict(self.upower_properties.GetAll("org.freedesktop.UPower", timeout=3))
        battery = dict(self.battery_properties.GetAll("org.freedesktop.UPower.Device", timeout=3))
        return {
            "on_battery": bool(values["OnBattery"]),
            "lid_closed": bool(values["LidIsClosed"]),
            "percentage": float(battery["Percentage"]),
            "warning_level": int(battery["WarningLevel"]),
            "critical_action": str(self.upower.GetCriticalAction(timeout=3)),
        }

    def inhibit_delay_max_usec(self) -> int:
        properties = self._dbus.Interface(
            self.bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1", introspect=False),
            "org.freedesktop.DBus.Properties",
        )
        return int(properties.Get("org.freedesktop.login1.Manager", "InhibitDelayMaxUSec", timeout=3))

    def inhibit_sleep_delay(self) -> object:
        return self.manager.Inhibit(
            "sleep", "Input Lock",
            "Extend the service watchdog before the system sleeps", "delay", timeout=3,
        )

    def close(self) -> None:
        self.bus.close()


class InputLockDaemon:
    # Scalar defaults for state added in the 2026-10-22 overhaul. They are
    # class attributes so partially constructed fixtures keep working.
    logind_monitor: LogindMonitor | None = None
    authorized_session_locked = False
    # Sleep window (A3/A4/A10): monotonic start of PrepareForSleep(true), or None.
    sleep_window_since: float | None = None
    # End of the post-resume grace period (resume + CONTROLLER_TIMEOUT).
    sleep_grace_until: float | None = None
    # Protection reason that arrived while a system sleep was in progress.
    deferred_protection: str | None = None
    # Consecutive logind failures (A6) and monitor restarts.
    logind_error_since: float | None = None
    logind_error_handled = False
    logind_restart_at: float | None = None
    logind_restart_attempts = 0
    # IPC lock requests are refused until this monotonic time (L3).
    lock_cooldown_until = 0.0
    # Last time lid-ignore was turned off, for the tray (A8).
    last_lid_disable: dict[str, object] | None = None
    safety_write_failed = False
    # Kernel suspend counters when the sleep window opened, and the sample
    # times that showed logind no longer preparing a sleep (S-R1).
    sleep_stats_at_open: tuple[int, int] | None = None
    sleep_not_preparing: tuple[float, ...] = ()
    # Cap of the current sleep window, fixed when it opens (P1-1).
    sleep_window_cap: float = SLEEP_WINDOW_MIN_SECONDS
    # Latest known logind PreparingForSleep (True/False/None = unknown) and
    # the monotonic time it was observed: from PrepareForSleep signals, the
    # sleep hook and telemetry samples, whichever is newest (P1-1).
    logind_preparing: bool | None = None
    logind_preparing_at = float("-inf")
    # Monotonic time the watchdog was extended for a sleep, until restored
    # (P1-1). It outlives the window while logind is still preparing.
    watchdog_extended_since: float | None = None
    watchdog_extended_cap: float = SLEEP_WINDOW_MIN_SECONDS
    # The window closed but the monitor has not yet been told that logind
    # stopped preparing (P3-2).
    sleep_monitor_reset_pending = False
    # Generation of the live LogindMonitor; events of older ones are dropped.
    logind_generation = 0
    # Latest readiness-probe result, written by its own thread (S-R2).
    protection_probe_errors: tuple[str, ...] = ("native protection probe pending",)
    protection_probe_at: float | None = None
    # A pending reload was requested over IPC by the user (U-R5).
    reload_user_initiated = False
    # Incremented by every state event, so a closing controller gets a final
    # event only when none was just emitted (U-R1).
    state_event_seq = 0

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
        self.lid_policy_healthy = False
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
        # Type=notify (B1): the socket exists, devices are known and the state
        # file is written, so CLI clients started after this can connect.
        sd_notify("READY=1")
        self._start_logind_monitor()
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
                for key, mask in self.selector.select(timeout):
                    category, identifier = key.data
                    if category == "server":
                        self._accept_clients()
                    elif category == "client":
                        if mask & selectors.EVENT_WRITE:
                            self._flush_client(int(identifier))
                        if mask & selectors.EVENT_READ:
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

    def _write_safety(self, *, force: bool = False, raise_errors: bool = False) -> None:
        """Write the safety journal (breadcrumb).

        A write failure (full or read-only /var/lib) is not fatal (B3): it is
        logged once, an armed lid-ignore is disarmed because its evidence can
        no longer be recorded, and the daemon keeps running. Arming passes
        raise_errors=True so it can refuse instead.
        """
        snapshot = self.safety.snapshot()
        InputLockDaemon._note_thermal_measurement(self, snapshot)
        # The thermal measurement is deliberately not part of the signature:
        # every flap would otherwise force an fsync on the main loop (B3).
        signature = (self.safety.state, self.safety.reason, self.safety.thermal_level, self.safety.armed,
                     self.ignore_lid_close, self._saved_lid_ignore_close, str(self.protective_result))
        now = time.monotonic()
        if not force and signature == self.safety_signature and now - self.safety_last_write < 30:
            return
        try:
            atomic_json(BREADCRUMB, {
                "schema": 1, "version": VERSION, "boot_id": self.boot_id,
                "invocation_id": os.environ.get("INVOCATION_ID"),
                "daemon_started_at": self.daemon_started_at, "updated_at": time.time(),
                "requested": self._saved_lid_ignore_close, "effective": self.ignore_lid_close,
                "generation": self.lid_generation, "clean_shutdown": self.clean_shutdown,
                "restrictions_released": (self.lid_inhibitor_fd is None
                                          and not any(r.grabbed for r in self.devices.values())),
                "safety": snapshot, "protective_result": self.protective_result,
                "previous_run": self.safety_previous_run,
                "events": self.safety.events[-24:],
            })
        except OSError as exc:
            if raise_errors:
                raise
            if not getattr(self, "safety_write_failed", False):
                LOG.error("event=SAFETY_JOURNAL_WRITE_FAILED path=%s error=%s", BREADCRUMB, exc)
            self.safety_write_failed = True
            # Retry on the next change, or after 30 s, not on every tick.
            self.safety_signature, self.safety_last_write = signature, now
            if self.safety.armed or self.ignore_lid_close:
                self._disable_lid("safety-journal-failed")
            return
        if getattr(self, "safety_write_failed", False):
            LOG.info("event=SAFETY_JOURNAL_RESTORED")
            self.safety_write_failed = False
        self.safety.events = self.safety.events[-24:]
        self.safety_signature, self.safety_last_write = signature, now

    def _note_thermal_measurement(self, snapshot: dict) -> None:
        """Log THERMAL_MEASUREMENT_CHANGED for transitions only (G1).

        A new condition is logged once it has been seen in at least two
        distinct samples (or for 5 s when samples stop arriving), so a single
        noisy reading does not produce a log line. While disarmed the line is
        DEBUG. A change of the armed state is logged at once.
        """
        measured = snapshot["thermal_measurement"]
        signature = (measured["state"], measured["fresh"], measured["complete"], self.safety.armed)
        logged = getattr(self, "thermal_measurement_signature", None)
        if signature == logged:
            self.thermal_log_candidate = None
            return
        if logged is not None and logged[3] == signature[3]:
            sample = self.safety.sample
            sample_at = sample.at if sample is not None else None
            now = time.monotonic()
            candidate = getattr(self, "thermal_log_candidate", None)
            if candidate is None or candidate[0] != signature:
                self.thermal_log_candidate = (signature, {sample_at}, now)
                return
            candidate[1].add(sample_at)
            if len(candidate[1]) < 2 and now - candidate[2] < 5:
                return
        self.thermal_log_candidate = None
        LOG.log(logging.INFO if self.safety.armed else logging.DEBUG,
                "event=THERMAL_MEASUREMENT_CHANGED condition=%s protection=%s fresh=%s complete=%s",
                measured["state"], snapshot["thermal_protection"], measured["fresh"], measured["complete"])
        self.thermal_measurement_signature = signature

    def _probe_protection(self) -> None:
        """Readiness probe loop on its own thread (S-R2).

        protection_errors() talks to systemd and logind and can take many
        seconds on a sluggish bus. Its result is cached here; telemetry
        sampling never waits for it. The probe gates arming only.
        """
        while not self.safety_stop.is_set():
            try:
                errors = tuple(protection_errors())
            except Exception as exc:  # protection_errors() normally never raises
                errors = ("native protection probe failed: " + str(exc),)
            self.protection_probe_errors, self.protection_probe_at = errors, time.monotonic()
            self.safety_stop.wait(PROTECTION_PROBE_INTERVAL_SECONDS)

    def _cached_protection_errors(self) -> list[str]:
        at = self.protection_probe_at
        if at is None:
            return ["native protection probe pending"]
        if time.monotonic() - at > PROTECTION_PROBE_STALE_SECONDS:
            return ["native protection probe has not answered recently"]
        return list(self.protection_probe_errors)

    def _sample_safety(self) -> None:
        telemetry = Telemetry()
        threading.Thread(target=self._probe_protection, name="input-lock-readiness", daemon=True).start()
        telemetry_failed = False
        while not self.safety_stop.is_set():
            errors = self._cached_protection_errors()
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

    def _read_protective_receipt(self) -> dict | None:
        """This boot's receipt for the pending protective generation, or None.

        The helper writes the authoritative receipt into the root-owned
        /var/lib/input-lock-protection first and a copy next to the daemon
        journal (the pre-overhaul location). Read the root-owned one first and
        fall back to the copy, e.g. when the root write failed or an older
        helper is still installed. Raises ValueError when a present receipt
        is unreadable and no usable one was found.
        """
        unreadable = None
        for path in (ROOT_RECEIPT, BREADCRUMB.with_name(RESULT_NAME)):
            try:
                receipt = json.loads(path.read_text())
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                unreadable = exc
                continue
            if not isinstance(receipt, dict):
                unreadable = ValueError("invalid protective receipt")
                continue
            if (receipt.get("boot_id") == self.boot_id
                    and receipt.get("generation") == self.protective_result.get("generation")):
                return receipt
        if unreadable is not None:
            raise ValueError(f"invalid protective receipt: {unreadable}")
        return None

    def _safety_tick(self) -> None:
        now = time.monotonic()
        self.next_safety_tick = now + 1
        if self.protective_result is not None:
            try:
                receipt = self._read_protective_receipt()
                self.safety_receipt_error = False
                if receipt is not None and receipt.get("result") != self.protective_result.get("result"):
                    self.protective_result.update(receipt)
                    if receipt["result"] == "suspend-transition-confirmed" and receipt.get("transition_confirmed") is True and not self.ignore_lid_close:
                        self.safety.transition("PROTECTIVE_ACTION_EXECUTED", "PROTECTIVE_ACTION_SUCCEEDED")
                    elif receipt["result"] == "protection-failed" and not self.ignore_lid_close:
                        self.safety.transition("PROTECTIVE_ACTION_FAILED", "OPEN_LID_OPERATOR_RECOVERY_REQUIRED")
                    elif receipt["result"].startswith("cancelled") and not self.ignore_lid_close:
                        self.safety.release()
                    elif ((receipt["result"] == DEFERRED_OS_SLEEP or receipt.get("transition_status") == "DEFERRED")
                          and not self.ignore_lid_close):
                        # The helper found the OS sleep path busy (or it just
                        # failed) and requested nothing. Like a cancellation,
                        # this ends the protective decision: lid-ignore is
                        # already off, so logind's own lid policy owns the
                        # closed lid. Supervision resumes normally once any
                        # sleep window and its grace end; lid-ignore is never
                        # re-armed automatically.
                        LOG.warning("event=PROTECTIVE_ACTION_DEFERRED reason=%s detail=%s",
                                    self.protective_result.get("reason"), receipt.get("detail", ""))
                        self.deferred_protection = None
                        self.safety.release("PROTECTIVE_ACTION_DEFERRED")
                    self._write_safety(force=True)
            except (OSError, ValueError, TypeError, KeyError):
                if not self.safety_receipt_error:
                    LOG.warning("event=PROTECTIVE_RESULT_UNAVAILABLE")
                self.safety_receipt_error = True
        sample = self.safety_sample
        if self.sleep_window_since is not None:
            evidence = self._sleep_resume_evidence()
            if evidence:
                self._leave_sleep_window("resume-evidence:" + evidence)
        else:
            self._observe_sample_preparing()
            self._after_sleep_window()
        # While logind prepares a system sleep, the OS is already suspending.
        # The policy is still updated in the window (S-R1): it gathers
        # evidence but takes no action while it knows a sleep is being
        # prepared (bounded by its own 300 s cap), and _start_protection()
        # defers anything that does come up until the window closes (A10).
        sleeping = self.sleep_window_since is not None
        if sample is not None:
            sample.dependencies_ok = now - self.last_power_at <= 12 and now - self.last_logind_at <= 12
            sample.warning_level = self.upower_warning
            if now - sample.at > 12:
                sample.lid_closed = None
            reason = self.safety.update(sample)
            if reason:
                self._start_protection(reason)
        elif self.ignore_lid_close and not sleeping:
            self._start_protection("TELEMETRY_LOST")
        self._write_safety()
        self._write_state()
        if now - self.last_safety_emit >= 5:
            self.last_safety_emit = now
            self._emit_state_event("safety-status")
        # A wedged main event loop loses its watchdog. InputLock recovery stops
        # the daemon before one final ordinary suspend attempt.
        sd_notify("WATCHDOG=1")

    def _lid_may_be_closed(self) -> bool:
        return self.lid_is_closed or self.safety_sample is None or self.safety_sample.lid_closed is not False

    def _start_protection(self, reason: str, *, lid_reason: str | None = None) -> None:
        """Release everything and ask the root helper for one ordinary suspend.

        ``lid_reason`` overrides the key reported in last_lid_disable (for
        example "controller-lease-expired" for CONTROLLER_LOST, U-R1).
        """
        if self.sleep_window_since is not None:
            # logind is already preparing a sleep (A10). A protective suspend
            # now would be refused as "already in progress" and latch the
            # failsafe. Re-evaluate once the sleep window and its grace end.
            if self.deferred_protection is None:
                self.deferred_protection = reason
            LOG.warning("event=PROTECTION_DEFERRED reason=%s detail=system-sleep-in-progress", reason)
            return
        # SafetyPolicy.update() already made the decision and recorded the
        # original evidence. Do not overwrite it or log a second decision.
        if self.safety.armed or self.safety.state != "PROTECTIVE_ACTION_PENDING" or self.safety.reason != reason:
            self.safety.decide(reason)
        self._emergency_unlock("safety-protection", preserve_safety=True,
                               lid_reason=lid_reason or "protective-suspend:" + reason,
                               user_initiated=False)
        if any(r.grabbed for r in self.devices.values()):
            raise RecoveryRequired("device release failed; terminating for InputLock recovery")
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
        sd_notify("STOPPING=1")
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

    # ---------------------------- sleep window ----------------------------

    def _enter_sleep_window(self, source: str) -> None:
        """PrepareForSleep(true) or the system-sleep 'pre' hook (A3, A4, A10).

        The kernel freezer may stop this process (20 s per failed attempt) and
        systemd-sleep may freeze user.slice, and with it the tray, for longer.
        Extend the systemd watchdog and pause controller/lease deadlines until
        the resume signal plus CONTROLLER_TIMEOUT. Idempotent.
        """
        now = time.monotonic()
        # Both sources mean logind is preparing a sleep right now.
        self._note_logind_preparing(True, now)
        sd_notify("WATCHDOG=1")
        sd_notify(f"WATCHDOG_USEC={WATCHDOG_SLEEP_USEC}")
        if self.sleep_window_since is None:
            self.sleep_window_since = now
            self.sleep_window_cap = self._sleep_window_cap()
            self.sleep_stats_at_open = self._suspend_stats()
            self.sleep_not_preparing = ()
            LOG.info("event=SLEEP_WINDOW_OPENED source=%s watchdog_usec=%d cap=%.0fs",
                     source, WATCHDOG_SLEEP_USEC, self.sleep_window_cap)
        if self.watchdog_extended_since is None:
            self.watchdog_extended_since = now
        self.watchdog_extended_cap = self.sleep_window_cap
        self.sleep_monitor_reset_pending = False
        self.sleep_grace_until = None
        self._note_safety_sleep_window(True)

    def _sleep_window_cap(self) -> float:
        """InhibitDelayMax + 60 s + 3 x pm_freeze_timeout, clamped (P1-1).

        A failing suspend can make several freeze attempts (20 s each by
        default) after logind's delay, all inside one PrepareForSleep window.
        """
        inhibit = getattr(self.logind_monitor, "inhibit_delay_max_seconds", None)
        if not isinstance(inhibit, (int, float)) or isinstance(inhibit, bool) or not 0 <= inhibit <= 3600:
            inhibit = INHIBIT_DELAY_MAX_FALLBACK_SECONDS
        freeze = _read_seconds(PM_FREEZE_TIMEOUT_PATH, 1000.0)
        if freeze is None:
            freeze = PM_FREEZE_TIMEOUT_FALLBACK_SECONDS
        cap = inhibit + SLEEP_WINDOW_MARGIN_SECONDS + SLEEP_WINDOW_FREEZE_ATTEMPTS * freeze
        return min(SLEEP_WINDOW_MAX_SECONDS, max(SLEEP_WINDOW_MIN_SECONDS, cap))

    def _note_logind_preparing(self, value: bool | None, at: float, *, strict: bool = False) -> None:
        """Record logind PreparingForSleep as observed at ``at``; an older
        observation never overrides a newer one (P1-1). Signals and the hook
        are processed in order, so one at the same instant wins; a telemetry
        sample must be strictly newer (``strict``)."""
        if at > self.logind_preparing_at or (not strict and at == self.logind_preparing_at):
            self.logind_preparing = value
            self.logind_preparing_at = at

    def _observe_sample_preparing(self) -> None:
        sample = self.safety_sample
        if sample is not None:
            self._note_logind_preparing(sample.preparing, sample.at, strict=True)

    def _logind_stopped_preparing_since(self, since: float) -> bool:
        """logind's latest reported PreparingForSleep is false, observed no
        earlier than ``since``."""
        return self.logind_preparing is False and self.logind_preparing_at >= since

    @staticmethod
    def _suspend_stats() -> tuple[int, int] | None:
        """The kernel's (success, fail) suspend counters, or None if unreadable."""
        try:
            return (int((SUSPEND_STATS_DIR / "success").read_text().strip()),
                    int((SUSPEND_STATS_DIR / "fail").read_text().strip()))
        except (OSError, ValueError):
            return None

    def _sleep_resume_evidence(self) -> str | None:
        """Evidence that the machine is awake again although no resume signal
        arrived (S-R1): logind reported PreparingForSleep=false in
        SLEEP_RESUME_EVIDENCE_SAMPLES consecutive distinct samples taken after
        the window opened, or a kernel suspend counter changed and logind's
        latest reading is PreparingForSleep=false.

        The counters change after each failed freeze attempt, and one suspend
        can make several attempts while logind is still preparing (P1-1), so
        a counter change alone is never resume evidence.
        """
        since = self.sleep_window_since
        if since is None:
            return None
        self._observe_sample_preparing()
        sample = self.safety_sample
        if sample is not None and sample.at > since:
            if sample.preparing is True:
                self.sleep_not_preparing = ()
            elif sample.preparing is False and sample.at not in self.sleep_not_preparing:
                self.sleep_not_preparing = (*self.sleep_not_preparing, sample.at)
        opened = self.sleep_stats_at_open
        if opened is not None and self._logind_stopped_preparing_since(since):
            current = self._suspend_stats()
            if current is not None and current != opened:
                return "suspend-stats"
        if len(self.sleep_not_preparing) >= SLEEP_RESUME_EVIDENCE_SAMPLES:
            return "logind-not-preparing"
        return None

    def _after_sleep_window(self) -> None:
        """Finish a closed sleep window once logind has stopped preparing.

        The watchdog stays extended while logind reports PreparingForSleep=
        true, however the window closed: a failing suspend's next freeze
        attempt would otherwise kill the daemon (P1-1). With PreparingForSleep
        unknown (no reading, or the last "true" is older than
        LOGIND_PREPARING_FRESH_SECONDS because telemetry stopped) it stays
        extended until the window cap. Only a reading of false also tells the
        logind monitor that the sleep is over, so it re-acquires its delay
        inhibitor if PrepareForSleep(false) was missed (P3-2).
        """
        if self.sleep_window_since is not None:
            return
        now = time.monotonic()
        since = self.watchdog_extended_since
        stopped = since is not None and self._logind_stopped_preparing_since(since)
        if since is not None:
            expired = now - since >= self.watchdog_extended_cap
            preparing = (self.logind_preparing is True
                         and now - self.logind_preparing_at <= LOGIND_PREPARING_FRESH_SECONDS)
            if stopped or (expired and not preparing):
                self.watchdog_extended_since = None
                sd_notify(f"WATCHDOG_USEC={WATCHDOG_NORMAL_USEC}")
                sd_notify("WATCHDOG=1")
                LOG.info("event=WATCHDOG_RESTORED watchdog_usec=%d reason=%s", WATCHDOG_NORMAL_USEC,
                         "logind-not-preparing" if stopped else "cap-preparing-unknown")
        if self.sleep_monitor_reset_pending and self.logind_preparing is False:
            self.sleep_monitor_reset_pending = False
            finished = getattr(self.logind_monitor, "sleep_finished", None)
            if callable(finished):
                finished(self.logind_preparing_at)

    def _note_safety_sleep_window(self, active: bool) -> None:
        """Tell the safety policy that an OS sleep is (no longer) in progress.

        SafetyPolicy.note_sleep_window() is idempotent and bounded in time on
        its side; it only marks the window, so supervision takes no action
        while it is open and the status reports os_sleep_in_progress. The
        daemon keeps calling SafetyPolicy.update() in its window, so evidence
        is still collected (S-R1).
        """
        note = getattr(self.safety, "note_sleep_window", None)
        if callable(note):
            note(active)

    def _leave_sleep_window(self, source: str) -> None:
        """PrepareForSleep(false), the 'post' hook, resume evidence or the
        window cap. Supervision resumes now; the watchdog is restored only
        once logind has stopped preparing (_after_sleep_window, P1-1)."""
        self._note_safety_sleep_window(False)
        if self.sleep_window_since is None:
            self._after_sleep_window()
            return
        now = time.monotonic()
        self.sleep_window_since = None
        self.sleep_stats_at_open = None
        self.sleep_not_preparing = ()
        self.sleep_monitor_reset_pending = True
        self.sleep_grace_until = now + CONTROLLER_TIMEOUT
        for client in self.clients.values():
            if client.subscribed:
                client.deadline = max(client.deadline, self.sleep_grace_until)
        if self.lid_lease_deadline is not None:
            self.lid_lease_deadline = max(self.lid_lease_deadline, self.sleep_grace_until)
        LOG.info("event=SLEEP_WINDOW_CLOSED source=%s grace=%.0fs logind_preparing=%s",
                 source, CONTROLLER_TIMEOUT, self.logind_preparing)
        self._after_sleep_window()
        if self.deferred_protection is not None and self.safety.state == "PROTECTIVE_ACTION_PENDING":
            # The policy itself already decided (and disarmed); nothing else
            # would act on that decision, so do not wait for the grace end.
            self._recheck_deferred_protection()

    def _lid_switch_readable(self) -> bool:
        """At least one lid-switch device still answers EVIOCGSW (S-R8b).

        Read with the ioctl directly (P2-1): python3-evdev 1.9 has no
        InputDevice.switches(). Any failure counts as unreadable.
        """
        for record in self.devices.values():
            if "lid-switch" not in record.kinds:
                continue
            try:
                read_lid_switch(record.device)
                return True
            except (OSError, AttributeError, TypeError, ValueError):
                continue
        return False

    def _recheck_deferred_protection(self) -> None:
        """At the end of the post-resume grace, act on a deferred reason only
        if its cause still holds; thermal/telemetry causes are left to the
        safety policy, which resumed supervising when the window closed,
        unless the policy had already decided on them."""
        reason, self.deferred_protection = self.deferred_protection, None
        if reason is None or not self.ignore_lid_close:
            return
        if self.safety.state == "PROTECTIVE_ACTION_PENDING":
            # The policy decided (possibly on a later reason than the first
            # deferred one) and disarmed itself: act on its decision.
            reason, persists = self.safety.reason, True
        else:
            persists = {
                "CONTROLLER_LOST": self.controller_fd not in self.clients,
                "SUPERVISION_LOST": not (self.authorized_session_active or self.authorized_session_locked),
                "LID_DEVICE_LOST": not any("lid-switch" in r.kinds for r in self.devices.values()),
                "LID_TELEMETRY_LOST": not self._lid_switch_readable(),
            }.get(reason, False)
        if not persists:
            LOG.info("event=DEFERRED_PROTECTION_RESOLVED reason=%s", reason)
            return
        if self._lid_may_be_closed():
            self._start_protection(reason)
        else:
            self._disable_lid(lid_disable_key(reason))

    # ------------------------------ logind -------------------------------

    def _start_logind_monitor(self) -> None:
        # Each monitor run tags its events with its own generation, so a
        # retired monitor's late refresh round is dropped (P3-2).
        self.logind_generation += 1
        generation = self.logind_generation

        def post(event: str) -> None:
            self._post_control(_LogindEvent(event, generation))

        self.logind_monitor = LogindMonitor(self.config.authorized_uid, post)
        self.logind_monitor.start()

    def _logind_succeeded(self) -> None:
        if self.logind_error_since is not None and self.logind_error_handled:
            LOG.info("event=LOGIND_SUPERVISION_RESTORED")
        self.logind_error_since = None
        self.logind_error_handled = False
        self.logind_restart_attempts = 0

    def _on_session_lost(self, disable_reason: str) -> None:
        """The authorized user has no active or locked local session (or logind
        has failed for LOGIND_ERROR_GRACE_SECONDS)."""
        was_active = self.authorized_session_active
        self.authorized_session_active = False
        self.authorized_session_locked = False
        if self.ignore_lid_close and self._lid_may_be_closed():
            self._start_protection("SUPERVISION_LOST")
        elif self.ignore_lid_close:
            self._disable_lid(disable_reason)
        if self.config.unlock_on_session_switch and was_active:
            self._emergency_unlock("authorized-session-inactive", preserve_safety=self.safety.state == "PROTECTIVE_ACTION_PENDING")

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
            generation = getattr(event, "logind_generation", None)
            if generation is not None and generation != self.logind_generation:
                # A retired monitor's refresh worker (joined for only 1 s)
                # finished one more round after the restart (P3-2).
                LOG.debug("event=LOGIND_STALE_EVENT_DROPPED generation=%s", generation)
                continue
            if event == "prepare-sleep":
                self._enter_sleep_window("logind")
                if self.config.unlock_on_suspend:
                    self._release_inputs_keep_lid("system-suspend")
            elif event == "resume-sleep":
                # PrepareForSleep(false): logind has stopped preparing.
                self._note_logind_preparing(False, time.monotonic())
                self._leave_sleep_window("logind")
            elif event == "session-active":
                self._logind_succeeded()
                if not self.authorized_session_active:
                    self.last_input_activity = time.monotonic()
                self.authorized_session_active = True
                self.authorized_session_locked = False
                self._write_state()
            elif event == "session-locked":
                # The user's own local session with the screen locked (GNOME
                # also locks on every suspend). New locks/lid changes still need
                # an unlocked session, but continuous lid-ignore is kept.
                self._logind_succeeded()
                was_active = self.authorized_session_active
                self.authorized_session_active = False
                self.authorized_session_locked = True
                if self.config.unlock_on_session_switch and was_active:
                    self._release_inputs_keep_lid(
                        "authorized-session-locked",
                        preserve_safety=self.safety.state == "PROTECTIVE_ACTION_PENDING")
            elif event == "session-inactive":
                self._logind_succeeded()
                self._on_session_lost("session-inactive")
            elif event == "logind-healthy":
                self._logind_succeeded()
                self.last_logind_at = time.monotonic()
            elif event == "logind-error":
                # One failed or timed-out D-Bus call says nothing about the
                # session. Act only if failures persist (see _handle_timers).
                if self.logind_error_since is None:
                    self.logind_error_since = time.monotonic()
            elif event == "logind-monitor-stopped":
                # Retire the stopped monitor: anything its worker posts from
                # now on belongs to an older generation (P3-2).
                self.logind_generation += 1
                now = time.monotonic()
                if self.logind_error_since is None:
                    self.logind_error_since = now
                delay = LOGIND_RESTART_BACKOFF[min(self.logind_restart_attempts, len(LOGIND_RESTART_BACKOFF) - 1)]
                self.logind_restart_attempts += 1
                self.logind_restart_at = now + delay
                LOG.warning("event=LOGIND_MONITOR_RESTART_SCHEDULED delay=%.0fs attempt=%d",
                            delay, self.logind_restart_attempts)
            elif event == "power-unavailable":
                self.last_power_at = 0
            elif event.startswith("safety-sample:"):
                payload = json.loads(event.partition(":")[2])
                payload["sensors"] = [Sensor(**row) for row in payload["sensors"]]
                self.safety_sample = Sample(**payload)
                if self.safety_sample.at < self.last_lid_event_at:
                    self.safety_sample.lid_closed = self.lid_is_closed
                self._observe_sample_preparing()
                self._after_sleep_window()
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
                    raise RecoveryRequired("protective service dispatch failed")
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
        # U-R5: an IPC "reload" from the tray or CLI is the user's own
        # settings change; a SIGHUP (systemctl reload, updates) is not.
        by_user, self.reload_user_initiated = self.reload_user_initiated, False
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
            self._emergency_unlock("invalid-config-reload", user_initiated=by_user)
            LOG.error("Configuration reload rejected; input remains unlocked: %s", exc)
            return

        self._emergency_unlock("configuration-reload", user_initiated=by_user)
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
            # The only user-requested disable: the controller's or the CLI's
            # "set-lid-ignore false" (interface 3).
            self._disable_lid("lid-ignore-disabled", requested_by_user=True)
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
        try:
            # The journal must be writable before anything is armed (B3).
            self._write_safety(force=True, raise_errors=True)
        except OSError as exc:
            self.safety.release("ARM_FAILED")
            raise TransitionError(f"safety journal is not writable: {exc}") from exc
        previous_saved = self._saved_lid_ignore_close
        acquired = []
        try:
            self._acquire_lid_inhibitor()
            for record in records:
                if not record.grabbed:
                    record.device.grab()
                    record.grabbed = True
                    acquired.append(record)
            # Persist the preference only once arming has succeeded (B5).
            self._persist_user_settings(ignore_lid_close=True)
        except Exception:
            self._release_records(acquired)
            self._release_lid_inhibitor()
            self.safety.release("ARM_FAILED")
            raise
        self._saved_lid_ignore_close = True
        self.ignore_lid_close = True
        self.lid_policy_healthy = True
        self.lid_generation += 1
        self.lid_lease_deadline = self.clients[self.controller_fd].deadline
        try:
            self._write_safety(force=True, raise_errors=True)
        except OSError as exc:
            self._emergency_unlock("safety-journal-failed")
            self._revert_saved_lid_ignore(previous_saved)
            raise TransitionError(f"safety journal is not writable: {exc}") from exc
        self.last_action = "lid-ignore-enabled"
        self._write_state()
        self._emit_state_event(self.last_action)

    def _revert_saved_lid_ignore(self, previous: bool) -> None:
        if self._saved_lid_ignore_close == previous:
            return
        try:
            self._persist_user_settings(ignore_lid_close=previous)
            self._saved_lid_ignore_close = previous
        except TransitionError as exc:
            LOG.error("Could not revert the saved lid-ignore preference: %s", exc)

    def _record_lid_disable(self, reason: str, *, requested_by_user: bool,
                            user_initiated: bool | None = None) -> None:
        """Interface 3 (A8): every end of an active lid-ignore is logged with its
        reason and reported to the tray in the state snapshot. Only a
        "set-lid-ignore false" request is user-requested. ``user_initiated``
        (U-R5) is also true for the user's own emergency unlocks and IPC
        settings changes; by default it is derived from the reason."""
        key = lid_disable_key(reason)
        if user_initiated is None:
            user_initiated = requested_by_user or reason in USER_INITIATED_LID_REASONS
        self.last_lid_disable = {
            "reason": key,
            "requested_by_user": bool(requested_by_user),
            "user_initiated": bool(user_initiated or requested_by_user),
            "at": time.time(),
        }
        LOG.info("event=LID_IGNORE_DISABLED reason=%s requested_by_user=%s user_initiated=%s detail=%s",
                 key, bool(requested_by_user), bool(user_initiated or requested_by_user), reason)

    def _disable_lid(self, reason: str, *, requested_by_user: bool = False,
                     user_initiated: bool | None = None) -> None:
        if self.ignore_lid_close:
            self._record_lid_disable(reason, requested_by_user=requested_by_user,
                                     user_initiated=user_initiated)
        self.ignore_lid_close = False
        self.lid_policy_healthy = False
        self.lid_lease_deadline = None
        self.lid_generation += 1
        self._release_lid_inhibitor()
        records = [r for r in self.devices.values() if r.grabbed and "lid-switch" in r.kinds and not r.wants_grab(self.locked_kinds)]
        self.release_errors = self._release_records(records)
        self.safety.release()
        self._write_safety()
        self.last_action = reason
        self._write_state()
        self._emit_state_event(reason)

    def _acquire_lid_inhibitor(self) -> None:
        if self.lid_inhibitor_fd is not None:
            return
        bus = None
        try:
            import dbus

            # Use a private synchronous connection. Reusing dbus.SystemBus()
            # here would cache a connection before LogindMonitor attaches its
            # GLib main loop, preventing that monitor from receiving signals.
            bus = dbus.bus.BusConnection(dbus.bus.BUS_SYSTEM)
            manager = dbus.Interface(
                bus.get_object("org.freedesktop.login1", "/org/freedesktop/login1", introspect=False),
                "org.freedesktop.login1.Manager",
            )
            # This runs on the main loop: bound the call well below the
            # 60 s watchdog instead of the ~25 s D-Bus default (B4).
            descriptor = manager.Inhibit(
                "handle-lid-switch",
                "Input Lock",
                "Keep the computer running while its lid is closed",
                "block",
                timeout=5,
            )
            self.lid_inhibitor_fd = descriptor.take()
            LOG.info("logind lid-switch inhibitor acquired")
        except Exception as exc:
            raise TransitionError(f"cannot acquire logind lid inhibitor: {exc}") from exc
        finally:
            # The descriptor is ours after take(); the private connection is
            # no longer needed and must not leak one bus connection per arm.
            if bus is not None:
                try:
                    bus.close()
                except Exception:
                    pass

    def _release_lid_inhibitor(self) -> None:
        if self.lid_inhibitor_fd is None:
            return
        try:
            os.close(self.lid_inhibitor_fd)
        except OSError:
            pass
        self.lid_inhibitor_fd = None
        LOG.info("logind lid-switch inhibitor released")

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
                        # Re-read SW_LID after SYN_DROPPED with EVIOCGSW (P2-1).
                        # Unreadable is treated as telemetry loss (fail safe).
                        try:
                            closed = read_lid_switch(record.device)
                        except (OSError, AttributeError, TypeError, ValueError):
                            if self.ignore_lid_close:
                                self._start_protection("LID_TELEMETRY_LOST")
                        else:
                            self._handle_lid_switch_event(closed)
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
            # In a sleep window the policy takes no action and
            # _start_protection() defers; evidence is still recorded (S-R1).
            reason = self.safety.update(self.safety_sample)
            if reason:
                self._start_protection(reason)
            self._write_safety()
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
        # The emergency unlock is never debounced (L2): pressed right after
        # the primary hotkey, it must still release everything.
        if action != "emergency-unlock" and now - self.last_hotkey_at < debounce:
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
                if pending.source in PHYSICAL_EMERGENCY_SOURCES:
                    # Same-UID code must not re-lock straight away (L3).
                    self.lock_cooldown_until = time.monotonic() + EMERGENCY_LOCK_COOLDOWN_SECONDS
                self._emergency_unlock(pending.source)
            elif pending.action == "toggle-default":
                self._toggle_default_kinds(reason=pending.source)
            else:
                self._apply_action(
                    pending.action, pending.mode, timeout=None, reason=pending.source
                )
        except RecoveryRequired:
            raise
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

    def _release_inputs_keep_lid(self, reason: str, *, preserve_safety: bool = False) -> None:
        """Release keyboard/mouse locks without dropping the user's lid-ignore.

        User decision 2026-10-22: continuous lid-ignore means "never sleep on
        lid close". A suspend from another source (menu, idle, another power
        manager) or a locked screen must not silently turn it off. Input locks
        are still released, so the user can type at the lock screen or after
        resume. The lid-switch grab, the logind lid inhibitor and the armed
        thermal and battery safety stay in place; only that safety, a lost
        tray controller, logout, or the user ends lid-ignore. A protective
        (safety) suspend releases lid-ignore itself before it requests sleep.

        Our own protective suspend also arrives here (PrepareForSleep). Its
        PROTECTIVE_ACTION_PENDING state must survive in the journal and the
        status (S-R8a), so a pending protective action is always preserved.
        """
        preserve_safety = preserve_safety or self.safety.state == "PROTECTIVE_ACTION_PENDING"
        if not self.ignore_lid_close:
            self._emergency_unlock(reason, preserve_safety=preserve_safety)
            return
        try:
            self.pending_hotkey = None
            self.auto_unlock_deadline = None
            # Records state and emits the event itself.
            self._set_locked_kinds(set(), reason=reason + "-lid-ignore-kept", timeout=None)
        except RecoveryRequired:
            raise
        except Exception:
            LOG.exception("Input release before suspend failed; using the full emergency unlock")
            self._emergency_unlock(reason, preserve_safety=preserve_safety)
            return
        LOG.info("Input locks released before suspend; continuous lid-ignore kept; reason=%s", reason)

    def _emergency_unlock(
        self, reason: str, *, preserve_safety: bool = False, lid_reason: str | None = None,
        user_initiated: bool | None = None,
    ) -> None:
        # No acquisition, device reopening, persistence prerequisite, or policy
        # reconciliation may precede release. Each restriction is independent.
        self.pending_hotkey = None
        if self.ignore_lid_close:
            try:
                if user_initiated is None:
                    user_initiated = reason in USER_INITIATED_LID_REASONS
                self._record_lid_disable(lid_reason or reason, requested_by_user=False,
                                         user_initiated=user_initiated)
            except Exception:
                pass
        self.ignore_lid_close = False
        self.lid_policy_healthy = False
        self.lid_lease_deadline = None
        self.lid_generation += 1
        self.locked_kinds.clear()
        self.auto_unlock_deadline = None
        self.release_errors = self._release_records(list(self.devices.values()))
        self._release_lid_inhibitor()
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
        paused = self.sleep_window_since is not None
        deadlines.extend(client.deadline for client in self.clients.values()
                         if not (paused and client.subscribed))
        if paused:
            deadlines.append(self.sleep_window_since + self.sleep_window_cap)
        if self.sleep_grace_until is not None:
            deadlines.append(self.sleep_grace_until)
        if self.logind_error_since is not None and not self.logind_error_handled:
            deadlines.append(self.logind_error_since + LOGIND_ERROR_GRACE_SECONDS)
        if self.logind_restart_at is not None:
            deadlines.append(self.logind_restart_at)
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
        if (self.sleep_window_since is not None
                and now - self.sleep_window_since >= self.sleep_window_cap):
            LOG.warning("event=SLEEP_WINDOW_TIMEOUT seconds=%.0f logind_preparing=%s; resume signal missing",
                        now - self.sleep_window_since, self.logind_preparing)
            self._leave_sleep_window("timeout")
        # Controller and lid-lease deadlines are paused during a system sleep
        # window and extended to resume + CONTROLLER_TIMEOUT (A4). The lease
        # always mirrors the controller deadline, so controller expiry below
        # also ends the lease (_close_client).
        paused = self.sleep_window_since is not None
        for fd, client in list(self.clients.items()):
            if paused and client.subscribed:
                continue
            if now >= client.deadline and fd in self.clients:
                # The controller's heartbeat lease ran out (U-R1).
                self._close_client(fd, lease_expired=fd == self.controller_fd)
        if self.sleep_grace_until is not None and now >= self.sleep_grace_until:
            self.sleep_grace_until = None
            self._recheck_deferred_protection()
        if (self.logind_error_since is not None and not self.logind_error_handled
                and now - self.logind_error_since >= LOGIND_ERROR_GRACE_SECONDS):
            self.logind_error_handled = True
            LOG.warning("event=LOGIND_ERROR_PERSISTENT seconds=%.0f; treating as session loss",
                        now - self.logind_error_since)
            self._on_session_lost("logind-unavailable")
        if self.logind_restart_at is not None and now >= self.logind_restart_at:
            self.logind_restart_at = None
            LOG.info("event=LOGIND_MONITOR_RESTART attempt=%d", self.logind_restart_attempts)
            self._start_logind_monitor()
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
            except RecoveryRequired:
                raise
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
            "protocol": PROTOCOL,  # == input_lock_common.PROTOCOL_VERSION
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
            "last_lid_disable": self.last_lid_disable,
            "system_sleep_in_progress": self.sleep_window_since is not None,
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
                pid, uid, _gid = struct.unpack(
                    PEERCRED_FORMAT,
                    client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEERCRED_SIZE),
                )
            except OSError:
                client.close()
                continue
            if uid not in {0, self.config.authorized_uid} or len(self.clients) >= MAX_CLIENTS:
                client.close()
                continue
            state = ClientState(sock=client, uid=uid, pid=pid)
            fd = client.fileno()
            self.clients[fd] = state
            self.selector.register(client, selectors.EVENT_READ, ("client", fd))

    def _read_client(self, fd: int) -> None:
        state = self.clients.get(fd)
        if state is None or state.close_after_flush:
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
                    problem = self._controller_claim_problem(state)
                    if problem:
                        LOG.warning("event=CONTROLLER_CLAIM_REFUSED pid=%d uid=%d reason=%s",
                                    state.pid, state.uid, problem)
                        raise ValueError(problem)
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
            if cmd in {"lock", "lock-default", "toggle", "toggle-default", "gnome-hotkey"}:
                self._check_lock_cooldown(fd, cmd)
            if cmd == "status":
                response: dict[str, object] = {
                    "ok": True,
                    "protocol": PROTOCOL,  # == input_lock_common.PROTOCOL_VERSION
                    "state": self._state_payload(),
                }
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
                # Called synchronously by the system-sleep "pre" hook, before
                # the kernel freezer runs (A3/A4). Root only (S-R1): the
                # window pauses deadlines and extends the watchdog.
                self._require_root_peer(fd, cmd)
                self._enter_sleep_window("sleep-hook")
                if self.config.unlock_on_suspend:
                    self._release_inputs_keep_lid("system-sleep-hook")
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "resume-sleep":
                # The system-sleep "post" hook (interface 4). Root only (S-R1).
                self._require_root_peer(fd, cmd)
                self._leave_sleep_window("sleep-hook")
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "reload":
                self.reload_requested = True
                self.reload_user_initiated = True
                response = {"ok": True, "message": "reload scheduled"}
            elif cmd == "quit":
                # The unit has Restart=no, so a clean exit stays stopped.
                # Cleanup releases every managed grab.
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
                # The same validation as the config and the UI (L4,
                # interface 2): >=2 modifiers plus one A-Z, 0-9 or F1-F12 key.
                # ConfigError is a ValueError and is reported to the client.
                from input_lock_common import canonical_hotkey
                self._set_primary_hotkey(canonical_hotkey(hotkey))
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
            elif cmd == "toggle-default":
                self._toggle_default_kinds(reason="ipc-toggle-default")
                response = {"ok": True, "state": self._state_payload()}
            elif cmd == "ping":
                response = {"ok": True, "version": VERSION, "protocol": PROTOCOL}
            else:
                raise ValueError("unsupported command")
            self._send_and_close(fd, response)
        except RecoveryRequired:
            # S-R7: e.g. a protective release that failed while a client was
            # being closed. Terminate so that systemd runs InputLock recovery.
            raise
        except (TransitionError, ValueError) as exc:
            self._send_and_close(fd, {"ok": False, "error": str(exc)})
        except Exception as exc:
            LOG.exception("Unexpected control request failure for %s", cmd)
            self._send_and_close(fd, {"ok": False, "error": f"internal error: {exc}"})

    def _require_root_peer(self, fd: int, cmd: str) -> None:
        """Only root (the system-sleep hook) may open or close the sleep
        window (S-R1). The authorized user's CLI gets a clear refusal."""
        state = self.clients.get(fd)
        if state is not None and state.uid == 0:
            return
        LOG.warning("event=SLEEP_WINDOW_REQUEST_REFUSED cmd=%s pid=%s uid=%s",
                    cmd, state.pid if state else "?", state.uid if state else "?")
        raise ValueError(f"{cmd} is accepted only from root (the system-sleep hook)")

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

    def _check_lock_cooldown(self, fd: int, cmd: str) -> None:
        """Refuse IPC requests that could lock right after a physical emergency
        or TTY-recovery unlock (L3), so same-UID code cannot defeat the
        emergency hotkey by re-locking at once. Unlocking is never refused."""
        remaining = self.lock_cooldown_until - time.monotonic()
        if remaining <= 0:
            return
        if cmd in {"toggle", "toggle-default", "gnome-hotkey"} and self.locked_kinds:
            return  # With something locked these toggle towards unlocked.
        state = self.clients.get(fd)
        LOG.warning("event=LOCK_REFUSED_COOLDOWN cmd=%s pid=%s uid=%s remaining=%.0fs",
                    cmd, state.pid if state else "?", state.uid if state else "?", remaining)
        raise TransitionError(
            f"locking is paused for {int(remaining) + 1} s after an emergency unlock"
        )

    @staticmethod
    def _peer_session_scope(pid: int, uid: int) -> tuple[str | None, str | None]:
        """Classify a peer process by its cgroup: ("session", id) for a logind
        session scope, ("user-manager", None) for the user's systemd --user
        services (where the tray runs), or (None, None) when unknown."""
        if pid <= 0:
            return None, None
        try:
            text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None, None
        for line in text.splitlines():
            cgroup = line.rpartition(":")[2]
            for part in cgroup.split("/"):
                if part.startswith("session-") and part.endswith(".scope"):
                    return "session", part[len("session-"):-len(".scope")]
            if f"/user@{uid}.service/" in cgroup + "/":
                return "user-manager", None
        return None, None

    def _controller_claim_problem(self, state: ClientState) -> str | None:
        """L6: the tray controller must come from the authorized user's local
        graphical session. Returns a refusal reason, or None to accept.

        A peer in a session scope must be in one of the user's local graphical
        sessions. A peer in the user's service manager is accepted while that
        user has an active or locked local session. Anything that cannot be
        verified keeps the previous first-come behaviour and is logged.
        """
        if state.uid != self.config.authorized_uid:
            return "the tray controller must run as the authorized user"
        kind, session_id = self._peer_session_scope(state.pid, state.uid)
        graphical = getattr(self.logind_monitor, "graphical_session_ids", None)
        if kind == "session" and graphical is not None:
            if session_id in graphical:
                LOG.info("event=CONTROLLER_CLAIMED pid=%d session=%s", state.pid, session_id)
                return None
            return "the tray controller must run in the local graphical session"
        if kind == "user-manager" and (self.authorized_session_active or self.authorized_session_locked):
            LOG.info("event=CONTROLLER_CLAIMED pid=%d scope=user-manager", state.pid)
            return None
        LOG.warning("event=CONTROLLER_CLAIM_UNVERIFIED pid=%d scope=%s session_active=%s; accepting first come",
                    state.pid, kind or "unknown", self.authorized_session_active)
        return None

    def _send(self, fd: int, payload: dict[str, object], *, coalesce: bool = False) -> bool:
        """Queue one message for a client and write what the socket accepts.

        Backpressure is never a reason to drop a client (B2): unsent bytes
        wait for EVENT_WRITE. With coalesce=True (state events) a newer event
        replaces an older one that has not started sending yet.
        """
        state = self.clients.get(fd)
        if state is None:
            return False
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if coalesce and (state.outbuf or state.pending_state is not None):
            state.pending_state = encoded
            return self._flush_client(fd)
        if state.pending_state is not None:
            # Keep order: the queued state event goes before this message.
            state.outbuf.extend(state.pending_state)
            state.pending_state = None
        state.outbuf.extend(encoded)
        return self._flush_client(fd)

    def _flush_client(self, fd: int) -> bool:
        state = self.clients.get(fd)
        if state is None:
            return False
        while True:
            if not state.outbuf and state.pending_state is not None:
                state.outbuf.extend(state.pending_state)
                state.pending_state = None
            if not state.outbuf:
                break
            try:
                sent = state.sock.send(state.outbuf)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                if not state.closing:
                    self._close_client(fd)
                return False
            if not isinstance(sent, int) or sent <= 0:
                break
            del state.outbuf[:sent]
        if state.closing:
            # Final best-effort flush from _close_client: never wait.
            return not (state.outbuf or state.pending_state is not None)
        if state.outbuf or state.pending_state is not None:
            if len(state.outbuf) > CLIENT_OUTPUT_LIMIT and fd != self.controller_fd:
                LOG.warning("Dropping IPC client pid=%d: %d unsent bytes", state.pid, len(state.outbuf))
                self._close_client(fd)
                return False
            self._watch_client(fd, state, write=True)
            return True
        if state.close_after_flush:
            self._close_client(fd)
            return True
        self._watch_client(fd, state, write=False)
        return True

    def _watch_client(self, fd: int, state: ClientState, *, write: bool) -> None:
        if state.watching_write == write and not (write and state.close_after_flush):
            return
        events = 0 if state.close_after_flush else selectors.EVENT_READ
        if write:
            events |= selectors.EVENT_WRITE
        try:
            self.selector.modify(state.sock, events, ("client", fd))
            state.watching_write = write
        except (KeyError, ValueError, OSError):
            pass

    def _send_and_close(self, fd: int, payload: dict[str, object]) -> None:
        state = self.clients.get(fd)
        if state is None:
            return
        # Stop reading; close once the response has been written (or when the
        # client's deadline expires).
        state.close_after_flush = True
        self._send(fd, payload)

    def _close_client(self, fd: int, *, lease_expired: bool = False) -> None:
        """Close one client. For the tray controller, end lid-ignore first
        (protective suspend with a possibly closed lid) and send the final
        state event, with last_lid_disable, before the socket closes (U-R1):
        the tray must learn why it lost lid-ignore, not just see EOF.

        ``lease_expired``: the controller missed its heartbeat lease; it is
        reported as "controller-lease-expired". A closed or broken connection
        stays "controller-disconnected".
        """
        state = self.clients.get(fd)
        if state is None:
            self.subscribers.discard(fd)
            return
        if state.closing:
            return
        state.closing = True
        try:
            if fd == self.controller_fd:
                self.controller_fd = None
                emitted = self.state_event_seq
                lid_key = CONTROLLER_LEASE_EXPIRED_KEY if lease_expired else "controller-disconnected"
                try:
                    if self.ignore_lid_close and self._lid_may_be_closed() and self.running:
                        if lease_expired:
                            self._start_protection("CONTROLLER_LOST", lid_reason=CONTROLLER_LEASE_EXPIRED_KEY)
                        else:
                            self._start_protection("CONTROLLER_LOST")
                    elif self.ignore_lid_close:
                        self._disable_lid(lid_key)
                finally:
                    self._final_flush(fd, state, lid_key, send_event=self.state_event_seq == emitted)
        finally:
            self.clients.pop(fd, None)
            self.subscribers.discard(fd)
            try:
                self.selector.unregister(state.sock)
            except Exception:
                pass
            try:
                state.sock.close()
            except OSError:
                pass

    def _final_flush(self, fd: int, state: ClientState, reason: str, *, send_event: bool) -> None:
        """Best effort and non-blocking: queue the final state event (unless
        one was just emitted) and write what the socket accepts now."""
        if not state.subscribed:
            return
        try:
            if send_event:
                self._send(fd, {"type": "event", "event": "state-changed", "reason": reason,
                                "state": self._state_payload()}, coalesce=True)
            else:
                self._flush_client(fd)
        except RecoveryRequired:
            raise
        except Exception:
            LOG.debug("Final state event to the closing controller failed", exc_info=True)

    def _emit_state_event(self, reason: str) -> None:
        self.state_event_seq += 1
        payload = {
            "type": "event",
            "event": "state-changed",
            "reason": reason,
            "state": self._state_payload(),
        }
        for fd in list(self.subscribers):
            self._send(fd, payload, coalesce=True)


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
