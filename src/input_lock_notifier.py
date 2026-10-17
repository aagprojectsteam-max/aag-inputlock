#!/usr/bin/python3
"""Per-user tray indicator and notification bridge for Input Lock."""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
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
CONTINUOUS_LID_LABEL: Final = "Ignore Lid Close (continuous, safety-monitored)"
GNOME_IDLE_ONLY: Final = 8


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


class TrayNotifier:
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
        self.idle_guard_failed_generation: int | None = None

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
        self.lid_item.connect("toggled", self._on_lid_toggled)
        self.menu.append(self.lid_item)

        self.autostart_item = Gtk.CheckMenuItem(label="Start at Login")
        self.autostart_item.set_active(self._autostart_enabled())
        self.autostart_item.connect("toggled", self._on_autostart_toggled)

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
        Gtk.main()
        self.running = False
        self.idle_guard.release()
        return 0


    def _subscription_worker(self) -> None:
        delay = 1.0
        while self.running:
            try:
                self._subscribe_once()
                delay = 1.0
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                if not self.running:
                    break
                LOG.warning("Daemon subscription unavailable: %s", exc)
                GLib.idle_add(self._show_disconnected)
                time.sleep(delay)
                delay = min(delay * 2.0, 15.0)

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

        prior_safety = self.current_state.get("safety", {}).get("state")
        self.connected = True
        self.current_state = state
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
        }:
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

    def _reconcile_idle_guard(self, state: dict[str, object]) -> None:
        """Suppress only automatic GNOME idle sleep while lid-ignore is active.

        All explicit and emergency suspend requests remain possible.
        If GNOME idle protection cannot run, fail closed: disable lid-ignore
        rather than advertising indefinite wakefulness without a guarantee.
        """
        guard = getattr(self, "idle_guard", None)
        if guard is None:
            return
        if state.get("ignore_lid_close") is not True:
            guard.release()
            self.idle_guard_failed_generation = None
            return
        try:
            guard.acquire()
            self.idle_guard_failed_generation = None
        except Exception as exc:
            generation = state.get("lid_generation")
            LOG.error("GNOME idle inhibition unavailable: %s", exc)
            if getattr(self, "idle_guard_failed_generation", None) == generation:
                return
            self.idle_guard_failed_generation = generation
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
                    "Lid safety",
                    "GNOME idle prevention failed. Lid-ignore is being disabled; open the lid.",
                    locked=bool(state.get("locked_kinds")),
                )
            except Exception as notification_error:
                LOG.warning("Safety disable started but notification failed: %s",
                            notification_error)

    def _idle_guard_safety_disable(self, generation: int | None) -> None:
        """Fail closed if the session bus cannot honor the continuous policy.

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
                RuntimeError("Idle supervision failed; InputLock controller was released. Open the lid."),
            )

    def _update_menu(self, locked: frozenset[str], hotkey: str) -> None:
        safety = self.current_state.get("safety", {})
        if isinstance(safety, dict):
            names = {"SAFE_NORMAL": "normal OS behavior", "RECOVERY": "recovered; re-arm required",
                     "LID_IGNORE_ARMED": "armed; lid open", "LID_CLOSED_MONITORED": "monitoring closed lid",
                     "LID_CLOSED_WARNING": "safety warning — inspect heat/battery/telemetry",
                     "LID_CLOSED_HIGH_RISK": "high risk — open the lid",
                     "PROTECTIVE_ACTION_FAILED": "protection FAILED — open the lid",
                     "PROTECTIVE_ACTION_PENDING": "protective action requested", "PROTECTIVE_ACTION_EXECUTED": "suspend transition confirmed"}
            label = names.get(safety.get("state"), "unavailable")
            if self.current_state.get("ignore_lid_close"):
                label += " (continuous; no timer)"
            self.safety_item.set_label("Lid safety: " + label + " | Temperature: "
                                       + str(safety.get("thermal", "UNKNOWN"))
                                       + " | Protection: " + str(safety.get("thermal_protection", "UNKNOWN")))
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
        self.lid_item.set_active(ignore_lid)
        self.lid_item.set_label(
            CONTINUOUS_LID_LABEL
            if not ignore_lid or policy_healthy
            else "Ignore Lid Close (safety unavailable)"
        )
        lid_count = self.current_state.get("lid_switch_devices", 0)
        self.lid_item.set_sensitive(isinstance(lid_count, int) and lid_count > 0)
        self.updating_lid_item = False
        self.toggle_item.set_sensitive(True)

    def _show_disconnected(self) -> bool:
        guard = getattr(self, "idle_guard", None)
        if guard is not None:
            guard.release()
        self.connected = False
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

    def _on_toggle(self, _item: Gtk.MenuItem) -> None:
        self.toggle_item.set_sensitive(False)
        threading.Thread(
            target=self._toggle_worker,
            args=(bool(self.current_state.get("locked_kinds")),),
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
        startup.set_active(self._autostart_enabled())
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
                "GNOME automatic idle suspend is prevented while enabled, but manual system sleep and OS critical-battery rules remain available. "
                "The setting starts OFF after a service restart or reboot. "
                "Keep the lid OPEN when enabling, keep the vents unobstructed, and NEVER put the running laptop in a bag."
            )
            dialog.set_keep_above(True)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.YES:
                self.updating_lid_item = True
                item.set_active(False)
                self.updating_lid_item = False
                return
        item.set_sensitive(False)
        threading.Thread(
            target=self._lid_toggle_worker,
            args=(enabled, self.current_state.get("lid_generation")),
            name="input-lock-lid-change",
            daemon=True,
        ).start()

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
        if not getattr(self, "connected", False):
            self.lid_item.set_sensitive(False)
            self.lid_item.set_label("Ignore Lid Switch (service unavailable)")
            return False
        enabled = self.current_state.get("ignore_lid_close") is True
        self.updating_lid_item = True
        self.lid_item.set_active(enabled)
        self.lid_item.set_label(
            CONTINUOUS_LID_LABEL
            if not enabled or self.current_state.get("lid_policy_healthy") is True
            else "Ignore Lid Close (safety unavailable)"
        )
        lid_count = self.current_state.get("lid_switch_devices", 0)
        self.lid_item.set_sensitive(isinstance(lid_count, int) and lid_count > 0)
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
        GLib.idle_add(self._finish_autostart_change, error)

    def _finish_autostart_change(self, error: str) -> bool:
        self.updating_autostart_item = True
        self.autostart_item.set_active(self._autostart_enabled())
        self.autostart_item.set_sensitive(True)
        self.updating_autostart_item = False
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

    def _notify(self, title: str, body: str, *, locked: bool) -> None:
        notify_send = shutil.which("notify-send")
        if notify_send:
            subprocess.run(
                [
                    notify_send,
                    "--app-name=Input Lock",
                    "--icon=input-lock-locked" if locked else "--icon=input-lock-unlocked",
                    "--urgency=normal",
                    "--expire-time=2500",
                    title,
                    body,
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=4,
            )
        if self.config.play_sound:
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
