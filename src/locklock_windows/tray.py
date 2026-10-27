"""Windows notification-area menu and Tk settings UI."""

from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

import pystray
from PIL import Image, ImageDraw

from locklock_core import ALLOWED_KEY_CATALOG, UserSettings, display_key, parse_hotkey
from locklock_windows import startup


def _icon_image(locked: bool) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = "#dc3545" if locked else "#2aa876"
    draw.rounded_rectangle((8, 25, 56, 59), radius=8, fill=color)
    draw.arc((18, 6, 46, 39), 180, 360, fill=color, width=7)
    draw.ellipse((29, 36, 35, 42), fill="white")
    draw.rectangle((31, 40, 33, 49), fill="white")
    return image


class WindowsTray:
    def __init__(
        self,
        root: tk.Tk,
        *,
        post: Callable,
        unlock: Callable,
        lock: Callable,
        status: Callable[[], dict[str, object]],
        toggle: Callable[[], None],
        save_settings: Callable[[UserSettings], None],
        quit_application: Callable[[], None],
    ) -> None:
        self.root = root
        self.post = post
        self.unlock = unlock
        self.lock = lock
        self.status = status
        self.toggle = toggle
        self.save_settings = save_settings
        self.quit_application = quit_application
        self._settings_window: tk.Toplevel | None = None
        self.icon = pystray.Icon(
            "AAG LockLock",
            _icon_image(False),
            "AAG - InputLock",
            menu=pystray.Menu(
                pystray.MenuItem(self._status_text, None, enabled=False),
                pystray.MenuItem(self._toggle_text, self._schedule_toggle, default=True),
                pystray.MenuItem("Settings…", self._schedule_settings),
                pystray.MenuItem(
                    "Start at login",
                    self._schedule_startup_toggle,
                    checked=lambda _item: startup.is_enabled(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._schedule_quit),
            ),
        )

    def start(self) -> None:
        threading.Thread(target=self.icon.run, name="locklock-win-tray", daemon=True).start()

    def stop(self) -> None:
        self.icon.stop()

    def refresh(self) -> None:
        locked = bool(self.status().get("locked_kinds"))
        self.icon.icon = _icon_image(locked)
        self.icon.title = "AAG - InputLock — " + ("Locked" if locked else "Unlocked")
        self.icon.update_menu()

    def _status_text(self, _item) -> str:
        state = self.status()
        if state.get("power_error"):
            return "Power policy unavailable: " + str(state["power_error"])[:100]
        return "Input locked" if state.get("locked_kinds") else "Input unlocked"

    def _toggle_text(self, _item) -> str:
        return "Unlock Input" if self.status().get("locked_kinds") else "Lock Input"

    def _schedule_toggle(self, _icon, _item) -> None:
        action = self.unlock if self.status().get("locked_kinds") else self.lock
        self.post(lambda: self._safe_action(action))

    def _safe_action(self, action) -> None:
        try:
            action()
        except Exception as exc:
            messagebox.showerror("AAG - InputLock", str(exc), parent=self.root)

    def _schedule_settings(self, _icon, _item) -> None:
        self.post(self.open_settings)

    def _schedule_startup_toggle(self, _icon, _item) -> None:
        self.post(self._toggle_startup)

    def _toggle_startup(self) -> None:
        try:
            startup.set_enabled(not startup.is_enabled())
            self.icon.update_menu()
        except Exception as exc:
            messagebox.showerror("AAG - InputLock", str(exc), parent=self.root)

    def _schedule_quit(self, _icon, _item) -> None:
        self.post(self._confirm_quit)

    def _confirm_quit(self) -> None:
        if messagebox.askyesno(
            "Quit AAG - InputLock",
            "Unlock all input, restore temporary settings and quit AAG - InputLock?",
            parent=self.root,
        ):
            self.quit_application()

    @staticmethod
    def _vertical_number(parent, value: tk.IntVar, minimum: int, maximum: int):
        frame = ttk.Frame(parent)
        entry = ttk.Entry(frame, width=5, justify="center", textvariable=value)

        def change(amount: int) -> None:
            try:
                current = int(value.get())
            except (ValueError, tk.TclError):
                current = minimum
            value.set(max(minimum, min(maximum, current + amount)))

        ttk.Button(frame, text="+", width=3, command=lambda: change(1)).grid(row=0, column=0)
        entry.grid(row=1, column=0, pady=2)
        ttk.Button(frame, text="−", width=3, command=lambda: change(-1)).grid(row=2, column=0)
        return frame

    def open_settings(self) -> None:
        if self._settings_window and self._settings_window.winfo_exists():
            self._settings_window.lift()
            return
        state = self.status()
        window = tk.Toplevel(self.root)
        self._settings_window = window
        window.title("AAG - InputLock Settings")
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        body = ttk.Frame(window, padding=16)
        body.grid()

        kinds = set(state.get("default_locked_kinds", []))
        keyboard = tk.BooleanVar(value="keyboard" in kinds)
        pointer = tk.BooleanVar(value=bool(kinds & {"mouse", "touchpad"}))
        touchscreen = tk.BooleanVar(value=False)
        ttk.Label(body, text="Lock these devices", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(body, text="Keyboard", variable=keyboard).grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(body, text="Mouse and touchpad", variable=pointer).grid(row=2, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(body, text="Touchscreen — unavailable on Windows", variable=touchscreen, state="disabled").grid(row=3, column=0, columnspan=4, sticky="w")

        ttk.Separator(body).grid(row=4, column=0, columnspan=4, sticky="ew", pady=10)
        shortcut = tk.StringVar(value=str(state.get("primary_hotkey", "CTRL+ALT+Z")))
        ttk.Label(body, text="Shortcut:").grid(row=5, column=0, sticky="w")
        ttk.Entry(body, width=24, textvariable=shortcut, state="readonly").grid(row=5, column=1, columnspan=2, sticky="ew")
        ttk.Button(body, text="Change…", command=lambda: self._capture_shortcut(window, shortcut)).grid(row=5, column=3, padx=(8, 0))

        allowed_keys = list(state.get("allowed_keys", []))
        allowed_text = tk.StringVar()

        def refresh_allowed() -> None:
            allowed_text.set(f"Keys allowed while locked: {len(allowed_keys)}")

        refresh_allowed()
        ttk.Label(body, textvariable=allowed_text).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(body, text="Manage Keys…", command=lambda: self._edit_allowed_keys(window, allowed_keys, refresh_allowed)).grid(row=6, column=3, padx=(8, 0), pady=(8, 0))

        idle_enabled = tk.BooleanVar(value=state.get("idle_lock_enabled") is True)
        idle_minutes = tk.IntVar(value=max(1, int(state.get("idle_lock_seconds", 600)) // 60))
        ttk.Checkbutton(body, text="Automatically lock after no activity", variable=idle_enabled).grid(row=8, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(body, text="Minutes:").grid(row=9, column=0, sticky="w")
        ttk.Spinbox(body, from_=1, to=1440, width=7, textvariable=idle_minutes).grid(row=9, column=1, sticky="w")

        hide_cursor = tk.BooleanVar(value=state.get("hide_cursor_enabled") is True)
        cursor_total = int(state.get("hide_cursor_seconds", 60))
        cursor_minutes = tk.IntVar(value=cursor_total // 60)
        cursor_seconds = tk.IntVar(value=cursor_total % 60)
        ttk.Checkbutton(body, text="Global cursor hiding is unavailable on Windows", variable=hide_cursor, state="disabled").grid(row=10, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(body, text="Minutes").grid(row=11, column=0)
        ttk.Label(body, text="Seconds").grid(row=11, column=2)
        self._vertical_number(body, cursor_minutes, 0, 1439).grid(row=12, column=0)
        self._vertical_number(body, cursor_seconds, 0, 59).grid(row=12, column=2)

        ignore_lid = tk.BooleanVar(value=state.get("ignore_lid_close") is True)
        lid_policy = tk.BooleanVar(value=state.get("lid_power_policy_enabled") is True)
        battery = tk.IntVar(value=int(state.get("lid_battery_threshold", 20)))
        ttk.Checkbutton(body, text="Request keep running when the lid is closed", variable=ignore_lid).grid(row=13, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Checkbutton(body, text="Temporarily change Windows lid power policy", variable=lid_policy).grid(row=14, column=0, columnspan=4, sticky="w")
        ttk.Label(body, text="Restore normal policy at battery:").grid(row=15, column=0, columnspan=2, sticky="w")
        ttk.Spinbox(body, from_=1, to=99, width=7, textvariable=battery).grid(row=15, column=2, sticky="w")
        ttk.Label(body, text="%").grid(row=15, column=3, sticky="w")
        ttk.Label(body, text="Normal Windows policy is used unless both options are selected. Power handling is experimental until tested on this laptop.", foreground="#a15c00", wraplength=430).grid(row=16, column=0, columnspan=4, sticky="w", pady=(4, 8))

        buttons = ttk.Frame(body)
        buttons.grid(row=17, column=0, columnspan=4, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="left", padx=4)

        def save() -> None:
            try:
                selected: list[str] = []
                if keyboard.get():
                    selected.append("keyboard")
                if pointer.get():
                    selected.extend(("mouse", "touchpad"))
                if not selected:
                    raise ValueError("Select at least one supported device type.")
                primary = parse_hotkey(shortcut.get()).text
                if primary.endswith("+F12"):
                    raise ValueError("F12 cannot be the Windows primary shortcut.")
                minutes_value = int(idle_minutes.get())
                hide_total = int(cursor_minutes.get()) * 60 + int(cursor_seconds.get())
                settings = UserSettings(
                    default_locked_kinds=tuple(selected),
                    primary_hotkey=primary,
                    emergency_hotkey=str(state.get("emergency_hotkey", "CTRL+ALT+SHIFT+F12")),
                    idle_lock_enabled=idle_enabled.get(),
                    idle_lock_seconds=max(1, min(1440, minutes_value)) * 60,
                    hide_cursor_enabled=False,
                    hide_cursor_seconds=max(1, min(86_399, hide_total)),
                    start_at_login=startup.is_enabled(),
                    ignore_lid_close=ignore_lid.get(),
                    lid_power_policy_enabled=lid_policy.get(),
                    lid_battery_threshold=max(1, min(99, int(battery.get()))),
                    allowed_keys=tuple(allowed_keys),
                )
                self.save_settings(settings)
                window.destroy()
            except Exception as exc:
                messagebox.showerror("Settings", str(exc), parent=window)

        ttk.Button(buttons, text="Save", command=save).pack(side="left", padx=4)

    def _edit_allowed_keys(self, parent, selected: list[str], changed: Callable[[], None]) -> None:
        dialog = tk.Toplevel(parent)
        dialog.title("Keys Allowed While Locked")
        dialog.transient(parent)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="System recovery shortcuts (fixed)", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        state = self.status()
        for value in (
            f"Primary: {state.get('primary_hotkey', 'CTRL+ALT+Z')}",
            f"Emergency: {state.get('emergency_hotkey', 'CTRL+ALT+SHIFT+F12')}",
        ):
            ttk.Label(body, text=value, foreground="#808080").pack(anchor="w")
        ttk.Separator(body).pack(fill="x", pady=10)
        ttk.Label(body, text="These keys continue to reach applications while locked.").pack(anchor="w")
        listing = tk.Listbox(body, height=9, width=42)
        listing.pack(fill="both", expand=True, pady=8)

        def rebuild() -> None:
            listing.delete(0, tk.END)
            for key in selected:
                listing.insert(tk.END, display_key(key))

        def remove() -> None:
            indices = listing.curselection()
            if indices:
                selected.pop(indices[0])
                rebuild()

        def add() -> None:
            capture = tk.Toplevel(dialog)
            capture.title("Add Allowed Key")
            capture.transient(dialog)
            capture.grab_set()
            label = ttk.Label(capture, text="Press one non-modifier key.", padding=20)
            label.pack()

            def key(event):
                keysym = str(event.keysym).upper()
                aliases = {"ESCAPE": "ESC", "RETURN": "ENTER", "PRIOR": "PAGEUP", "NEXT": "PAGEDOWN"}
                candidate = aliases.get(keysym, keysym)
                if candidate not in ALLOWED_KEY_CATALOG:
                    label.configure(text="That key is not supported.")
                    return "break"
                reserved = {
                    str(state.get("primary_hotkey", "CTRL+ALT+Z")).split("+")[-1],
                    str(state.get("emergency_hotkey", "CTRL+ALT+SHIFT+F12")).split("+")[-1],
                }
                if candidate in reserved:
                    label.configure(text="That key belongs to a fixed recovery shortcut.")
                    return "break"
                if candidate not in selected:
                    selected.append(candidate)
                capture.destroy()
                rebuild()
                return "break"

            capture.bind("<KeyPress>", key)
            capture.focus_force()

        controls = ttk.Frame(body)
        controls.pack(fill="x")
        ttk.Button(controls, text="Add Key…", command=add).pack(side="left")
        ttk.Button(controls, text="Remove", command=remove).pack(side="left", padx=6)
        ttk.Button(controls, text="Done", command=lambda: (changed(), dialog.destroy())).pack(side="right")
        rebuild()

    def _capture_shortcut(self, parent: tk.Toplevel, target: tk.StringVar) -> None:
        dialog = tk.Toplevel(parent)
        dialog.title("Change Shortcut")
        dialog.transient(parent)
        ttk.Label(dialog, text="Press at least two modifiers and A–Z, 0–9, or F1–F11.", padding=16).pack()
        selected = ttk.Label(dialog, text="Waiting…")
        selected.pack(pady=(0, 16))

        def key(event) -> str:
            keysym = str(event.keysym).upper()
            if keysym in {"CONTROL_L", "CONTROL_R", "ALT_L", "ALT_R", "SHIFT_L", "SHIFT_R", "SUPER_L", "SUPER_R"}:
                return "break"
            user32 = ctypes.windll.user32
            modifiers = []
            for name, codes in (
                ("CTRL", (0x11, 0xA2, 0xA3)),
                ("ALT", (0x12, 0xA4, 0xA5)),
                ("SHIFT", (0x10, 0xA0, 0xA1)),
                ("SUPER", (0x5B, 0x5C)),
            ):
                if any(user32.GetKeyState(code) & 0x8000 for code in codes):
                    modifiers.append(name)
            candidate = "+".join((*modifiers, keysym))
            try:
                canonical = parse_hotkey(candidate).text
                if canonical.endswith("+F12"):
                    raise ValueError("F12 is reserved on Windows")
            except ValueError as exc:
                selected.configure(text=str(exc))
                return "break"
            target.set(canonical)
            dialog.destroy()
            return "break"

        dialog.bind("<KeyPress>", key)
        dialog.grab_set()
        dialog.focus_force()
