"""Interactive Windows AAG - InputLock agent and tray entry point."""

from __future__ import annotations

import ctypes
import os
import sys
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path

from locklock_core import UserSettings, load_settings, save_settings
from locklock_windows.cursor import WindowsCursorController
from locklock_windows.input_hooks import WindowsInputHooks, configure_kernel32
from locklock_windows.dispatch import UiQueue, PowerWorker
from locklock_windows.ipc import NamedPipeServer, current_user_sid
from locklock_windows.paths import SETTINGS_PATH, pipe_name_for_sid
from locklock_windows.service_client import PowerServiceClient
from locklock_windows.session import WindowsSessionMonitor
from locklock_windows.state import AgentController
from locklock_windows.tray import WindowsTray


def _default_settings() -> UserSettings:
    return UserSettings(default_locked_kinds=("keyboard", "mouse", "touchpad"))


def _load_or_create_settings(path: Path) -> UserSettings:
    if not path.exists():
        settings = _default_settings()
        save_settings(path, settings)
        return settings
    try:
        settings = load_settings(path)
        if settings.ignore_lid_close or settings.hide_cursor_enabled:
            settings = replace(settings, ignore_lid_close=False, hide_cursor_enabled=False)
            save_settings(path, settings)
        return settings
    except Exception:
        invalid = path.with_name(f"settings.invalid-{int(time.time())}.json")
        path.replace(invalid)
        settings = _default_settings()
        save_settings(path, settings)
        return settings


class SingleInstance:
    def __init__(self) -> None:
        configure_kernel32(ctypes.windll.kernel32)
        self.handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AAG-LockLock-Agent")
        last_error = ctypes.windll.kernel32.GetLastError()
        if not self.handle:
            raise ctypes.WinError()
        if last_error == 183:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError("AAG - InputLock is already running")

    def close(self) -> None:
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


class WindowsApplication:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("The Windows agent can run only on Windows")
        self._closing = False
        self.ui = UiQueue()
        self.instance = SingleInstance()
        self.root = tk.Tk()
        self.root.withdraw()
        self.sid = current_user_sid()
        self.settings = _load_or_create_settings(SETTINGS_PATH)
        self.cursor = WindowsCursorController()
        controller_holder: dict[str, AgentController] = {}

        def controller() -> AgentController | None:
            return controller_holder.get("controller")

        self.backend = WindowsInputHooks(
            self.settings.primary_hotkey,
            self.settings.emergency_hotkey,
            self.settings.allowed_keys,
            on_toggle=lambda: controller() and controller().toggle_default(),
            on_emergency=lambda: controller() and controller().unlock(),
            on_activity=lambda kind: controller() and controller().activity(kind),
        )
        self.controller = AgentController(
            self.backend,
            self.settings,
            SETTINGS_PATH,
            cursor_hide=self.cursor.hide,
            cursor_show=self.cursor.show,
            state_changed=self._state_changed,
        )
        controller_holder["controller"] = self.controller
        self.session_monitor = WindowsSessionMonitor(self.controller.session_changed)
        self.power = PowerServiceClient(self.sid)
        self.power_worker = PowerWorker(self.power, lambda: self.ui.post(self._refresh_tray))
        self.pipe = NamedPipeServer(
            pipe_name_for_sid(self.sid), self.sid, self._dispatch
        )
        self.tray = WindowsTray(
            self.root,
            status=self._status,
            post=self.ui.post,
            unlock=self.controller.unlock,
            lock=lambda: self.controller.lock(frozenset(self.controller.settings.default_locked_kinds)),
            toggle=self.controller.toggle_default,
            save_settings=self._save_settings,
            quit_application=self.close,
        )
        self._closing = False

    def run(self) -> int:
        self.pipe.start()
        self.tray.start()
        self.root.after(100, self._tick)
        self._sync_power()
        try:
            self.root.mainloop()
        finally:
            self._cleanup()
        return 0

    def _tick(self) -> None:
        if self._closing:
            return
        try:
            self.ui.drain()
            if self.pipe.error:
                self.close()
                return
            self.controller.tick()
        finally:
            self.root.after(250, self._tick)

    def _dispatch(self, request: dict[str, object]) -> dict[str, object]:
        response = self.controller.dispatch(request)
        if request.get("cmd") == "quit" and response.get("ok") is True:
            self.ui.post(self.close)
        return response

    def _status(self):
        return {**self.controller.status(), "power_error": self.power.last_error}

    def _refresh_tray(self):
        if hasattr(self, "tray") and not self._closing:
            self.tray.refresh()

    def _sync_power(self) -> None:
        if hasattr(self, "power_worker") and not self._closing:
            self.power_worker.submit(self.controller.settings, self.controller.session_active)

    def _save_settings(self, settings: UserSettings) -> None:
        self.controller.set_settings(settings)
        self.settings = settings
        self._sync_power()
        self.tray.refresh()

    def _state_changed(self, _state: dict[str, object]) -> None:
        self._sync_power()
        self.ui.post(self._refresh_tray)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.root.after(0, self.root.quit)

    def _cleanup(self) -> None:
        self._closing = True
        try:
            self.controller.close()
        finally:
            self.session_monitor.close()
            try:
                self.power_worker.close()
            except Exception:
                pass
            self.pipe.close()
            self.tray.stop()
            self.cursor.close()
            self.instance.close()
            try:
                self.root.destroy()
            except tk.TclError:
                pass


def main() -> int:
    if os.name != "nt":
        print("AAG - InputLock Windows agent requires Windows 11.", file=sys.stderr)
        return 2
    try:
        return WindowsApplication().run()
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(None, str(exc), "AAG - InputLock", 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
