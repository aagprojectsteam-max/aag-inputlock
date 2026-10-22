#!/usr/bin/python3
"""Per-user tray indicator and notification bridge for Input Lock."""

from __future__ import annotations

import errno
import json
import logging
import os
import queue
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from input_lock_common import (  # noqa: E402
    MAX_MESSAGE_BYTES,
    VERSION,
    load_config,
    response_ok,
    send_request,
)
from locklock_core.allowed_keys import display_key, gtk_key_to_canonical  # noqa: E402


TITLES: Final = {
    "all-locked": ("Input locked", "Press the primary shortcut to unlock."),
    "keyboard-locked": ("Keyboard locked", "Press the primary shortcut to unlock."),
    "mouse-locked": ("Pointer locked", "Press the primary shortcut to unlock."),
    "touchscreen-locked": ("Touchscreen locked", "Press the primary shortcut to unlock."),
    "unlocked": ("Input unlocked", "Local input is available."),
    "auto-unlocked": ("Input auto-unlocked", "The safety timer released local input."),
}
LOG = logging.getLogger("input-lock-tray")
CURSOR_SETTINGS_PATH: Final = Path.home() / ".config/input-lock/cursor-settings.json"
DEFAULT_CURSOR_TIMEOUT_SECONDS: Final = 60
MAX_CURSOR_TIMEOUT_SECONDS: Final = 86_399
GNOME_IDLE_ONLY: Final = 8
# Keep the GNOME idle-only inhibitor this long after lid-ignore is lost
# without a user request while the lid is open, so a long-idle session is not
# suspended by gsd the instant the override disappears (review U3).
IDLE_GRACE_SECONDS: Final = 60
SECOND_INSTANCE_RETRY_SECONDS: Final = 300.0
DISCONNECTED_REMINDER_SECONDS: Final = 600.0
CURSOR_EXTENSION_UUID: Final = "input-lock-cursor@aag-projects-team"

# ---------------------------------------------------------------------------
# User-facing strings (English only this round; kept together for a later
# translation pass).
# ---------------------------------------------------------------------------
LID_LABEL: Final = "Ignore Lid Close"
CONTINUOUS_LID_LABEL: Final = "Ignore Lid Close (continuous, safety-monitored)"
LID_UNAVAILABLE_LABEL: Final = "Ignore Lid Close (service unavailable)"
LID_SAFETY_UNAVAILABLE_LABEL: Final = "Ignore Lid Close (safety unavailable)"
TRAY_STOP_NOTE: Final = (
    "Quitting, stopping or restarting the Input Lock tray turns Ignore Lid Close off "
    "and unlocks input."
)
LID_TOOLTIP: Final = (
    "Keep running with the lid closed, under thermal, battery and telemetry supervision. "
    + TRAY_STOP_NOTE
)
LID_LOST_TITLE: Final = "Ignore Lid Close was turned off"
REENABLE_ACTION_LABEL: Final = "Re-enable…"
REENABLE_HINT: Final = (
    "It will not turn itself back on. To turn it on again, open the lid and choose "
    "“Re-enable…” or use the Input Lock tray menu."
)
POWER_RESTORED_TITLE: Final = "Power mode restored"
TRAY_UNLOCK_LID_NOTE: Final = (
    "Unlocking input from the tray also turns Ignore Lid Close off. "
    "Turn it on again from the tray menu if you still need it."
)
POWER_RESTORED_BODY: Final = (
    "Power mode was changed manually; power-saver restored while Ignore Lid Close is on."
)
DISCONNECTED_ICON: Final = "input-lock-unlocked"
SAFETY_STATE_NAMES: Final = {
    "SAFE_NORMAL": "normal OS behavior", "RECOVERY": "recovered; re-arm required",
    "LID_IGNORE_ARMED": "armed; lid open", "LID_CLOSED_MONITORED": "monitoring closed lid",
    "LID_CLOSED_WARNING": "safety warning — inspect heat/battery/telemetry",
    "LID_CLOSED_HIGH_RISK": "high risk — open the lid",
    "PROTECTIVE_ACTION_FAILED": "protection FAILED — open the lid",
    "PROTECTIVE_ACTION_PENDING": "protective action requested",
    "PROTECTIVE_ACTION_EXECUTED": "suspend transition confirmed",
}
# Disable reasons that mean "the user asked for it" when an older daemon does
# not publish last_lid_disable.requested_by_user.
USER_DISABLE_REASONS: Final = frozenset({"lid-ignore-disabled"})
SAFETY_REASON_TEXT: Final = {
    "THERMAL_EMERGENCY": "the computer reached a dangerous temperature",
    "THERMAL_HIGH_RISK": "the computer stayed too hot for too long",
    "THERMAL_WARNING": "the computer was running hot",
    "BATTERY_CRITICAL": "the battery is critically low",
    "TELEMETRY_LOST": "temperature or battery readings were unavailable for too long",
    "CONTROLLER_LOST": "the tray lost contact with the service while the lid may have been closed",
}
LID_DISABLE_REASON_TEXT: Final = {
    "controller-lease-expired": (
        "The Input Lock tray did not check in with the background service in time "
        "(this can happen while the computer is going to sleep or waking up)."),
    "controller-disconnected": "The Input Lock tray's connection to the background service was closed.",
    "session-inactive": "Your desktop session stopped being active (user switch, logout or a session problem).",
    "authorized-session-inactive": (
        "Your desktop session stopped being active (user switch, logout or a session problem)."),
    "user-session-ended": "The Input Lock tray was stopped or your desktop session ended.",
    "daemon-shutdown": "The Input Lock background service was stopped or restarted.",
    "daemon-unavailable": "The connection to the Input Lock background service was lost (the service stopped or crashed).",
    "daemon-restarted": (
        "The Input Lock background service restarted; Ignore Lid Close always starts off "
        "after a restart."),
    "ipc-emergency-unlock": "Input was unlocked with an emergency unlock, which also turns Ignore Lid Close off.",
    "emergency-hotkey": "The emergency unlock shortcut was used, which also turns Ignore Lid Close off.",
    "gnome-emergency-hotkey": "The emergency unlock shortcut was used, which also turns Ignore Lid Close off.",
    "tty-recovery-hotkey": "A text-console recovery shortcut was used, which also turns Ignore Lid Close off.",
    "hotkey-change": "The lock shortcut was changed, which resets all locks.",
    "lock-kinds-change": "The devices to lock were changed, which resets all locks.",
    "allowed-keys-change": "The keys allowed while locked were changed, which resets all locks.",
    "configuration-reload": "The Input Lock configuration was reloaded, which resets all locks.",
    "invalid-config-reload": "The Input Lock configuration could not be reloaded, so all locks were reset.",
    "release-all": "All locks were released.",
    "safety-journal-failed": "Input Lock could not save its safety record, so it stopped ignoring the lid.",
    "power-saver-ineffective": "The power-saver cooling hold could not be kept.",
    "lease-unavailable": "GNOME idle protection or power-saver cooling stopped working.",
}


def _safety_cause(reason: str, state: dict[str, object]) -> str:
    upper = reason.upper()
    for part in (upper, *upper.replace(":", "=").split("=")[1:]):
        if part in SAFETY_REASON_TEXT:
            return part
    safety = state.get("safety")
    decision = safety.get("last_decision") if isinstance(safety, dict) else None
    if isinstance(decision, dict) and isinstance(decision.get("reason"), str):
        return decision["reason"].upper()
    result = state.get("protective_result")
    if isinstance(result, dict) and isinstance(result.get("reason"), str):
        return result["reason"].upper()
    return ""


def describe_lid_disable(reason: str, state: dict[str, object] | None = None,
                         detail: str = "") -> str:
    """Plain-words explanation for an Ignore Lid Close loss (review A8)."""
    state = state if isinstance(state, dict) else {}
    reason = (reason or "").strip()
    lowered = reason.lower()
    text = LID_DISABLE_REASON_TEXT.get(lowered)
    if text is None:
        cause = _safety_cause(reason, state) if (
            lowered.startswith("safety") or reason.upper() in SAFETY_REASON_TEXT
            or ":" in reason or "=" in reason) else ""
        if cause in SAFETY_REASON_TEXT:
            text = "Safety protection stepped in: " + SAFETY_REASON_TEXT[cause] + "."
        elif lowered.startswith("safety"):
            text = ("Safety protection stepped in"
                    + (f" ({cause.lower().replace('_', ' ')})." if cause else "."))
        elif "thermal" in lowered or "temperature" in lowered or "heat" in lowered:
            text = "Safety protection stepped in because of heat."
        elif "battery" in lowered:
            text = "Safety protection stepped in because of the battery level."
        elif "telemetry" in lowered or "supervision" in lowered or "monitor" in lowered:
            text = "Safety supervision could not keep monitoring the computer."
        elif "sleep" in lowered or "suspend" in lowered or "resume" in lowered:
            text = "It was lost around a sleep or wake-up of the computer."
        elif "restart" in lowered or "shutdown" in lowered:
            text = LID_DISABLE_REASON_TEXT["daemon-shutdown"]
        elif reason:
            text = f"The Input Lock service turned it off (reason: {reason})."
        else:
            text = "The Input Lock service turned it off without giving a reason."
    if detail:
        text = text.rstrip() + f" Details: {detail}"
    return text


class SerialWorker:
    """One background thread that runs blocking lease work strictly in order.

    D-Bus lease checks, HoldProfile retries and the enable request never run on
    the GTK main thread (review U4); serialising them also removes races
    between acquire/release of the two guards.
    """

    def __init__(self, name: str) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, function, *args) -> None:
        self._queue.put((function, args))

    def stop(self, timeout: float = 5.0) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            function, args = item
            try:
                function(*args)
            except Exception:
                LOG.exception("Background lease task failed")


def cursor_extension_enabled(settings=None) -> bool | None:
    """Whether GNOME Shell is configured to run the cursor-hider extension."""
    try:
        if settings is None:
            source = Gio.SettingsSchemaSource.get_default()
            if source is None or source.lookup("org.gnome.shell", True) is None:
                return None
            settings = Gio.Settings.new("org.gnome.shell")
        if settings.get_boolean("disable-user-extensions"):
            return False
        return (CURSOR_EXTENSION_UUID in settings.get_strv("enabled-extensions")
                and CURSOR_EXTENSION_UUID not in settings.get_strv("disabled-extensions"))
    except Exception:
        return None


class GnomeIdleGuard:
    """Per-session GNOME idle-only inhibitor; never blocks protective suspend.

    The cookie belongs to the tray's session D-Bus connection. GNOME removes
    it on session-bus disconnect. It is not a persistent gsettings override,
    a system sleep blocker, or a modification of critical-battery actions.
    """

    def __init__(self, proxy_factory=None) -> None:
        self.proxy_factory = proxy_factory or self._new_proxy
        self.proxy = None
        self.cookie: int | None = None
        self.owner: str | None = None

    @staticmethod
    def _new_proxy():
        # Dedicated connection: closing it removes any session inhibitor even
        # if an Inhibit/Uninhibit reply was lost. Never close the shared GTK bus.
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        bus = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
            Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None,
        )
        try:
            return Gio.DBusProxy.new_sync(
                bus, Gio.DBusProxyFlags.NONE, None,
                "org.gnome.SessionManager", "/org/gnome/SessionManager",
                "org.gnome.SessionManager", None,
            )
        except Exception:
            bus.close_sync(None)
            raise

    def acquire(self) -> None:
        if self.proxy is None:
            self.proxy = self.proxy_factory()
        try:
            owner = self.proxy.get_name_owner()
            if not owner:
                raise RuntimeError("GNOME SessionManager is unavailable")
        except Exception:
            self._close_connection()
            raise
        if self.cookie is not None and self.owner == owner:
            return
        # A GNOME restart makes the old cookie invalid.
        self.cookie = None
        self.owner = None
        try:
            result = self.proxy.call_sync(
                "Inhibit",
                GLib.Variant("(susu)", (
                    "aag-inputlock", 0,
                    "Continuous lid-ignore: prevent automatic GNOME idle sleep",
                    GNOME_IDLE_ONLY,
                )),
                Gio.DBusCallFlags.NONE, 3000, None,
            )
            self.cookie = int(result.unpack()[0])
            self.owner = owner
        except Exception:
            # The request might have succeeded but its reply been lost.
            # Close the *private* connection to cancel any orphaned cookie.
            self._close_connection()
            raise

    def _close_connection(self) -> None:
        proxy = self.proxy
        self.cookie = None
        self.owner = None
        self.proxy = None
        if proxy is None:
            return
        try:
            proxy.get_connection().close_sync(None)
        except Exception as exc:
            LOG.warning("GNOME idle inhibitor bus close unconfirmed: %s", exc)

    def release(self) -> None:
        cookie, owner, proxy = self.cookie, self.owner, self.proxy
        try:
            if cookie is not None and proxy is not None and proxy.get_name_owner() == owner:
                proxy.call_sync(
                    "Uninhibit", GLib.Variant("(u)", (cookie,)),
                    Gio.DBusCallFlags.NONE, 2000, None,
                )
        except Exception as exc:
            LOG.warning("GNOME idle-only inhibitor release unconfirmed: %s", exc)
        finally:
            # Always sever the independent bus connection. GNOME drops any
            # surviving cookie for this sender, including lost acknowledgments.
            self._close_connection()


class PowerSaverGuard:
    """Reversible heat-prevention hold while continuous lid-ignore is active.

    HoldProfile is a *lease*, not a persistent power-profile setting. Releasing
    it (or closing this dedicated system-bus connection on a tray crash) lets
    power-profiles-daemon return to the underlying user preference. Holding
    power-saver is mitigation, never an excuse to weaken thermal thresholds.
    """

    RETRY_SETTLE_SECONDS: Final = 1.0

    def __init__(self, proxy_factory=None, *, sleep=None) -> None:
        self.proxy_factory = proxy_factory or self._new_proxy
        self.sleep = sleep or time.sleep
        self.proxy = None
        self.cookie: int | None = None
        self.owner: str | None = None
        self.released_externally = False
        self.on_released = None
        self._signal_connection = None
        self._signal_id: int | None = None
        self._released_cookies: set[int] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _new_proxy():
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None)
        bus = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
            Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None,
        )
        try:
            return Gio.DBusProxy.new_sync(
                bus, Gio.DBusProxyFlags.NONE, None,
                "net.hadess.PowerProfiles", "/net/hadess/PowerProfiles",
                "net.hadess.PowerProfiles", None,
            )
        except Exception:
            bus.close_sync(None)
            raise

    def _active_profile(self) -> str:
        # Ask the service rather than trusting a possibly stale cached signal.
        assert self.proxy is not None
        result = self.proxy.get_connection().call_sync(
            "net.hadess.PowerProfiles", "/net/hadess/PowerProfiles",
            "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("net.hadess.PowerProfiles", "ActiveProfile")),
            GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, 3000, None,
        )
        return str(result.unpack()[0])

    def _subscribe_released(self) -> None:
        """Watch PPD's ProfileReleased so a manual mode change is noticed.

        Subscribed from whatever thread acquires the hold, but without a
        thread-default main context the callback is dispatched on the global
        default context, i.e. the GTK main thread.
        """
        if self._signal_id is not None or self.proxy is None:
            return
        try:
            connection = self.proxy.get_connection()
            self._signal_id = connection.signal_subscribe(
                "net.hadess.PowerProfiles", "net.hadess.PowerProfiles",
                "ProfileReleased", "/net/hadess/PowerProfiles", None,
                Gio.DBusSignalFlags.NONE, self._on_profile_released_signal,
            )
            self._signal_connection = connection
        except Exception as exc:
            # The post-hold verification still catches an ineffective hold.
            LOG.warning("Could not watch power-saver hold releases: %s", exc)
            self._signal_id = None
            self._signal_connection = None

    def _on_profile_released_signal(self, _connection, _sender, _path, _interface,
                                    _signal, parameters) -> None:
        try:
            cookie = int(parameters.unpack()[0])
        except Exception:
            return
        self.profile_released(cookie)

    def profile_released(self, cookie: int) -> bool:
        """Record that PPD dropped our hold (e.g. a manual Power Mode pick).

        Runs on the GTK main thread, so it never waits for the guard lock
        (an acquire may be blocked in D-Bus on the lease worker). It only
        marks the cookie; the next acquire() takes a fresh hold.
        """
        current = self.cookie
        if current is None or cookie != current:
            return False
        self._released_cookies.add(cookie)
        self.released_externally = True
        LOG.warning("Power-saver hold %s was released by power-profiles-daemon "
                    "(manual power-mode change?)", cookie)
        callback = self.on_released
        if callback is not None:
            try:
                callback()
            except Exception as exc:
                LOG.warning("Power-saver release handler failed: %s", exc)
        return True

    def _hold(self) -> None:
        owner = self.proxy.get_name_owner()
        if not owner:
            raise RuntimeError("Power Profiles service is unavailable")
        if self.cookie is not None and self.owner != owner:
            # The manager restarted, so any old cookie became invalid.
            self._close_connection()
            self.proxy = self.proxy_factory()
            owner = self.proxy.get_name_owner()
            if not owner:
                raise RuntimeError("Power Profiles service disappeared")
        self._subscribe_released()
        if self.cookie is not None and self.cookie in self._released_cookies:
            # PPD already dropped it (ProfileReleased): never reuse it.
            self._released_cookies.discard(self.cookie)
            self.cookie = None
        if self.cookie is None:
            # Request only the lower-power profile, never disable
            # hardware throttling or override system thermal services.
            reply = self.proxy.call_sync(
                "HoldProfile", GLib.Variant("(sss)", (
                    "power-saver",
                    "Keep closed-lid laptop cool during supervised InputLock",
                    "org.aag.InputLock",
                )), Gio.DBusCallFlags.NONE, 3000, None,
            )
            self.cookie = int(reply.unpack()[0])
            self.owner = owner

    def acquire(self) -> bool:
        """Hold power-saver and verify it; return True if it was re-taken.

        A single mismatch (stale cookie after a manual Power Mode change, or a
        missed ProfileReleased) is not a failure: reconnect, hold again and
        re-check after about a second. Only a fresh hold that is still
        ineffective raises, naming the observed profile.
        """
        with self._lock:
            restored = self.released_externally
            if self.proxy is None:
                self.proxy = self.proxy_factory()
            try:
                self._hold()
                observed = self._active_profile()
                if observed != "power-saver":
                    LOG.warning("Power-saver hold not effective (active profile: %s); "
                                "reconnecting and holding again", observed)
                    self._close_connection()
                    self.proxy = self.proxy_factory()
                    self._hold()
                    self.sleep(self.RETRY_SETTLE_SECONDS)
                    observed = self._active_profile()
                    if observed != "power-saver":
                        raise RuntimeError(
                            "Power-saver hold did not become effective after one retry "
                            f"(active profile: {observed})")
                    restored = True
                self.released_externally = False
                return restored
            except Exception:
                # Includes uncertain replies: close the dedicated connection so
                # no orphan power lease can outlive a failed activation.
                self._close_connection()
                raise

    def _close_connection(self) -> None:
        proxy = self.proxy
        connection, signal_id = self._signal_connection, self._signal_id
        self.proxy = None
        self.cookie = None
        self.owner = None
        self._signal_connection = None
        self._signal_id = None
        if connection is not None and signal_id is not None:
            try:
                connection.signal_unsubscribe(signal_id)
            except Exception:
                pass
        if proxy is not None:
            try:
                proxy.get_connection().close_sync(None)
            except Exception as exc:
                LOG.warning("Power-saver system-bus release unconfirmed: %s", exc)

    def release(self) -> None:
        with self._lock:
            self.released_externally = False
            cookie, owner, proxy = self.cookie, self.owner, self.proxy
            if cookie is not None and cookie in self._released_cookies:
                cookie = None  # PPD already released it
            self._released_cookies.clear()
            try:
                if cookie is not None and proxy is not None and proxy.get_name_owner() == owner:
                    proxy.call_sync(
                        "ReleaseProfile", GLib.Variant("(u)", (cookie,)),
                        Gio.DBusCallFlags.NONE, 2500, None,
                    )
            except Exception as exc:
                LOG.warning("Power-saver hold release unconfirmed: %s", exc)
            finally:
                self._close_connection()


class TrayNotifier:
    # Class-level defaults keep partially constructed (test) instances and a
    # half-initialised tray safe; __init__ sets the real per-instance values.
    current_state: Mapping[str, object] = MappingProxyType({})  # replaced, never mutated
    connected = False
    state_stale = False
    updating_lid_item = False
    updating_autostart_item = False
    autostart_cached = False
    idle_guard = None
    power_guard = None
    lease_worker = None
    reconnect_event = None
    idle_guard_failed_generation = None
    enable_in_flight = False
    cooling_verified = False
    idle_held = False
    lease_acquire_pending = False
    grace_until: float | None = None
    grace_source = 0
    own_disable_reason: str | None = None
    down_since: float | None = None
    tray_unlock_until: float | None = None

    def __init__(self) -> None:
        self.config = load_config()
        self.running = True
        self.previous_locked: frozenset[str] | None = None
        self.current_state: dict[str, object] = {}
        self.connected = False
        self.selected_hotkey: str | None = None
        self.updating_lid_item = False
        self.updating_autostart_item = False
        self.idle_guard = GnomeIdleGuard()
        self.power_guard = PowerSaverGuard()
        self.power_guard.on_released = self._on_power_hold_released
        self.idle_guard_failed_generation: int | None = None
        self.reconnect_event = threading.Event()
        self.lease_worker = SerialWorker("input-lock-leases")

        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "input-lock",
            "input-lock-unlocked",
            AyatanaAppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
        )
        # AppIndicator does not recursively search an arbitrary theme root.
        # Point it at the directory that directly contains our status icons;
        # otherwise GNOME may display its three-dot missing-icon placeholder.
        self.indicator.set_icon_theme_path("/usr/share/icons/hicolor/scalable/status")
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Input Lock")

        self.menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label="Input Lock — Connecting…")
        self.status_item.set_sensitive(False)
        self.menu.append(self.status_item)
        self.safety_item = Gtk.MenuItem(label="Lid safety: connecting…")
        self.safety_item.set_sensitive(False)
        self.menu.append(self.safety_item)

        self.menu.append(Gtk.SeparatorMenuItem())
        self.toggle_item = Gtk.MenuItem(label="Lock Input")
        self.toggle_item.set_sensitive(False)
        self.toggle_item.connect("activate", self._on_toggle)
        self.menu.append(self.toggle_item)

        self.shortcut_item = Gtk.MenuItem(label="Shortcut: Ctrl+Alt+Z")
        self.shortcut_item.set_sensitive(False)
        self.menu.append(self.shortcut_item)

        self.settings_item = Gtk.MenuItem(label="Settings…")
        self.settings_item.connect("activate", self._on_settings)
        self.menu.append(self.settings_item)

        self.lid_item = Gtk.CheckMenuItem(label=CONTINUOUS_LID_LABEL)
        self.lid_item.set_sensitive(False)
        self.lid_item.set_tooltip_text(LID_TOOLTIP)
        self.lid_item.connect("toggled", self._on_lid_toggled)
        self.menu.append(self.lid_item)

        # The real state is read off the main thread (systemctl can block).
        self.autostart_item = Gtk.CheckMenuItem(label="Start at Login")
        self.autostart_item.set_sensitive(False)
        self.autostart_item.connect("toggled", self._on_autostart_toggled)
        self.menu.append(self.autostart_item)
        self._refresh_autostart_async()
        self._watch_socket_directory()

        self.menu.append(Gtk.SeparatorMenuItem())
        self.about_item = Gtk.MenuItem(label="About")
        self.about_item.connect("activate", self._on_about)
        self.menu.append(self.about_item)

        self.quit_item = Gtk.MenuItem(label="Quit")
        self.quit_item.connect("activate", self._on_quit)
        self.menu.append(self.quit_item)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

    def stop(self, _signum: int, _frame: object) -> None:
        self.running = False
        # Never make a synchronous D-Bus call inside a Python signal handler.
        # run() releases the guard after Gtk.main() unwinds; process exit is
        # an independent final D-Bus inhibitor release guarantee.
        client = getattr(self, "subscription_socket", None)
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        Gtk.main_quit()

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        worker = threading.Thread(
            target=self._subscription_worker,
            name="input-lock-subscription",
            daemon=True,
        )
        worker.start()
        try:
            Gtk.main()
        finally:
            self.running = False
            event = self.reconnect_event
            if event is not None:
                event.set()
            worker = self.lease_worker
            if worker is not None:
                worker.stop()
            self.idle_guard.release()
            self.power_guard.release()
        return 0

    # ------------------------------------------------------------------
    # Subscription (heartbeat) thread
    # ------------------------------------------------------------------
    def _watch_socket_directory(self) -> None:
        """Reconnect promptly when the daemon socket reappears (G1)."""
        try:
            socket_path = Path(self.config.socket_path)
            monitor = Gio.File.new_for_path(str(socket_path.parent)).monitor_directory(
                Gio.FileMonitorFlags.NONE, None)

            def changed(_monitor, file, _other, event_type) -> None:
                if (event_type == Gio.FileMonitorEvent.CREATED
                        and file is not None and file.get_basename() == socket_path.name
                        and self.reconnect_event is not None):
                    self.reconnect_event.set()

            monitor.connect("changed", changed)
            self._socket_monitor = monitor
        except Exception as exc:
            LOG.debug("Socket directory monitor unavailable: %s", exc)
            self._socket_monitor = None

    def _wait_before_retry(self, seconds: float) -> None:
        event = self.reconnect_event
        if event is None:
            time.sleep(seconds)
            return
        event.wait(seconds)
        event.clear()

    @staticmethod
    def _is_second_instance(exc: BaseException) -> bool:
        return isinstance(exc, ValueError) and "controller is already connected" in str(exc)

    def _subscription_worker(self) -> None:
        """Keep the controller subscription alive; never die silently (G1).

        Only state transitions are logged: the first failure, one reminder per
        DISCONNECTED_REMINDER_SECONDS while still down, and the reconnect.
        """
        delay = 1.0
        last_reminder = 0.0
        while self.running:
            try:
                self._subscribe_once()
                delay = 1.0
            except Exception as exc:  # noqa: BLE001 - this thread must survive
                if not self.running:
                    break
                now = time.monotonic()
                second_instance = self._is_second_instance(exc)
                if self.down_since is None:
                    self.down_since = now
                    last_reminder = now
                    delay = 1.0
                    if second_instance:
                        LOG.warning("Another Input Lock tray is already the controller; "
                                    "this instance retries every %d s", SECOND_INSTANCE_RETRY_SECONDS)
                    elif isinstance(exc, (OSError, ValueError)):
                        LOG.warning("Daemon subscription unavailable: %s", exc)
                    else:
                        LOG.exception("Daemon subscription failed unexpectedly")
                    GLib.idle_add(self._show_disconnected,
                                  "another-tray" if second_instance else None)
                elif now - last_reminder >= DISCONNECTED_REMINDER_SECONDS:
                    last_reminder = now
                    LOG.warning("Daemon subscription still unavailable after %d s: %s",
                                int(now - self.down_since), exc)
                self._wait_before_retry(SECOND_INSTANCE_RETRY_SECONDS if second_instance else delay)
                delay = min(delay * 2.0, 15.0)
        LOG.info("Daemon subscription worker stopped")

    def _subscription_up(self) -> None:
        down_since = self.down_since
        self.down_since = None
        if down_since is not None:
            LOG.info("Daemon subscription reconnected after %d s", int(time.monotonic() - down_since))

    def _subscribe_once(self) -> None:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(10.0)
        try:
            client.connect(str(self.config.socket_path))
            request = json.dumps({"cmd": "subscribe", "controller": True}, separators=(",", ":"))
            client.sendall(request.encode("utf-8") + b"\n")
            self.subscription_socket = client
            client.settimeout(1.0)
            heartbeat_at = time.monotonic() + 10.0
            buffer = bytearray()
            while self.running:
                if time.monotonic() >= heartbeat_at:
                    client.sendall(b'{"cmd":"heartbeat"}\n')
                    heartbeat_at = time.monotonic() + 10.0
                try:
                    data = client.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    raise OSError("daemon subscription ended")
                buffer.extend(data)
                if len(buffer) > MAX_MESSAGE_BYTES * 2:
                    raise ValueError("notification stream exceeded safety limit")
                while b"\n" in buffer:
                    raw, _separator, remainder = bytes(buffer).partition(b"\n")
                    buffer = bytearray(remainder)
                    message = json.loads(raw.decode("utf-8"))
                    if isinstance(message, dict) and message.get("ok") is False:
                        raise ValueError(str(message.get("error", "subscription rejected")))
                    if isinstance(message, dict):
                        if message.get("subscribed") is True:
                            self._subscription_up()
                        GLib.idle_add(self._handle_message, message)
        finally:
            self.subscription_socket = None
            client.close()

    def _handle_message(self, message: dict[str, object]) -> bool:
        state: object
        reason = ""
        if message.get("subscribed") is True:
            state = message.get("state")
        elif message.get("type") == "event" and message.get("event") == "state-changed":
            state = message.get("state")
            reason = str(message.get("reason", ""))
        else:
            return False
        if not isinstance(state, dict):
            return False

        prior_safety_value = self.current_state.get("safety", {})
        prior_safety = prior_safety_value.get("state") if isinstance(prior_safety_value, dict) else None
        # A loss noticed while disconnected was already reported by
        # _show_disconnected(); only compare against live, connected state.
        prior_lid = self.connected and self.current_state.get("ignore_lid_close") is True
        self.connected = True
        self.state_stale = False
        self.current_state = state
        lid_lost_notified = False
        if prior_lid and state.get("ignore_lid_close") is not True:
            lid_lost_notified = self._on_lid_ignore_lost(state, reason)
        self._reconcile_idle_guard(state)
        locked_value = state.get("locked_kinds", [])
        if not isinstance(locked_value, list):
            locked_value = []
        locked = frozenset(str(value) for value in locked_value)
        hotkey = str(state.get("primary_hotkey", "CTRL+ALT+Z"))
        self._update_menu(locked, hotkey)


        if locked != self.previous_locked and self.previous_locked is not None:
            self._notify_state(locked, reason, hotkey)
        self.previous_locked = locked
        safety = state.get("safety", {})
        safety_state = safety.get("state") if isinstance(safety, dict) else None
        if safety_state != prior_safety and safety_state in {
            "LID_CLOSED_WARNING", "PROTECTIVE_ACTION_PENDING",
            "PROTECTIVE_ACTION_FAILED", "PROTECTIVE_ACTION_EXECUTED",
        } and not (lid_lost_notified and safety_state == "PROTECTIVE_ACTION_PENDING"):
            message = "Safety warning: check heat, battery or missing telemetry. Open the lid if needed."
            if safety_state == "PROTECTIVE_ACTION_PENDING":
                decision = safety.get("last_decision")
                cause = decision.get("reason") if isinstance(decision, dict) else "unknown safety reason"
                message = f"Safety protection requested ({cause}). Lid-ignore released."
            elif safety_state == "PROTECTIVE_ACTION_EXECUTED":
                message = "A protective suspend was confirmed. Re-enable lid-ignore only with lid open."
            elif safety_state == "PROTECTIVE_ACTION_FAILED":
                message = "Open the lid. Automatic safety protection failed; see InputLock diagnostics."
            self._notify("Lid safety", message, locked=bool(locked))
        return False

    # ------------------------------------------------------------------
    # Ignore Lid Close loss (A8, A4) and idle grace (U3)
    # ------------------------------------------------------------------
    @staticmethod
    def _lid_disable_info(state: dict[str, object], event_reason: str) -> tuple[str, bool]:
        """Return (reason, requested_by_user) for an observed True->False change."""
        reason = event_reason
        requested: bool | None = None
        info = state.get("last_lid_disable")
        if isinstance(info, dict):
            if isinstance(info.get("reason"), str) and info["reason"]:
                reason = info["reason"]
            if isinstance(info.get("requested_by_user"), bool):
                requested = info["requested_by_user"]
        if requested is None:
            requested = reason in USER_DISABLE_REASONS
        return reason, requested

    def _on_lid_ignore_lost(self, state: dict[str, object], event_reason: str) -> bool:
        """Handle Ignore Lid Close going True->False; return True if notified.

        Never re-arms. An unrequested loss gets a critical, persistent
        notification naming the cause, with a "Re-enable…" action that goes
        through the normal confirmation dialog (and needs an open lid).
        """
        self.cooling_verified = False
        reason, requested = self._lid_disable_info(state, event_reason)
        own = self.own_disable_reason
        self.own_disable_reason = None
        if own is not None:
            # The tray failed closed itself and already showed the critical
            # notification with the real cause; the daemon sees that request
            # as an ordinary "off".
            LOG.warning("Ignore Lid Close turned off by the tray's own fail-safe (%s)", own)
            return True
        if requested:
            LOG.info("Ignore Lid Close turned off on request (reason=%s)", reason)
            return False
        until = self.tray_unlock_until
        if (reason == "ipc-emergency-unlock" and until is not None
                and time.monotonic() <= until):
            self.tray_unlock_until = None
            LOG.info("Ignore Lid Close turned off by the tray's Unlock/Quit action")
            self._notify(LID_LOST_TITLE, TRAY_UNLOCK_LID_NOTE,
                         locked=bool(state.get("locked_kinds")))
            return True
        LOG.warning("Ignore Lid Close was turned off without a user request: reason=%s", reason)
        self._notify_lid_lost(describe_lid_disable(reason, state), state)
        self._start_idle_grace(state)
        return True

    def _notify_lid_lost(self, text: str, state: dict[str, object]) -> None:
        try:
            self._notify(LID_LOST_TITLE, text + "\n" + REENABLE_HINT,
                         locked=bool(state.get("locked_kinds")), critical=True, reenable=True)
        except Exception as exc:
            LOG.warning("Lid-ignore loss notification failed: %s", exc)

    def _start_idle_grace(self, state: dict[str, object]) -> None:
        """Keep the GNOME idle-only inhibitor ~60 s after a loss, lid open."""
        if state.get("lid_is_closed") is True:
            return
        self.grace_until = time.monotonic() + IDLE_GRACE_SECONDS
        if self.grace_source:
            GLib.source_remove(self.grace_source)
        self.grace_source = GLib.timeout_add_seconds(IDLE_GRACE_SECONDS, self._idle_grace_expired)

    def _cancel_idle_grace(self) -> None:
        self.grace_until = None
        if self.grace_source:
            GLib.source_remove(self.grace_source)
        self.grace_source = 0

    def _idle_grace_expired(self) -> bool:
        self.grace_source = 0
        self.grace_until = None
        if self.connected and self.current_state.get("ignore_lid_close") is True:
            return False  # re-enabled by the user: the lease is in use again
        if self.enable_in_flight:
            return False
        self._run_lease(self._release_leases_job, True, False)
        return False

    # ------------------------------------------------------------------
    # Lease reconciliation (A7, U3, U4, U6) — blocking work on lease_worker
    # ------------------------------------------------------------------
    def _run_lease(self, function, *args) -> None:
        worker = self.lease_worker
        if worker is None:
            function(*args)
        else:
            worker.submit(function, *args)

    def _to_main(self, function, *args) -> None:
        if self.lease_worker is None:
            function(*args)
        else:
            GLib.idle_add(function, *args)

    def _reconcile_idle_guard(self, state: dict[str, object]) -> None:
        """Hold reversible cooling + GNOME-idle leases during lid-ignore only.

        GNOME idle and the power-saver hold are not system sleep inhibitors.
        On a lease failure (after A7's retry) drop the leases and disable the
        lid override: never trade away thermal protection just to stay awake.
        The blocking D-Bus work runs on the lease worker, not the GTK thread.
        """
        if self.idle_guard is None:
            return
        if state.get("ignore_lid_close") is not True:
            self.cooling_verified = False
            self.idle_guard_failed_generation = None
            if self.enable_in_flight:
                # U6: the hold taken before an enable request must survive
                # until that request has been answered.
                return
            keep_idle = self.grace_until is not None
            self._run_lease(self._release_leases_job, not keep_idle, True)
            return
        self._cancel_idle_grace()
        self._queue_lease_acquire(state)

    def _queue_lease_acquire(self, state: dict[str, object]) -> None:
        if self.lease_acquire_pending:
            return  # coalesce: one queued check is enough
        self.lease_acquire_pending = True
        self._run_lease(self._lease_acquire_job, state.get("lid_generation"),
                        bool(state.get("locked_kinds")))

    def _release_leases_job(self, idle: bool, cooling: bool) -> None:
        if idle and self.idle_guard is not None:
            self.idle_guard.release()
            self.idle_held = False
        if cooling and self.power_guard is not None:
            self.power_guard.release()

    def _lease_acquire_job(self, generation: int | None, locked: bool) -> None:
        self.lease_acquire_pending = False
        guard, cooling = self.idle_guard, self.power_guard
        stage = "cooling"
        try:
            # Throttle CPU spikes as soon as the user explicitly arms lid-ignore
            # rather than awaiting the first hot sample with the lid closed.
            restored = bool(cooling.acquire()) if cooling is not None else False
            stage = "idle"
            guard.acquire()
            self.idle_held = True
        except Exception as exc:
            if cooling is not None:
                cooling.release()
            # U3: if only cooling failed and the idle inhibitor is still held
            # with the lid open, keep it through the grace period.
            keep_idle = (stage == "cooling" and self.idle_held
                         and self.current_state.get("lid_is_closed") is not True)
            if not keep_idle:
                guard.release()
                self.idle_held = False
            self._to_main(self._lease_failed, generation, exc, locked, keep_idle, stage)
            return
        self._to_main(self._lease_ok, generation, restored, locked)

    def _lease_ok(self, generation: int | None, restored: bool, locked: bool) -> bool:
        self.idle_guard_failed_generation = None
        self.cooling_verified = self.power_guard is not None
        self._refresh_safety_label()
        if restored and self.current_state.get("ignore_lid_close") is True:
            LOG.warning("Power-saver hold restored after an external release (generation %s)", generation)
            self._notify(POWER_RESTORED_TITLE, POWER_RESTORED_BODY, locked=locked)
        return False

    def _lease_failed(self, generation: int | None, error: Exception, locked: bool,
                      idle_kept: bool = False, stage: str = "idle") -> bool:
        self.cooling_verified = False
        LOG.error("Continuous lid safety leases unavailable: %s", error)
        current_generation = self.current_state.get("lid_generation", generation)
        if current_generation != generation:
            # Stale result: lid-ignore already changed since this check.
            return False
        if self.idle_guard_failed_generation == generation:
            return False
        self.idle_guard_failed_generation = generation
        key = "power-saver-ineffective" if stage == "cooling" else "lease-unavailable"
        self.own_disable_reason = key
        # Do not allow a stuck/broken desktop notification to delay the
        # actual fail-safe disable of continuous lid-ignore.
        threading.Thread(
            target=self._idle_guard_safety_disable,
            args=(generation,),
            name="input-lock-idle-safety-disable",
            daemon=True,
        ).start()
        try:
            self._notify(
                LID_LOST_TITLE,
                describe_lid_disable(key, self.current_state, detail=str(error)) + "\n" + REENABLE_HINT,
                locked=locked, critical=True, reenable=True,
            )
        except Exception as notification_error:
            LOG.warning("Safety disable started but notification failed: %s",
                        notification_error)
        if idle_kept:
            self._start_idle_grace(self.current_state)
        return False

    def _on_power_hold_released(self) -> None:
        """PPD dropped our hold (manual Power Mode change): re-hold at once."""
        if self.enable_in_flight or self.current_state.get("ignore_lid_close") is True:
            self.cooling_verified = False
            self._queue_lease_acquire(self.current_state)

    def _idle_guard_safety_disable(self, generation: int | None) -> None:
        """Fail closed if the cooling or GNOME idle service cannot honor policy.

        If the daemon rejects the guarded off command, disconnect our own
        controller. The daemon's existing CONTROLLER_LOST fail-safe can then
        release lid suppression and request ordinary sleep on a closed lid.
        """
        try:
            response_ok(send_request(
                {"cmd": "set-lid-ignore", "enabled": False, "generation": generation},
                socket_path=self.config.socket_path, timeout=5.0))
        except Exception as exc:
            LOG.error("Could not disable unsupported continuous lid-ignore: %s", exc)
            client = getattr(self, "subscription_socket", None)
            if client is not None:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            GLib.idle_add(
                self._action_error, "Lid Safety",
                RuntimeError("Continuous lid safety prerequisites failed; controller released. Open the lid."),
            )

    def _safety_label(self) -> str | None:
        safety = self.current_state.get("safety", {})
        if not isinstance(safety, dict):
            return None
        label = SAFETY_STATE_NAMES.get(safety.get("state"), "unavailable")
        if self.current_state.get("ignore_lid_close"):
            # U2: claim active cooling only once the hold has been verified.
            label += (" (continuous; power-saver cooling active)" if self.cooling_verified
                      else " (continuous; power-saver cooling not confirmed)")
        return ("Lid safety: " + label + " | Temperature: "
                + str(safety.get("thermal", "UNKNOWN"))
                + " | Protection: " + str(safety.get("thermal_protection", "UNKNOWN")))

    def _refresh_safety_label(self) -> None:
        item = getattr(self, "safety_item", None)
        if item is None or not self.connected:
            return
        label = self._safety_label()
        if label is not None:
            item.set_label(label)

    def _update_menu(self, locked: frozenset[str], hotkey: str) -> None:
        label = self._safety_label()
        if label is not None:
            self.safety_item.set_label(label)
        ignore_lid = self.current_state.get("ignore_lid_close") is True
        policy_healthy = self.current_state.get("lid_policy_healthy") is True
        if locked:
            self.status_item.set_label("Input Lock — Locked")
            self.toggle_item.set_label("Unlock Input")
            icon = (
                "input-lock-locked-lid-ignored"
                if ignore_lid
                else "input-lock-locked"
            )
            description = (
                "Input is locked; lid close is ignored"
                if ignore_lid
                else "Input is locked"
            )
        else:
            self.status_item.set_label("Input Lock — Unlocked")
            self.toggle_item.set_label("Lock Input")
            icon = (
                "input-lock-unlocked-lid-ignored"
                if ignore_lid
                else "input-lock-unlocked"
            )
            description = (
                "Input is unlocked; lid close is ignored"
                if ignore_lid
                else "Input is unlocked"
            )
        self.indicator.set_icon_full(icon, description)
        self.shortcut_item.set_label(f"Shortcut: {self._display_hotkey(hotkey)}")
        self.updating_lid_item = True
        try:
            self.lid_item.set_active(ignore_lid)
            self.lid_item.set_label(
                CONTINUOUS_LID_LABEL
                if not ignore_lid or policy_healthy
                else LID_SAFETY_UNAVAILABLE_LABEL
            )
            lid_count = self.current_state.get("lid_switch_devices", 0)
            self.lid_item.set_sensitive(
                isinstance(lid_count, int) and lid_count > 0 and not self.enable_in_flight)
        finally:
            self.updating_lid_item = False
        self.toggle_item.set_sensitive(True)

    def _show_disconnected(self, detail: str | None = None) -> bool:
        """Daemon unreachable: never claim lid-ignore is active (U1)."""
        lid_was_on = self.connected and self.current_state.get("ignore_lid_close") is True
        self.connected = False
        self.state_stale = True
        self.cooling_verified = False
        if self.current_state:
            # Keep display-only values (hotkeys) but never a stale lid state.
            self.current_state = {**self.current_state, "ignore_lid_close": False,
                                  "lid_no_suspend_effective": False, "stale": True}
        if lid_was_on:
            own = self.own_disable_reason
            self.own_disable_reason = None
            if own is None:
                LOG.warning("Ignore Lid Close lost: daemon connection lost")
                self._notify_lid_lost(describe_lid_disable("daemon-unavailable"), self.current_state)
                self._start_idle_grace(self.current_state)
        if self.idle_guard is not None and not self.enable_in_flight:
            keep_idle = self.grace_until is not None
            self._run_lease(self._release_leases_job, not keep_idle, True)
        self.indicator.set_icon_full(DISCONNECTED_ICON, "Input Lock service unavailable")
        if detail == "another-tray":
            self.status_item.set_label("Input Lock — Another tray is in control")
            self.safety_item.set_label("Lid safety: managed by the other Input Lock tray")
        else:
            self.status_item.set_label("Input Lock — Service unavailable")
            self.safety_item.set_label("Lid safety: disconnected — status unknown; open the lid")
        self.toggle_item.set_sensitive(False)
        self._restore_lid_item()
        return False

    def _action_error(self, title: str, error: Exception) -> bool:
        if isinstance(error, OSError) and error.errno in {errno.ENOENT, errno.ECONNREFUSED}:
            self._show_disconnected()
            return self._show_error(title, "InputLock background service unavailable. Open the lid and check "
                                    "input-lock.service with systemctl status input-lock.service. "
                                    "After the service is repaired, the tray will reconnect automatically.")
        self.toggle_item.set_sensitive(getattr(self, "connected", False))
        return self._show_error(title, str(error))

    def _mark_tray_unlock(self) -> None:
        # emergency-unlock also ends lid-ignore; when the tray sent it on the
        # user's behalf, that loss is user-initiated, not a critical surprise.
        self.tray_unlock_until = time.monotonic() + 10.0

    def _on_toggle(self, _item: Gtk.MenuItem) -> None:
        self.toggle_item.set_sensitive(False)
        unlocking = bool(self.current_state.get("locked_kinds"))
        if unlocking:
            self._mark_tray_unlock()
        threading.Thread(
            target=self._toggle_worker,
            args=(unlocking,),
            name="input-lock-tray-action",
            daemon=True,
        ).start()

    def _toggle_worker(self, unlocking: bool) -> None:
        try:
            response_ok(
                send_request(
                    {"cmd": "emergency-unlock"} if unlocking else {"cmd": "lock-default"},
                    socket_path=self.config.socket_path,
                    timeout=5.0,
                )
            )
        except Exception as exc:
            GLib.idle_add(self._action_error, "Input Lock", exc)

    @staticmethod
    def _event_hotkey(event: Gdk.EventKey) -> str | None:
        key_name = Gdk.keyval_name(event.keyval)
        if not key_name or key_name in {
            "Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
            "Super_L", "Super_R", "Meta_L", "Meta_R",
        }:
            return None
        if len(key_name) == 1 and key_name.isalnum():
            key = key_name.upper()
        elif key_name.startswith("F") and key_name[1:].isdigit() and 1 <= int(key_name[1:]) <= 12:
            key = key_name.upper()
        else:
            return ""

        state = event.state
        modifiers: list[str] = []
        if state & Gdk.ModifierType.CONTROL_MASK:
            modifiers.append("CTRL")
        if state & Gdk.ModifierType.MOD1_MASK:
            modifiers.append("ALT")
        if state & Gdk.ModifierType.SHIFT_MASK:
            modifiers.append("SHIFT")
        if state & Gdk.ModifierType.SUPER_MASK:
            modifiers.append("SUPER")
        if len(modifiers) < 2:
            return ""
        return "+".join([*modifiers, key])

    def _on_settings(self, _item: Gtk.MenuItem) -> None:
        dialog = Gtk.Dialog(title="Input Lock Settings", flags=Gtk.DialogFlags.MODAL)
        dialog.set_default_size(480, 380)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        dialog.set_keep_above(True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.APPLY)

        box = dialog.get_content_area()
        box.set_spacing(12)
        box.set_border_width(18)

        title = Gtk.Label()
        title.set_markup("<b>Lock these devices</b>")
        title.set_xalign(0)
        box.add(title)

        configured = self.current_state.get("default_locked_kinds", [])
        if not isinstance(configured, list):
            configured = ["keyboard", "mouse", "touchpad", "touchscreen"]
        keyboard = Gtk.CheckButton(label="Keyboard")
        pointer = Gtk.CheckButton(label="Mouse and touchpad")
        touchscreen = Gtk.CheckButton(label="Touchscreen")
        keyboard.set_active("keyboard" in configured)
        pointer.set_active("mouse" in configured or "touchpad" in configured)
        touchscreen.set_active("touchscreen" in configured)
        for control in (keyboard, pointer, touchscreen):
            box.add(control)

        box.add(Gtk.Separator())
        shortcut = Gtk.Label(
            label="Shortcut: "
            + self._display_hotkey(
                str(self.current_state.get("primary_hotkey", "CTRL+ALT+Z"))
            )
        )
        shortcut.set_xalign(0)
        shortcut_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        shortcut_row.pack_start(shortcut, True, True, 0)
        change_shortcut = Gtk.Button(label="Change Shortcut…")
        change_shortcut.connect("clicked", lambda _button: dialog.response(1001))
        shortcut_row.pack_end(change_shortcut, False, False, 0)
        box.add(shortcut_row)

        raw_allowed = self.current_state.get("allowed_keys", [])
        allowed_keys = list(raw_allowed) if isinstance(raw_allowed, list) else []
        allowed_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        allowed_summary = Gtk.Label()
        allowed_summary.set_xalign(0)

        def refresh_allowed_summary() -> None:
            allowed_summary.set_text(
                "Keys allowed while locked: "
                + (f"{len(allowed_keys)} selected" if allowed_keys else "None")
            )

        refresh_allowed_summary()
        allowed_row.pack_start(allowed_summary, True, True, 0)
        allowed_button = Gtk.Button(label="Manage Keys…")
        allowed_button.connect("clicked", lambda _button: dialog.response(1002))
        allowed_row.pack_end(allowed_button, False, False, 0)
        box.add(allowed_row)

        startup = Gtk.CheckButton(label="Start Input Lock when I log in")
        startup.set_active(self.autostart_cached)
        box.add(startup)

        box.add(Gtk.Separator())
        idle_enabled = Gtk.CheckButton(label="Automatically lock after no activity")
        idle_enabled.set_active(
            self.current_state.get("idle_lock_enabled") is True
        )
        box.add(idle_enabled)

        idle_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        idle_label = Gtk.Label(label="No activity for")
        idle_label.set_xalign(0)
        idle_row.pack_start(idle_label, False, False, 0)
        idle_minutes = Gtk.Entry()
        idle_minutes.set_width_chars(4)
        idle_minutes.set_max_length(4)
        idle_minutes.set_alignment(0.5)
        idle_minutes.set_input_purpose(Gtk.InputPurpose.DIGITS)
        seconds = self.current_state.get("idle_lock_seconds", 600)
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            seconds = 600
        idle_minutes.set_text(str(max(1, min(1440, round(seconds / 60)))))
        decrease_idle = Gtk.Button(label="−")
        increase_idle = Gtk.Button(label="+")

        def idle_value() -> int:
            try:
                return max(1, min(1440, int(idle_minutes.get_text())))
            except ValueError:
                return 10

        def change_idle(_button: Gtk.Button, amount: int) -> None:
            idle_minutes.set_text(str(max(1, min(1440, idle_value() + amount))))

        decrease_idle.connect("clicked", change_idle, -1)
        increase_idle.connect("clicked", change_idle, 1)
        idle_row.pack_start(decrease_idle, False, False, 0)
        idle_row.pack_start(idle_minutes, False, False, 0)
        idle_row.pack_start(increase_idle, False, False, 0)
        idle_row.pack_start(Gtk.Label(label="minutes"), False, False, 0)
        box.add(idle_row)

        def update_idle_sensitivity(_button: Gtk.CheckButton | None = None) -> None:
            idle_row.set_sensitive(idle_enabled.get_active())

        idle_enabled.connect("toggled", update_idle_sensitivity)
        update_idle_sensitivity()

        box.add(Gtk.Separator())
        cursor_config = self._load_cursor_settings()
        hide_cursor = Gtk.CheckButton(label="Hide cursor after no activity")
        hide_cursor.set_active(cursor_config["enabled"])
        box.add(hide_cursor)

        # U5: cursor hiding is done by the GNOME Shell extension; say so when
        # it is not enabled instead of silently doing nothing.
        extension_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        extension_state = cursor_extension_enabled()
        extension_label = Gtk.Label(label=self._cursor_extension_text(extension_state))
        extension_label.set_xalign(0)
        extension_label.set_line_wrap(True)
        extension_row.pack_start(extension_label, True, True, 0)
        if extension_state is not True:
            enable_extension = Gtk.Button(label="Enable Extension")

            def on_enable_extension(button: Gtk.Button) -> None:
                button.set_sensitive(False)
                extension_label.set_text("Enabling the cursor extension…")
                threading.Thread(
                    target=self._enable_cursor_extension_worker,
                    args=(extension_label, button),
                    name="input-lock-cursor-extension",
                    daemon=True,
                ).start()

            enable_extension.connect("clicked", on_enable_extension)
            extension_row.pack_end(enable_extension, False, False, 0)
        box.add(extension_row)

        cursor_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        cursor_row.pack_start(Gtk.Label(label="Hide cursor after"), False, False, 0)
        cursor_minutes = Gtk.Entry()
        cursor_minutes.set_width_chars(3)
        cursor_minutes.set_max_length(4)
        cursor_minutes.set_alignment(0.5)
        cursor_minutes.set_input_purpose(Gtk.InputPurpose.DIGITS)
        cursor_seconds = Gtk.Entry()
        cursor_seconds.set_width_chars(3)
        cursor_seconds.set_max_length(2)
        cursor_seconds.set_alignment(0.5)
        cursor_seconds.set_input_purpose(Gtk.InputPurpose.DIGITS)
        total_cursor_seconds = cursor_config["seconds"]
        cursor_minutes.set_text(str(total_cursor_seconds // 60))
        cursor_seconds.set_text(str(total_cursor_seconds % 60))
        decrease_cursor_minutes = Gtk.Button(label="−")
        increase_cursor_minutes = Gtk.Button(label="+")
        decrease_cursor_seconds = Gtk.Button(label="−")
        increase_cursor_seconds = Gtk.Button(label="+")

        def bounded_entry_value(entry: Gtk.Entry, minimum: int, maximum: int) -> int:
            try:
                return max(minimum, min(maximum, int(entry.get_text())))
            except ValueError:
                return minimum

        def change_cursor_minutes(_button: Gtk.Button, amount: int) -> None:
            value = bounded_entry_value(cursor_minutes, 0, 1439)
            cursor_minutes.set_text(str(max(0, min(1439, value + amount))))

        def change_cursor_seconds(_button: Gtk.Button, amount: int) -> None:
            value = bounded_entry_value(cursor_seconds, 0, 59)
            cursor_seconds.set_text(str((value + amount) % 60))

        decrease_cursor_minutes.connect("clicked", change_cursor_minutes, -1)
        increase_cursor_minutes.connect("clicked", change_cursor_minutes, 1)
        decrease_cursor_seconds.connect("clicked", change_cursor_seconds, -1)
        increase_cursor_seconds.connect("clicked", change_cursor_seconds, 1)
        minutes_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        minutes_column.pack_start(increase_cursor_minutes, False, False, 0)
        minutes_column.pack_start(cursor_minutes, False, False, 0)
        minutes_column.pack_start(decrease_cursor_minutes, False, False, 0)
        minutes_column.pack_start(Gtk.Label(label="minutes"), False, False, 0)
        seconds_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        seconds_column.pack_start(increase_cursor_seconds, False, False, 0)
        seconds_column.pack_start(cursor_seconds, False, False, 0)
        seconds_column.pack_start(decrease_cursor_seconds, False, False, 0)
        seconds_column.pack_start(Gtk.Label(label="seconds"), False, False, 0)
        cursor_row.pack_start(minutes_column, False, False, 0)
        cursor_row.pack_start(seconds_column, False, False, 0)
        box.add(cursor_row)

        def update_cursor_sensitivity(_button: Gtk.CheckButton | None = None) -> None:
            cursor_row.set_sensitive(hide_cursor.get_active())

        hide_cursor.connect("toggled", update_cursor_sensitivity)
        update_cursor_sensitivity()

        warning = Gtk.Label()
        warning.set_xalign(0)
        box.add(warning)
        dialog.show_all()

        while True:
            response = dialog.run()
            if response == 1001:
                dialog.hide()
                self._on_change_shortcut(self.settings_item)
                if self.selected_hotkey:
                    shortcut.set_text(
                        "Shortcut: " + self._display_hotkey(self.selected_hotkey)
                    )
                dialog.show_all()
                continue
            if response == 1002:
                dialog.hide()
                updated = self._edit_allowed_keys(dialog, allowed_keys)
                if updated is not None:
                    allowed_keys[:] = updated
                    refresh_allowed_summary()
                dialog.show_all()
                continue
            if response != Gtk.ResponseType.APPLY:
                dialog.destroy()
                return
            kinds: list[str] = []
            if keyboard.get_active():
                kinds.append("keyboard")
            if pointer.get_active():
                kinds.extend(("mouse", "touchpad"))
            if touchscreen.get_active():
                kinds.append("touchscreen")
            if not kinds:
                warning.set_text("Select at least one device type.")
                continue
            start_at_login = startup.get_active()
            idle_lock_enabled = idle_enabled.get_active()
            try:
                minutes = int(idle_minutes.get_text())
            except ValueError:
                warning.set_text("Enter a whole number of minutes.")
                continue
            if not 1 <= minutes <= 1440:
                warning.set_text("Idle time must be between 1 and 1440 minutes.")
                continue
            idle_lock_seconds = minutes * 60
            try:
                hide_minutes = int(cursor_minutes.get_text())
                hide_seconds = int(cursor_seconds.get_text())
            except ValueError:
                warning.set_text("Enter whole numbers for cursor minutes and seconds.")
                continue
            if not 0 <= hide_minutes <= 1439 or not 0 <= hide_seconds <= 59:
                warning.set_text("Cursor time must use 0–1439 minutes and 0–59 seconds.")
                continue
            cursor_timeout = hide_minutes * 60 + hide_seconds
            if hide_cursor.get_active() and cursor_timeout < 1:
                warning.set_text("Cursor hiding time must be at least one second.")
                continue
            dialog.destroy()
            threading.Thread(
                target=self._settings_worker,
                args=(
                    kinds,
                    start_at_login,
                    idle_lock_enabled,
                    idle_lock_seconds,
                    hide_cursor.get_active(),
                    max(1, cursor_timeout),
                    allowed_keys,
                ),
                name="input-lock-settings",
                daemon=True,
            ).start()
            return

    def _settings_worker(
        self,
        kinds: list[str],
        start_at_login: bool,
        idle_lock_enabled: bool,
        idle_lock_seconds: int,
        hide_cursor_enabled: bool,
        hide_cursor_seconds: int,
        allowed_keys: list[str],
    ) -> None:
        try:
            response_ok(
                send_request(
                    {"cmd": "set-lock-kinds", "kinds": kinds},
                    socket_path=self.config.socket_path,
                    timeout=5.0,
                )
            )
            response_ok(
                send_request(
                    {"cmd": "set-allowed-keys", "keys": allowed_keys},
                    socket_path=self.config.socket_path,
                    timeout=5.0,
                )
            )
            self._save_cursor_settings(hide_cursor_enabled, hide_cursor_seconds)
            response_ok(
                send_request(
                    {
                        "cmd": "set-idle-lock",
                        "enabled": idle_lock_enabled,
                        "seconds": idle_lock_seconds,
                    },
                    socket_path=self.config.socket_path,
                    timeout=5.0,
                )
            )
        except Exception as exc:
            GLib.idle_add(self._show_error, "Settings Failed", str(exc))
            return
        if start_at_login != self._autostart_enabled():
            self._autostart_worker(start_at_login)
        else:
            GLib.idle_add(self._show_info, "Settings Saved", "Your settings were saved.")

    def _edit_allowed_keys(
        self, parent: Gtk.Dialog, initial: list[str]
    ) -> list[str] | None:
        dialog = Gtk.Dialog(title="Keys Allowed While Locked", transient_for=parent, flags=Gtk.DialogFlags.MODAL)
        dialog.set_default_size(480, 430)
        dialog.set_keep_above(True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Done", Gtk.ResponseType.APPLY)
        box = dialog.get_content_area()
        box.set_spacing(10)
        box.set_border_width(16)
        system_title = Gtk.Label(label="System recovery shortcuts (always available)")
        system_title.set_xalign(0)
        box.add(system_title)
        for text in (
            f"Primary: {self._display_hotkey(str(self.current_state.get('primary_hotkey', 'CTRL+ALT+Z')))}",
            f"Emergency: {self._display_hotkey(str(self.current_state.get('emergency_hotkey', 'CTRL+ALT+SHIFT+F12')))}",
            "TTY recovery: Ctrl+Alt+F1–F12",
        ):
            label = Gtk.Label(label=text)
            label.set_xalign(0)
            label.set_sensitive(False)
            box.add(label)
        box.add(Gtk.Separator())
        note = Gtk.Label(label="These individual keys continue to reach applications while the keyboard is locked.")
        note.set_xalign(0)
        note.set_line_wrap(True)
        box.add(note)
        key_list = Gtk.ListBox()
        key_list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(170)
        scroller.add(key_list)
        box.add(scroller)
        selected = list(initial)

        def rebuild() -> None:
            for child in key_list.get_children():
                key_list.remove(child)
            for key in selected:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                label = Gtk.Label(label=display_key(key))
                label.set_xalign(0)
                row.pack_start(label, True, True, 8)
                remove = Gtk.Button(label="Remove")
                remove.connect("clicked", lambda _button, value=key: (selected.remove(value), rebuild()))
                row.pack_end(remove, False, False, 4)
                key_list.add(row)
            key_list.show_all()

        def add_key(_button: Gtk.Button) -> None:
            capture = Gtk.Dialog(title="Add Allowed Key", transient_for=dialog, flags=Gtk.DialogFlags.MODAL)
            capture.set_default_size(360, 140)
            capture.add_button("Cancel", Gtk.ResponseType.CANCEL)
            message = Gtk.Label(label="Press one key. Modifier keys cannot be added.")
            message.set_margin_top(22)
            message.set_margin_bottom(22)
            capture.get_content_area().add(message)
            chosen: list[str] = []

            def pressed(_window: Gtk.Dialog, event: Gdk.EventKey) -> bool:
                name = Gdk.keyval_name(event.keyval) or ""
                # Current Copilot keyboards expose evdev KEY_F23 through XKB
                # hardware keycode 201.  The symbolic name varies between
                # XF86Assistant and XF86TouchpadOff as Super/Shift state changes.
                key = (
                    "COPILOT"
                    if int(event.hardware_keycode) == 201
                    else gtk_key_to_canonical(name)
                )
                if key is None:
                    message.set_text("That key is not supported. Try a letter, number, function or media key.")
                    return True
                primary_key = str(self.current_state.get("primary_hotkey", "CTRL+ALT+Z")).split("+")[-1]
                emergency_key = str(self.current_state.get("emergency_hotkey", "CTRL+ALT+SHIFT+F12")).split("+")[-1]
                if key in {primary_key, emergency_key} or key in {f"F{n}" for n in range(1, 13)}:
                    message.set_text("That key belongs to a fixed recovery shortcut.")
                    return True
                chosen.append(key)
                capture.response(Gtk.ResponseType.APPLY)
                return True

            capture.connect("key-press-event", pressed)
            capture.show_all()
            response = capture.run()
            capture.destroy()
            if response == Gtk.ResponseType.APPLY and chosen and chosen[0] not in selected:
                selected.append(chosen[0])
                rebuild()

        add = Gtk.Button(label="Add Key…")
        add.connect("clicked", add_key)
        box.add(add)
        rebuild()
        dialog.show_all()
        response = dialog.run()
        dialog.destroy()
        return selected if response == Gtk.ResponseType.APPLY else None

    @staticmethod
    def _cursor_extension_text(enabled: bool | None) -> str:
        if enabled is True:
            return "Cursor hiding extension: enabled."
        if enabled is False:
            return ("Cursor hiding extension: disabled — the cursor will not be hidden "
                    "until the Input Lock Cursor Hider GNOME extension is enabled.")
        return "Cursor hiding extension: status unknown (GNOME Shell settings unavailable)."

    @staticmethod
    def _enable_cursor_extension() -> tuple[bool, str]:
        """Run `gnome-extensions enable` (blocking; call from a worker)."""
        tool = shutil.which("gnome-extensions")
        if not tool:
            return False, "The gnome-extensions command is not installed."
        try:
            result = subprocess.run(
                [tool, "enable", CURSOR_EXTENSION_UUID],
                check=False, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or "gnome-extensions failed"
            return False, (detail + " If GNOME does not know the extension yet, log out and "
                           "back in once, then try again.")
        return True, "Cursor hiding extension: enabled."

    def _enable_cursor_extension_worker(self, label: Gtk.Label, button: Gtk.Button) -> None:
        ok, message = self._enable_cursor_extension()

        def finish() -> bool:
            try:
                label.set_text(message)
                button.set_sensitive(not ok)
            except Exception:
                pass  # the dialog was closed meanwhile
            return False

        GLib.idle_add(finish)

    @staticmethod
    def _load_cursor_settings() -> dict[str, object]:
        try:
            payload = json.loads(CURSOR_SETTINGS_PATH.read_text(encoding="utf-8"))
            enabled = payload.get("enabled", False)
            seconds = payload.get("seconds", DEFAULT_CURSOR_TIMEOUT_SECONDS)
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be a boolean")
            if isinstance(seconds, bool) or not isinstance(seconds, int):
                raise ValueError("seconds must be an integer")
            if not 1 <= seconds <= MAX_CURSOR_TIMEOUT_SECONDS:
                raise ValueError("seconds are outside the supported range")
            return {"enabled": enabled, "seconds": seconds}
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            return {"enabled": False, "seconds": DEFAULT_CURSOR_TIMEOUT_SECONDS}

    @staticmethod
    def _save_cursor_settings(enabled: bool, seconds: int) -> None:
        CURSOR_SETTINGS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = CURSOR_SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"enabled": enabled, "seconds": seconds}, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, CURSOR_SETTINGS_PATH)

    def _on_change_shortcut(self, _item: Gtk.MenuItem) -> None:
        dialog = Gtk.Dialog(title="Change Shortcut", flags=Gtk.DialogFlags.MODAL)
        dialog.set_default_size(420, 170)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        dialog.set_keep_above(True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        apply_button = dialog.add_button("Apply", Gtk.ResponseType.APPLY)
        apply_button.set_sensitive(False)

        box = dialog.get_content_area()
        box.set_spacing(12)
        box.set_border_width(18)
        label = Gtk.Label(label="Press a shortcut with at least two modifiers.\nSupported keys: A–Z, 0–9, and F1–F12.")
        label.set_justify(Gtk.Justification.CENTER)
        box.add(label)
        selected = Gtk.Label(label="Waiting for shortcut…")
        selected.get_style_context().add_class("title")
        box.add(selected)
        warning = Gtk.Label()
        box.add(warning)
        self.selected_hotkey = None

        def capture(_dialog: Gtk.Dialog, event: Gdk.EventKey) -> bool:
            if event.keyval == Gdk.KEY_Escape:
                dialog.response(Gtk.ResponseType.CANCEL)
                return True
            candidate = self._event_hotkey(event)
            if candidate is None:
                return False
            if not candidate:
                warning.set_text("Use at least two modifiers and one supported key.")
                apply_button.set_sensitive(False)
                return True
            emergency = str(self.current_state.get("emergency_hotkey", "CTRL+ALT+SHIFT+F12"))
            if candidate == emergency:
                warning.set_text("This shortcut is reserved for emergency unlock.")
                apply_button.set_sensitive(False)
                return True
            self.selected_hotkey = candidate
            selected.set_text(self._display_hotkey(candidate))
            warning.set_text("")
            apply_button.set_sensitive(True)
            return True

        dialog.connect("key-press-event", capture)
        dialog.show_all()
        response = dialog.run()
        candidate = self.selected_hotkey
        dialog.destroy()
        if response == Gtk.ResponseType.APPLY and candidate:
            threading.Thread(
                target=self._change_shortcut_worker,
                args=(candidate,),
                name="input-lock-shortcut-change",
                daemon=True,
            ).start()

    def _change_shortcut_worker(self, hotkey: str) -> None:
        try:
            result = subprocess.run(
                ["/usr/bin/input-lock", "set-hotkey", hotkey],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "Could not change the shortcut.")
            GLib.idle_add(
                self._show_info,
                "Shortcut Changed",
                f"The new shortcut is {self._display_hotkey(hotkey)}.",
            )
        except Exception as exc:
            GLib.idle_add(self._show_error, "Shortcut Change Failed", str(exc))

    def _reset_lid_item(self, item: Gtk.CheckMenuItem) -> None:
        self.updating_lid_item = True
        try:
            item.set_active(False)
        finally:
            self.updating_lid_item = False

    def _on_lid_toggled(self, item: Gtk.CheckMenuItem) -> None:
        if self.updating_lid_item:
            return
        enabled = item.get_active()
        if enabled:
            dialog = Gtk.MessageDialog(
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.YES_NO,
                text="Keep running with the lid closed?",
            )
            dialog.format_secondary_text(
                "Lid closure will be ignored continuously, with NO 2/4-minute timeout. "
                "This stays enabled until you turn it off, or a safety/fail-safe condition intervenes. "
                "Dangerous temperature, critical battery, lost monitoring or controller/daemon failure can request regular OS suspend. "
                "GNOME idle suspend is prevented and a temporary power-saver cooling profile is held while enabled; "
                "the previous performance preference automatically returns when disabled. "
                "Manual system sleep and OS critical-battery protection remain available. "
                "The setting starts OFF after a service restart or reboot. "
                + TRAY_STOP_NOTE + " "
                "Keep the lid OPEN when enabling, keep the vents unobstructed, and NEVER put the running laptop in a bag."
            )
            dialog.set_keep_above(True)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.YES:
                self._reset_lid_item(item)
                return
            if self.current_state.get("lid_is_closed") is True:
                self._reset_lid_item(item)
                self._show_error(LID_LABEL, "Open the lid first, then turn Ignore Lid Close on.")
                return
            if self.enable_in_flight:
                return
            # Acquire cooling BEFORE even requesting the lid inhibitor: the
            # user may physically close the lid immediately after accepting
            # the dialog. Both steps run on the lease worker (U4); the
            # reconcile path will not release this hold meanwhile (U6).
            self.enable_in_flight = True
            item.set_sensitive(False)
            self._run_lease(self._enable_job, self.current_state.get("lid_generation"))
            return
        item.set_sensitive(False)
        threading.Thread(
            target=self._lid_toggle_worker,
            args=(enabled, self.current_state.get("lid_generation")),
            name="input-lock-lid-change",
            daemon=True,
        ).start()

    def _enable_job(self, generation: int | None) -> None:
        cooling = self.power_guard
        try:
            if cooling is not None:
                cooling.acquire()
        except Exception as exc:
            self._to_main(self._enable_finished, False,
                          "Temporary power-saver cooling unavailable: " + str(exc), None)
            return
        try:
            # Captured generation prevents delayed UI work from undoing a later
            # disable/session loss. Only the daemon owns effective lid state.
            response_ok(send_request(
                {"cmd": "set-lid-ignore", "enabled": True, "generation": generation},
                socket_path=self.config.socket_path, timeout=5.0))
        except Exception as exc:
            # U6: never leave the pre-enable hold behind a failed request.
            if cooling is not None and self.current_state.get("ignore_lid_close") is not True:
                cooling.release()
            self._to_main(self._enable_finished, False, None, exc)
            return
        self._to_main(self._enable_finished, True, None, None)

    def _enable_finished(self, ok: bool, message: str | None, error: Exception | None) -> bool:
        self.enable_in_flight = False
        if ok:
            if self.current_state.get("ignore_lid_close") is True:
                self._reconcile_idle_guard(self.current_state)
            else:
                # The state event normally follows the reply; if it never
                # shows lid-ignore on, reconcile then (releasing the hold).
                GLib.timeout_add_seconds(3, self._reconcile_after_enable)
            self._restore_lid_item()
            return False
        self._restore_lid_item()
        if error is not None:
            self._action_error("Lid Setting Failed", error)
        else:
            self._show_error("Lid Safety", message or "Ignore Lid Close could not be turned on.")
        return False

    def _reconcile_after_enable(self) -> bool:
        if not self.enable_in_flight:
            self._reconcile_idle_guard(self.current_state)
        return False

    def _offer_reenable(self) -> bool:
        """Handle the "Re-enable…" action: same dialog, open lid, no auto-arm."""
        if not self.connected:
            self._show_error(LID_LABEL, "The Input Lock service is not available. "
                             "When the tray reconnects, turn Ignore Lid Close on from its menu.")
            return False
        state = self.current_state
        if state.get("ignore_lid_close") is True or self.enable_in_flight:
            return False
        if state.get("lid_is_closed") is True:
            self._show_error(LID_LABEL, "Open the lid first, then choose “Re-enable…” again "
                             "or use the Input Lock tray menu.")
            return False
        lid_count = state.get("lid_switch_devices", 0)
        if not isinstance(lid_count, int) or lid_count <= 0:
            self._show_error(LID_LABEL, "No lid switch is available right now.")
            return False
        # Emits "toggled" -> _on_lid_toggled -> the usual confirmation dialog.
        self.lid_item.set_active(True)
        return False

    def _lid_toggle_worker(self, enabled: bool, generation: int | None) -> None:
        try:
            # Captured generation prevents delayed UI work from undoing a later
            # disable/session loss. Only the daemon owns effective lid state.
            response_ok(send_request(
                {"cmd": "set-lid-ignore", "enabled": enabled, "generation": generation},
                socket_path=self.config.socket_path, timeout=5.0))
        except Exception as exc:
            GLib.idle_add(self._restore_lid_item)
            GLib.idle_add(self._action_error, "Lid Setting Failed", exc)


    def _restore_lid_item(self) -> bool:
        self.updating_lid_item = True
        try:
            if not self.connected:
                # U1: unchecked, insensitive, consistent wording.
                self.lid_item.set_active(False)
                self.lid_item.set_sensitive(False)
                self.lid_item.set_label(LID_UNAVAILABLE_LABEL)
                return False
            enabled = self.current_state.get("ignore_lid_close") is True
            self.lid_item.set_active(enabled)
            self.lid_item.set_label(
                CONTINUOUS_LID_LABEL
                if not enabled or self.current_state.get("lid_policy_healthy") is True
                else LID_SAFETY_UNAVAILABLE_LABEL
            )
            lid_count = self.current_state.get("lid_switch_devices", 0)
            self.lid_item.set_sensitive(
                isinstance(lid_count, int) and lid_count > 0 and not self.enable_in_flight)
        finally:
            self.updating_lid_item = False
        return False

    @staticmethod
    def _service_enabled(*command: str) -> bool:
        try:
            return subprocess.run(
                list(command), check=False, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5.0,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _autostart_enabled(self) -> bool:
        return self._service_enabled(
            "systemctl", "is-enabled", "--quiet", "input-lock.service"
        ) and self._service_enabled(
            "systemctl", "--user", "is-enabled", "--quiet",
            "input-lock-notifier.service",
        )

    def _on_autostart_toggled(self, item: Gtk.CheckMenuItem) -> None:
        if self.updating_autostart_item:
            return
        self.autostart_item.set_sensitive(False)
        threading.Thread(
            target=self._autostart_worker,
            args=(item.get_active(),),
            name="input-lock-autostart",
            daemon=True,
        ).start()

    def _autostart_worker(self, enabled: bool) -> None:
        action = "enable" if enabled else "disable"
        reverse_action = "disable" if enabled else "enable"
        user_changed = False
        error = ""
        try:
            user_result = subprocess.run(
                ["systemctl", "--user", action, "input-lock-notifier.service"],
                check=False, capture_output=True, text=True, timeout=15.0,
            )
            if user_result.returncode != 0:
                raise RuntimeError(
                    user_result.stderr.strip() or "could not update the user service"
                )
            user_changed = True
            system_result = subprocess.run(
                ["pkexec", "/usr/bin/systemctl", action, "input-lock.service"],
                check=False, capture_output=True, text=True, timeout=120.0,
            )
            if system_result.returncode != 0:
                raise RuntimeError(
                    system_result.stderr.strip()
                    or "administrator authentication was cancelled or failed"
                )
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            error = str(exc)
            if user_changed:
                subprocess.run(
                    [
                        "systemctl", "--user", reverse_action,
                        "input-lock-notifier.service",
                    ],
                    check=False, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=15.0,
                )
        GLib.idle_add(self._finish_autostart_change, error, self._autostart_enabled())

    def _refresh_autostart_async(self) -> None:
        """Read the start-at-login state off the GTK main thread (U4)."""
        def worker() -> None:
            GLib.idle_add(self._apply_autostart_state, self._autostart_enabled())

        threading.Thread(target=worker, name="input-lock-autostart-state", daemon=True).start()

    def _apply_autostart_state(self, enabled: bool) -> bool:
        self.autostart_cached = enabled
        self.updating_autostart_item = True
        try:
            self.autostart_item.set_active(enabled)
            self.autostart_item.set_sensitive(True)
        finally:
            self.updating_autostart_item = False
        return False

    def _finish_autostart_change(self, error: str, enabled: bool) -> bool:
        self._apply_autostart_state(enabled)
        if error:
            self._show_error("Startup Setting Failed", error)
        return False


    def _about_logo(self) -> GdkPixbuf.Pixbuf | None:
        installed = Path("/usr/share/pixmaps/input-lock-about.png")
        source = Path(__file__).resolve().parent.parent / "assets" / "aag-projects-team.png"
        logo_path = installed if installed.is_file() else source
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(logo_path))
            # Crop the large empty margins before scaling to preserve sharpness.
            cropped = pixbuf.new_subpixbuf(330, 235, 876, 455)
            return cropped.scale_simple(500, 260, GdkPixbuf.InterpType.HYPER)
        except GLib.Error as exc:
            LOG.warning("Unable to load About logo: %s", exc)
            return None

    def _on_about(self, _item: Gtk.MenuItem) -> None:
        dialog = Gtk.AboutDialog()
        dialog.set_program_name("Input Lock")
        dialog.set_version(VERSION)
        dialog.set_comments("Safe keyboard, mouse, touchpad, and touchscreen locking for Ubuntu.")
        logo = self._about_logo()
        if logo is not None:
            dialog.set_logo(logo)
        else:
            dialog.set_logo_icon_name("input-lock-unlocked")
        dialog.set_copyright("© AAG Projects Team")
        dialog.set_website("mailto:AAG.Projects.Team@gmail.com")
        dialog.set_website_label("AAG.Projects.Team@gmail.com")
        dialog.set_modal(True)
        dialog.set_keep_above(True)
        dialog.run()
        dialog.destroy()

    def _on_quit(self, _item: Gtk.MenuItem) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Quit Input Lock?",
        )
        dialog.format_secondary_text(
            "This will unlock all input and stop both the Input Lock service "
            "and its tray process."
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Quit", Gtk.ResponseType.OK)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self.quit_item.set_sensitive(False)
        self._mark_tray_unlock()
        threading.Thread(
            target=self._quit_worker,
            name="input-lock-tray-quit",
            daemon=True,
        ).start()

    def _quit_worker(self) -> None:
        try:
            response_ok(send_request({"cmd": "emergency-unlock"}, socket_path=self.config.socket_path, timeout=5.0))
            response_ok(send_request({"cmd": "quit"}, socket_path=self.config.socket_path, timeout=5.0))
        except Exception as exc:
            GLib.idle_add(self._show_error, "Input Lock", str(exc))
            GLib.idle_add(self.quit_item.set_sensitive, True)
            return
        self.running = False
        GLib.idle_add(Gtk.main_quit)


    @staticmethod
    def _display_hotkey(hotkey: str) -> str:
        names = {"CTRL": "Ctrl", "ALT": "Alt", "SHIFT": "Shift", "SUPER": "Super"}
        return "+".join(names.get(part, part) for part in hotkey.split("+"))

    @staticmethod
    def _message(kind: Gtk.MessageType, title: str, body: str) -> bool:
        dialog = Gtk.MessageDialog(
            flags=Gtk.DialogFlags.MODAL,
            message_type=kind,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(body)
        dialog.set_keep_above(True)
        dialog.run()
        dialog.destroy()
        return False

    def _show_error(self, title: str, body: str) -> bool:
        return self._message(Gtk.MessageType.ERROR, title, body)

    def _show_info(self, title: str, body: str) -> bool:
        return self._message(Gtk.MessageType.INFO, title, body)

    def _notify_state(self, locked: frozenset[str], reason: str, hotkey: str) -> None:
        if not self.config.show_notifications:
            return
        if not locked:
            key = "auto-unlocked" if reason == "auto-unlock-timer" else "unlocked"
        elif {"keyboard", "mouse", "touchpad"}.issubset(locked):
            key = "all-locked"
        elif locked == {"keyboard"}:
            key = "keyboard-locked"
        elif locked <= {"mouse", "touchpad"}:
            key = "mouse-locked"
        elif locked == {"touchscreen"}:
            key = "touchscreen-locked"
        else:
            key = "all-locked"
        title, body = TITLES[key]
        if locked:
            body = f"Press {self._display_hotkey(hotkey)} to unlock."
        self._notify(title, body, locked=bool(locked))

    def _notify(self, title: str, body: str, *, locked: bool,
                critical: bool = False, reenable: bool = False) -> None:
        """Queue a desktop notification; never blocks the GTK thread (U4)."""
        threading.Thread(
            target=self._notify_blocking,
            args=(title, body, locked, critical, reenable),
            name="input-lock-notify",
            daemon=True,
        ).start()

    @staticmethod
    def _notify_command(notify_send: str, title: str, body: str, locked: bool,
                        critical: bool, reenable: bool) -> list[str]:
        command = [
            notify_send,
            "--app-name=Input Lock",
            "--icon=input-lock-locked" if locked else "--icon=input-lock-unlocked",
        ]
        if critical:
            # Critical + no expiry: stays until the user dismisses it (A8).
            command += ["--urgency=critical", "--expire-time=0"]
            if reenable:
                command.append(f"--action=reenable={REENABLE_ACTION_LABEL}")
        else:
            command += ["--urgency=normal", "--expire-time=2500"]
        return [*command, title, body]

    def _notify_blocking(self, title: str, body: str, locked: bool,
                         critical: bool = False, reenable: bool = False) -> None:
        try:
            config = getattr(self, "config", None)
            if config is not None and config.play_sound:
                player = shutil.which("canberra-gtk-play")
                if player:
                    sound = "service-login" if locked else "service-logout"
                    subprocess.run(
                        [player, "--id", sound],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=4,
                    )
            notify_send = shutil.which("notify-send")
            if not notify_send:
                if critical:
                    LOG.warning("notify-send missing; critical notice not shown: %s: %s", title, body)
                return
            command = self._notify_command(notify_send, title, body, locked, critical, reenable)
            if critical and reenable:
                # --action implies --wait: this thread waits until the user
                # dismisses the notification or picks the action.
                result = subprocess.run(command, check=False, capture_output=True, text=True)
                if "reenable" in result.stdout.split():
                    GLib.idle_add(self._offer_reenable)
                return
            subprocess.run(command, check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=4)
        except Exception as exc:
            LOG.warning("Desktop notification failed: %s", exc)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    initialized = Gtk.init_check(None)
    display_ready = bool(initialized[0] if isinstance(initialized, tuple) else initialized)
    if not display_ready:
        # A user systemd manager may reach graphical-session.target before
        # GNOME has published DISPLAY/WAYLAND_DISPLAY.  Exit cleanly and let
        # Restart=on-failure retry instead of aborting inside GTK and leaving a
        # core dump.  The unit is also ordered after GNOME initialization.
        LOG.error("Graphical display is not ready; retrying after GNOME initialization")
        return os.EX_TEMPFAIL
    try:
        return TrayNotifier().run()
    except Exception:
        LOG.exception("Tray service failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
