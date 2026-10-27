"""Platform-neutral orchestration for the Windows interactive agent."""

from __future__ import annotations

from contextlib import contextmanager
import threading
import time
from collections.abc import Callable
from pathlib import Path

from locklock_core import InputBackend, PolicyAction, PolicyEngine, UserSettings, parse_hotkey, save_settings


MODE_KINDS = {
    "keyboard": frozenset({"keyboard"}),
    "mouse": frozenset({"mouse", "touchpad"}),
    "touchpad": frozenset({"touchpad"}),
    "touchscreen": frozenset({"touchscreen"}),
    "all": frozenset({"keyboard", "mouse", "touchpad"}),
    "all-input": frozenset({"keyboard", "mouse", "touchpad", "touchscreen"}),
}


class AgentController:
    def __init__(
        self,
        backend: InputBackend,
        settings: UserSettings,
        settings_path: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        cursor_hide: Callable[[], None] | None = None,
        cursor_show: Callable[[], None] | None = None,
        state_changed: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.backend = backend
        self.settings = settings
        self.settings_path = settings_path
        self.clock = clock
        self.cursor_hide = cursor_hide or (lambda: None)
        self.cursor_show = cursor_show or (lambda: None)
        self.state_changed = state_changed or (lambda _state: None)
        self.policy = PolicyEngine(settings=settings, last_activity=clock())
        self._lock = threading.RLock()
        self._stopping = False
        self.session_active = False
        self._transaction_depth = 0
        self._notify_pending = False

    @contextmanager
    def _transaction(self):
        notification = None
        try:
            with self._lock:
                self._transaction_depth += 1
                try:
                    yield
                finally:
                    self._transaction_depth -= 1
                    if self._transaction_depth == 0 and self._notify_pending:
                        self._notify_pending = False
                        notification = self.status()
        finally:
            # Never invoke UI/client code while any controller lock is held,
            # including nested policy actions such as tick -> lock.
            if notification is not None:
                self.state_changed(notification)

    def session_changed(self, active: bool) -> None:
        with self._transaction():
            if self.session_active != active:
                self.session_active = active
                self.backend.unlock()
                self.policy.unlocked(self.clock())
                self._notify_pending = True

    def status(self) -> dict[str, object]:
        with self._lock:
            capabilities = self.backend.capabilities
            return {
                "platform": "windows",
                "session_active": self.session_active,
                "daemon": "running" if not self._stopping else "stopping",
                "locked_kinds": sorted(self.backend.locked_kinds),
                "default_locked_kinds": list(self.settings.default_locked_kinds),
                "primary_hotkey": self.settings.primary_hotkey,
                "emergency_hotkey": self.settings.emergency_hotkey,
                "idle_lock_enabled": self.settings.idle_lock_enabled,
                "idle_lock_seconds": self.settings.idle_lock_seconds,
                "hide_cursor_enabled": self.settings.hide_cursor_enabled,
                "hide_cursor_seconds": self.settings.hide_cursor_seconds,
                "allowed_keys": list(self.settings.allowed_keys),
                "ignore_lid_close": self.settings.ignore_lid_close,
                "lid_power_policy_enabled": self.settings.lid_power_policy_enabled,
                "lid_battery_threshold": self.settings.lid_battery_threshold,
                "capabilities": {
                    name: getattr(capabilities, name).value
                    for name in (
                        "keyboard",
                        "mouse",
                        "touchpad",
                        "touchscreen",
                        "cursor_hiding",
                        "lid_control",
                    )
                },
                "capability_detail": capabilities.detail,
            }

    def activity(self, kind: str) -> None:
        with self._transaction():
            actions = self.policy.activity(
                self.clock(), pointer=kind in {"mouse", "touchpad", "touchscreen", "pointer"}
            )
            self._apply_policy_actions(actions)

    def lock(self, kinds: frozenset[str], *, timeout: int = 0) -> dict[str, object]:
        with self._transaction():
            if self._stopping or not self.session_active:
                raise RuntimeError("AAG - InputLock requires an active unlocked console session")
            self.backend.capabilities.require_lockable(kinds)
            before = self.backend.locked_kinds
            try:
                self.backend.lock(kinds)
            except BaseException:
                if self.backend.locked_kinds != before:
                    self.backend.unlock()
                raise
            self.policy.locked(self.clock(), timeout)
            state = self.status()
            self._notify_pending = True
            return state

    def unlock(self, kinds: frozenset[str] | None = None) -> dict[str, object]:
        with self._transaction():
            self.backend.unlock(kinds)
            if not self.backend.locked_kinds:
                self.policy.unlocked(self.clock())
            state = self.status()
            self._notify_pending = True
            return state

    def toggle_default(self) -> dict[str, object]:
        kinds = frozenset(self.settings.default_locked_kinds)
        if self.backend.locked_kinds:
            return self.unlock()
        return self.lock(kinds)

    def set_settings(self, settings: UserSettings) -> dict[str, object]:
        if settings.hide_cursor_enabled:
            raise ValueError("Global Windows cursor hiding is unsupported")
        settings.default_locked_kinds and self.backend.capabilities.require_lockable(
            frozenset(settings.default_locked_kinds)
        )
        reserved = {
            parse_hotkey(settings.primary_hotkey).key,
            parse_hotkey(settings.emergency_hotkey).key,
        }
        if reserved & set(settings.allowed_keys):
            raise ValueError("a shortcut key cannot also be an allowed key")
        with self._transaction():
            update_hotkeys = getattr(self.backend, "update_hotkeys", None)
            if callable(update_hotkeys):
                update_hotkeys(settings.primary_hotkey, settings.emergency_hotkey)
            update_allowed_keys = getattr(self.backend, "update_allowed_keys", None)
            if callable(update_allowed_keys):
                update_allowed_keys(settings.allowed_keys)
            try:
                save_settings(self.settings_path, settings)
            except BaseException:
                if callable(update_hotkeys):
                    update_hotkeys(
                        self.settings.primary_hotkey, self.settings.emergency_hotkey
                    )
                if callable(update_allowed_keys):
                    update_allowed_keys(self.settings.allowed_keys)
                raise
            self.settings = settings
            actions = self.policy.update_settings(settings, self.clock())
            self._apply_policy_actions(actions)
            state = self.status()
            self._notify_pending = True
            return state

    def tick(self) -> None:
        with self._transaction():
            if not self.session_active or self._stopping:
                return
            actions = self.policy.tick(
                self.clock(), is_locked=bool(self.backend.locked_kinds)
            )
            self._apply_policy_actions(actions)

    def dispatch(self, request: dict[str, object]) -> dict[str, object]:
        command = request.get("cmd")
        try:
            if command == "status":
                state = self.status()
            elif command == "lock-default":
                state = self.lock(frozenset(self.settings.default_locked_kinds))
            elif command == "toggle-default":
                state = self.toggle_default()
            elif command in {"lock", "unlock", "toggle"}:
                mode = request.get("mode")
                if not isinstance(mode, str) or mode not in MODE_KINDS:
                    raise ValueError("unknown lock mode")
                kinds = MODE_KINDS[mode]
                if command == "lock":
                    timeout = request.get("timeout", 0)
                    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 <= timeout <= 86_400:
                        raise ValueError("timeout must be between 0 and 86400 seconds")
                    state = self.lock(kinds, timeout=timeout)
                elif command == "unlock":
                    state = self.unlock(kinds)
                elif self.backend.locked_kinds & kinds:
                    state = self.unlock(kinds)
                else:
                    state = self.lock(kinds)
            elif command == "emergency-unlock":
                state = self.unlock()
            elif command == "set-settings":
                payload = request.get("settings")
                if not isinstance(payload, dict):
                    raise ValueError("settings must be an object")
                state = self.set_settings(UserSettings.from_mapping(payload))
            elif command == "quit":
                self.close()
                state = self.status()
            else:
                raise ValueError("unknown command")
            return {"ok": True, "state": state}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "state": self.status()}

    def _apply_policy_actions(self, actions: tuple[PolicyAction, ...]) -> None:
        for action in actions:
            if action is PolicyAction.LOCK_DEFAULT:
                self.lock(frozenset(self.settings.default_locked_kinds))
            elif action is PolicyAction.UNLOCK_ALL:
                self.unlock()
            elif action is PolicyAction.HIDE_CURSOR:
                self.cursor_hide()
            elif action is PolicyAction.SHOW_CURSOR:
                self.cursor_show()

    def close(self) -> None:
        with self._transaction():
            if self._stopping:
                return
            self._stopping = True
            self.backend.unlock()
            self.cursor_show()
            self._notify_pending = True
        self.backend.close()
